#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
trap 'echo "Bootstrap failed at line $LINENO; no cluster reset was performed." >&2' ERR
source /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 && "$(uname -m)" == x86_64 ]] || {
  echo 'This reproducible profile requires Ubuntu 24.04 amd64.' >&2; exit 1;
}
sudo -n true || { echo 'Passwordless sudo is required for the dedicated demo VM.' >&2; exit 1; }
sudo env DEBIAN_FRONTEND=noninteractive apt-get update -q
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-venv python3-pip make git curl ca-certificates gnupg jq openssl rsync shellcheck poppler-utils
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: -r requirements.lock
printf 'Bootstrap complete. Run make preflight && make deploy\n'
