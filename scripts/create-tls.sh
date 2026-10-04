#!/usr/bin/env bash
# Preserve the local CA, validate existing material, and renew only an aging leaf.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
umask 077
mkdir -p .secrets
chmod 700 .secrets

workdir=$(mktemp -d .secrets/.tls-work.XXXXXX)
cleanup() {
  rm -rf -- "$workdir"
}
trap cleanup EXIT

public_key_from_certificate() {
  openssl x509 -in "$1" -pubkey -noout | openssl sha256
}

public_key_from_private_key() {
  openssl pkey -in "$1" -pubout | openssl sha256
}

require_matching_key() {
  local certificate=$1 private_key=$2 description=$3
  [[ "$(public_key_from_certificate "$certificate")" == "$(public_key_from_private_key "$private_key")" ]] || {
    echo "$description certificate/private key mismatch." >&2
    exit 1
  }
}

validate_leaf_identity() {
  local certificate=$1 private_key=$2
  openssl verify -no_check_time -CAfile .secrets/ca.crt "$certificate" >/dev/null || {
    echo 'TLS certificate is not signed by the preserved local CA.' >&2
    exit 1
  }
  openssl verify -no_check_time -verify_hostname demo.test -CAfile .secrets/ca.crt "$certificate" >/dev/null || {
    echo 'TLS certificate does not contain SAN demo.test.' >&2
    exit 1
  }
  openssl verify -no_check_time -verify_hostname canary.test -CAfile .secrets/ca.crt "$certificate" >/dev/null || {
    echo 'TLS certificate does not contain SAN canary.test.' >&2
    exit 1
  }
  require_matching_key "$certificate" "$private_key" TLS
}

leaf_validity_days() {
  local ca_not_after ca_epoch now remaining days
  ca_not_after=$(openssl x509 -in .secrets/ca.crt -enddate -noout)
  ca_not_after=${ca_not_after#notAfter=}
  ca_epoch=$(python3 - "$ca_not_after" <<'PY'
import ssl
import sys

print(int(ssl.cert_time_to_seconds(sys.argv[1])))
PY
)
  now=$(date +%s)
  remaining=$((ca_epoch - now))
  days=$((remaining / 86400))
  (( days > 365 )) && days=365
  (( days >= 31 )) || {
    echo 'Local CA has fewer than 31 full days remaining; rotate trust explicitly before issuing another leaf.' >&2
    exit 1
  }
  printf '%s\n' "$days"
}

issue_leaf() {
  local private_key=$1 certificate=$2 days serial
  days=$(leaf_validity_days)
  serial=$(openssl rand -hex 16)
  openssl req -new -sha256 -key "$private_key" -subj '/CN=demo.test' -out "$workdir/tls.csr"
  cat > "$workdir/tls.ext" <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectAltName=DNS:demo.test,DNS:canary.test
EOF
  openssl x509 -req -sha256 -days "$days" -in "$workdir/tls.csr" \
    -CA .secrets/ca.crt -CAkey .secrets/ca.key -set_serial "0x$serial" \
    -extfile "$workdir/tls.ext" -out "$certificate"
  validate_leaf_identity "$certificate" "$private_key"
  openssl x509 -in "$certificate" -checkend $((30 * 86400)) -noout >/dev/null
}

if [[ ! -e .secrets/ca.crt && ! -e .secrets/ca.key ]]; then
  openssl ecparam -name prime256v1 -genkey -noout -out "$workdir/ca.key"
  openssl req -x509 -new -sha256 -days 3650 -key "$workdir/ca.key" \
    -subj '/CN=MTC Hack Demo Local CA' \
    -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' -out "$workdir/ca.crt"
  require_matching_key "$workdir/ca.crt" "$workdir/ca.key" CA
  chmod 600 "$workdir/ca.key"
  chmod 644 "$workdir/ca.crt"
  mv "$workdir/ca.key" .secrets/ca.key
  mv "$workdir/ca.crt" .secrets/ca.crt
fi
[[ -f .secrets/ca.crt && -f .secrets/ca.key ]] || {
  echo 'Incomplete local CA: restore the matching files; refusing to replace trust silently.' >&2
  exit 1
}
chmod 600 .secrets/ca.key
chmod 644 .secrets/ca.crt
require_matching_key .secrets/ca.crt .secrets/ca.key CA
openssl verify -CAfile .secrets/ca.crt .secrets/ca.crt >/dev/null
openssl x509 -in .secrets/ca.crt -checkend 86400 -noout >/dev/null || {
  echo 'Local CA expires within 24 hours; rotate trust explicitly.' >&2
  exit 1
}

if [[ ! -e .secrets/tls.crt && ! -e .secrets/tls.key ]]; then
  openssl ecparam -name prime256v1 -genkey -noout -out "$workdir/tls.key"
  issue_leaf "$workdir/tls.key" "$workdir/tls.crt"
  chmod 600 "$workdir/tls.key"
  chmod 644 "$workdir/tls.crt"
  mv "$workdir/tls.key" .secrets/tls.key
  mv "$workdir/tls.crt" .secrets/tls.crt
fi
[[ -f .secrets/tls.crt && -f .secrets/tls.key ]] || {
  echo 'Incomplete TLS key pair: refusing to silently replace it.' >&2
  exit 1
}
chmod 600 .secrets/tls.key
chmod 644 .secrets/tls.crt
validate_leaf_identity .secrets/tls.crt .secrets/tls.key

if ! openssl x509 -in .secrets/tls.crt -checkend $((30 * 86400)) -noout >/dev/null; then
  issue_leaf .secrets/tls.key "$workdir/tls.crt"
  chmod 644 "$workdir/tls.crt"
  mv -f "$workdir/tls.crt" .secrets/tls.crt
fi

openssl verify -CAfile .secrets/ca.crt .secrets/tls.crt >/dev/null
validate_leaf_identity .secrets/tls.crt .secrets/tls.key
kubectl -n demo create secret tls demo-tls --cert=.secrets/tls.crt --key=.secrets/tls.key \
  --dry-run=client -o yaml | kubectl apply --server-side --field-manager=mtc-tls -f -
if [[ -n "${DEPLOY_USER:-}" ]]; then
  chown -R "$DEPLOY_USER" .secrets
fi
