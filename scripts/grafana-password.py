#!/usr/bin/env python3
"""Create newline-free credentials; migrate the old trailing-newline password safely.

Run prepare before Helm and migrate after Grafana is ready, through privileged
Ansible/make deploy (preserving file ownership may require root). Migration repairs
partial runs (DB -> file -> Secret -> Pod environment). Passwords
never enter process arguments, output, or HTTP redirects. Requires only Python stdlib.
Grafana v13.2.3 API: pkg/api/user.go ChangeUserPassword (PUT /api/user/password).
"""
import argparse
import base64
import contextlib
import json
import hashlib
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def read_password(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Password file must be a regular file, not a symlink')
    raw = path.read_bytes()
    if not raw.rstrip(b'\r\n'):
        raise ValueError('Password file is empty')
    try:
        raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Password file must contain UTF-8 text') from None
    return raw


def prepare(path):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        read_password(path)
        return
    # Publish a complete file without overwriting one created concurrently.
    descriptor, temporary = tempfile.mkstemp(prefix='.grafana-password-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(secrets.token_hex(24).encode('ascii'))
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            read_password(path)
    finally:
        os.unlink(temporary)


def validate_base_url(base):
    parsed = urllib.parse.urlsplit(base)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1', 'localhost')
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Grafana API URL must be plain HTTP on loopback with no path or credentials')
    # Force malformed ports to fail before constructing an authenticated request.
    _ = parsed.port
    return base.rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def api(base, password, path='/api/user', payload=None):
    credentials = base64.b64encode(b'admin:' + password).decode('ascii')
    headers = {'Authorization': 'Basic ' + credentials}
    body = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(base + path, data=body, headers=headers,
                                     method='PUT' if payload is not None else 'GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            code = response.status
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
    except (urllib.error.URLError, OSError):
        raise ValueError('Grafana API connection failed; credentials were not printed') from None
    if code not in (200, 401):
        raise ValueError(f'Grafana API returned HTTP {code}; no response body logged')
    return code


def replace_password(path, password):
    original = path.stat()
    descriptor, temporary = tempfile.mkstemp(prefix='.grafana-password-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            # chown may clear mode bits, so restore the mode afterwards.
            if (os.fstat(output.fileno()).st_uid, os.fstat(output.fileno()).st_gid) != (original.st_uid, original.st_gid):
                os.fchown(output.fileno(), original.st_uid, original.st_gid)
            os.fchmod(output.fileno(), stat.S_IMODE(original.st_mode))
            output.write(password)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reconcile_secret(password):
    document = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
                'metadata': {'namespace': 'observability', 'name': 'grafana-admin'},
                'data': {'admin-user': base64.b64encode(b'admin').decode('ascii'),
                         'admin-password': base64.b64encode(password).decode('ascii')}}
    try:
        result = subprocess.run(['kubectl', '--request-timeout=20s', 'apply', '--server-side',
                                 '--field-manager=mtc-grafana', '-f', '-'],
                                input=json.dumps(document).encode('utf-8'),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('Grafana Secret reconciliation failed; rerun migrate to recover') from None
    if result.returncode:
        raise ValueError('Grafana Secret reconciliation failed; rerun migrate to recover')


def refresh_grafana_env(password):
    # Kubernetes changes the Pod template only when this deterministic value differs.
    # A missing annotation also repairs deployments left behind by the old helper.
    fingerprint = hashlib.sha256(password).hexdigest()
    try:
        snapshot = subprocess.run(['kubectl', '--request-timeout=20s', '-n', 'observability',
                                   'get', 'deployment', 'monitoring-grafana', '-o', 'json'],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  timeout=30, check=False)
        if snapshot.returncode:
            raise ValueError('Grafana deployment inspection failed')
        deployment = json.loads(snapshot.stdout)
        annotations = deployment['spec']['template']['metadata'].get('annotations', {})
        changed = annotations.get('mtc-hack/grafana-password-sha256') != fingerprint
        status = deployment.get('status', {})
        replicas = deployment['spec'].get('replicas', 1)
        unsettled = (status.get('observedGeneration', 0) < deployment.get('metadata', {}).get('generation', 1)
                     or status.get('updatedReplicas', 0) != replicas
                     or status.get('readyReplicas', 0) != replicas
                     or status.get('availableReplicas', 0) != replicas)
    except (OSError, subprocess.TimeoutExpired, KeyError, json.JSONDecodeError):
        raise ValueError('Grafana deployment inspection failed; rerun migrate') from None
    document = {'spec': {'template': {'metadata': {'annotations': {
        'mtc-hack/grafana-password-sha256': fingerprint}}}}}
    commands = [(['kubectl', '--request-timeout=20s', '-n', 'observability', 'patch',
                  'deployment', 'monitoring-grafana', '--type=merge', '--patch-file=/dev/stdin'],
                 json.dumps(document).encode('utf-8'), 30),
                (['kubectl', '-n', 'observability', 'rollout', 'status',
                  'deployment/monitoring-grafana', '--timeout=180s'], None, 190)]
    for command, body, timeout in commands:
        try:
            result = subprocess.run(command, input=body, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Grafana credential rollout failed; rerun migrate to recover') from None
        if result.returncode:
            raise ValueError('Grafana credential rollout failed; rerun migrate to recover')
    return changed or unsettled


def wait_for_lockout():
    # Grafana13.2.3 stores attempts for5min; username lockout uses the same401 as
    # invalid credentials. Do not keep submitting failed logins during cooldown.
    print('Grafana authentication rejected; waiting 310s once for existing login protection to expire', flush=True)
    for _ in range(31):
        time.sleep(10)


def authenticate(base, desired, raw, allow_cooldown):
    for attempt in range(2 if allow_cooldown else 1):
        if api(base, desired) == 200:
            return desired
        if raw != desired and api(base, raw) == 200:
            return raw
        if attempt == 0 and allow_cooldown:
            wait_for_lockout()
    raise ValueError('Grafana credentials rejected; refusing to reset an unrelated password')


def migrate(path, base=None):
    path = Path(path)
    if base is not None:
        base = validate_base_url(base)
    raw = read_password(path)
    desired = raw.rstrip(b'\r\n')
    changed = False
    if raw == desired:
        # Stop stale sidecars BEFORE auth, including after a file/Secret-only crash.
        reconcile_secret(raw)
        changed = refresh_grafana_env(raw)
    if base is None:
        import verify
        connection = verify.port_forward('monitoring-grafana', 80)
    else:
        connection = contextlib.nullcontext(base)
    # Port-forward targets a Pod: open after pre-auth rollout and close before the
    # post-migration rollout, otherwise the restarted Pod kills this connection.
    with connection as endpoint:
        current = authenticate(endpoint, desired, raw, changed or raw != desired)
        if current != desired:
            status = api(endpoint, raw, '/api/user/password',
                         {'oldPassword': raw.decode('utf-8'), 'newPassword': desired.decode('utf-8')})
            if status != 200 or api(endpoint, desired) != 200:
                raise ValueError('Grafana password migration could not be verified; rerun migrate')
    if desired != raw:
        replace_password(path, desired)
        reconcile_secret(desired)
        refresh_grafana_env(desired)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'migrate'))
    parser.add_argument('--password-file', type=Path, default=ROOT / '.secrets/grafana-password')
    parser.add_argument('--base-url', help='Test override: loopback HTTP URL only')
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            if args.base_url:
                raise ValueError('--base-url is only valid for migrate')
            prepare(args.password_file)
        else:
            migrate(args.password_file, args.base_url)
    except PermissionError:
        print('Grafana password migration needs privileged Ansible/make deploy to preserve file owner and group', file=sys.stderr)
        return 1
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # Paths and API errors are controlled; never dump arbitrary subprocess output.
        print(f'Grafana password {args.command} failed: {error}', file=sys.stderr)
        return 1
    print(f'Grafana password {args.command}: OK (credential values hidden)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
