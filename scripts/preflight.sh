#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
python3 scripts/preflight.py "$@"
sudo -n true
for url in https://pkgs.k8s.io/core:/stable:/v1.35/deb/Release https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz.sha256sum https://raw.githubusercontent.com/projectcalico/calico/v3.32.2/manifests/custom-resources.yaml; do
  curl --fail --silent --show-error --connect-timeout 10 --max-time 30 --retry 2 "$url" -o /dev/null
done
echo 'Preflight passed: OS, architecture, resources, network CIDRs, sudo and artifact endpoints.'
