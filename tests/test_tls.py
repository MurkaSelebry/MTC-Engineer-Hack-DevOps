import os
from pathlib import Path
import shutil
import ssl
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "create-tls.sh"


class TLSLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.project = Path(self.tempdir.name)
        scripts = self.project / "scripts"
        scripts.mkdir()
        shutil.copy2(SCRIPT, scripts / SCRIPT.name)

        bindir = self.project / "bin"
        bindir.mkdir()
        kubectl = bindir / "kubectl"
        kubectl.write_text(
            "#!/bin/sh\n"
            "case \" $* \" in\n"
            "  *\" create secret tls \"*) printf '%s\\n' 'apiVersion: v1' 'kind: Secret' ;;\n"
            "  *\" apply \"*) cat >/dev/null ;;\n"
            "  *) exit 64 ;;\n"
            "esac\n"
        )
        kubectl.chmod(0o755)
        self.env = os.environ.copy()
        self.env["PATH"] = f"{bindir}:{self.env['PATH']}"
        self.env.pop("DEPLOY_USER", None)

    def tearDown(self):
        try:
            if self.secrets.exists():
                self.assertEqual(list(self.secrets.glob(".tls-work.*")), [])
        finally:
            self.tempdir.cleanup()

    @property
    def secrets(self):
        return self.project / ".secrets"

    def command(self, *args, check=True, cwd=None):
        return subprocess.run(
            args,
            cwd=cwd or self.project,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )

    def run_script(self, check=True):
        return self.command("bash", "scripts/create-tls.sh", check=check)

    def openssl(self, *args, check=True):
        return self.command("openssl", *args, check=check)

    def assert_valid_leaf(self):
        self.openssl(
            "verify", "-CAfile", str(self.secrets / "ca.crt"),
            str(self.secrets / "tls.crt"),
        )
        self.openssl("x509", "-in", str(self.secrets / "tls.crt"), "-checkhost", "demo.test", "-noout")
        self.openssl("x509", "-in", str(self.secrets / "tls.crt"), "-checkhost", "canary.test", "-noout")

    def certificate_expiry(self, path):
        result = self.openssl("x509", "-in", str(path), "-enddate", "-noout")
        return ssl.cert_time_to_seconds(result.stdout.strip().removeprefix("notAfter="))

    def replace_leaf(self, *, days=1, ca_cert=None, ca_key=None, san="DNS:demo.test,DNS:canary.test"):
        ca_cert = ca_cert or self.secrets / "ca.crt"
        ca_key = ca_key or self.secrets / "ca.key"
        csr = self.project / "replacement.csr"
        ext = self.project / "replacement.ext"
        ext.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=serverAuth\n"
            f"subjectAltName={san}\n"
        )
        self.openssl(
            "req", "-new", "-sha256", "-key", str(self.secrets / "tls.key"),
            "-subj", "/CN=demo.test", "-out", str(csr),
        )
        self.openssl(
            "x509", "-req", "-sha256", "-days", str(days), "-in", str(csr),
            "-CA", str(ca_cert), "-CAkey", str(ca_key), "-CAcreateserial",
            "-extfile", str(ext), "-out", str(self.secrets / "tls.crt"),
        )

    def test_initial_run_creates_restrictive_valid_material(self):
        self.run_script()

        self.assert_valid_leaf()
        self.assertEqual(stat.S_IMODE(self.secrets.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.secrets / "ca.key").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.secrets / "tls.key").stat().st_mode), 0o600)

    def test_repeat_with_healthy_leaf_preserves_all_key_and_certificate_bytes(self):
        self.run_script()
        before = {path.name: path.read_bytes() for path in self.secrets.glob("*.key")}
        before.update({path.name: path.read_bytes() for path in self.secrets.glob("*.crt")})

        self.run_script()

        after = {name: (self.secrets / name).read_bytes() for name in before}
        self.assertEqual(after, before)

    def test_leaf_expiring_within_thirty_days_is_renewed_with_same_private_key(self):
        self.run_script()
        original_key = (self.secrets / "tls.key").read_bytes()
        self.replace_leaf(days=1)
        expiring_cert = (self.secrets / "tls.crt").read_bytes()

        self.run_script()

        self.assertEqual((self.secrets / "tls.key").read_bytes(), original_key)
        self.assertNotEqual((self.secrets / "tls.crt").read_bytes(), expiring_cert)
        self.openssl("x509", "-in", str(self.secrets / "tls.crt"), "-checkend", str(30 * 86400), "-noout")
        self.assert_valid_leaf()

    def test_expired_leaf_is_authenticated_without_time_check_then_renewed(self):
        self.run_script()
        original_key = (self.secrets / "tls.key").read_bytes()
        self.replace_leaf(days=0)

        self.run_script()

        self.assertEqual((self.secrets / "tls.key").read_bytes(), original_key)
        self.openssl("x509", "-in", str(self.secrets / "tls.crt"), "-checkend", str(30 * 86400), "-noout")
        self.assert_valid_leaf()

    def test_new_leaf_expiry_is_bounded_by_shorter_lived_ca(self):
        self.secrets.mkdir(mode=0o700)
        self.openssl(
            "ecparam", "-name", "prime256v1", "-genkey", "-noout",
            "-out", str(self.secrets / "ca.key"),
        )
        self.openssl(
            "req", "-x509", "-new", "-sha256", "-days", "45",
            "-key", str(self.secrets / "ca.key"), "-subj", "/CN=Short CA",
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-out", str(self.secrets / "ca.crt"),
        )

        self.run_script()

        self.assertLessEqual(
            self.certificate_expiry(self.secrets / "tls.crt"),
            self.certificate_expiry(self.secrets / "ca.crt"),
        )
        self.assert_valid_leaf()

    def test_leaf_signed_by_another_ca_is_rejected_without_replacement(self):
        self.run_script()
        other_key = self.project / "other-ca.key"
        other_cert = self.project / "other-ca.crt"
        self.openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(other_key))
        self.openssl(
            "req", "-x509", "-new", "-sha256", "-days", "3650", "-key", str(other_key),
            "-subj", "/CN=Other CA", "-out", str(other_cert),
        )
        self.replace_leaf(days=1, ca_cert=other_cert, ca_key=other_key)
        wrong_cert = (self.secrets / "tls.crt").read_bytes()

        result = self.run_script(check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.secrets / "tls.crt").read_bytes(), wrong_cert)

    def test_leaf_with_mismatched_private_key_is_rejected(self):
        self.run_script()
        original_cert = (self.secrets / "tls.crt").read_bytes()
        self.openssl(
            "ecparam", "-name", "prime256v1", "-genkey", "-noout",
            "-out", str(self.secrets / "tls.key"),
        )

        result = self.run_script(check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.secrets / "tls.crt").read_bytes(), original_cert)

    def test_ca_with_mismatched_private_key_is_rejected(self):
        self.run_script()
        original_leaf = (self.secrets / "tls.crt").read_bytes()
        self.openssl(
            "ecparam", "-name", "prime256v1", "-genkey", "-noout",
            "-out", str(self.secrets / "ca.key"),
        )

        result = self.run_script(check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.secrets / "tls.crt").read_bytes(), original_leaf)

    def test_leaf_with_wrong_san_is_rejected_instead_of_silently_reissued(self):
        self.run_script()
        self.replace_leaf(days=1, san="DNS:demo.test")
        wrong_cert = (self.secrets / "tls.crt").read_bytes()

        result = self.run_script(check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.secrets / "tls.crt").read_bytes(), wrong_cert)

    def test_partial_leaf_pair_is_rejected(self):
        self.run_script()
        (self.secrets / "tls.crt").unlink()

        result = self.run_script(check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.secrets / "tls.crt").exists())


if __name__ == "__main__":
    unittest.main()
