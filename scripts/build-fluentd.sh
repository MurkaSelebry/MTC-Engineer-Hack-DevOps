#!/usr/bin/env bash
# Run on the target Ubuntu amd64 Kubernetes node. No registry push is required.
set -euo pipefail
root_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $(uname -m) != x86_64 ]]; then
  echo 'Build this pinned amd64 image on an x86_64 node.' >&2
  exit 1
fi
if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi
command -v podman >/dev/null
command -v ctr >/dev/null
command -v flock >/dev/null
# Serialize imports/tagging when deployment and an operator trigger a build together.
exec 9>/var/lock/mtc-hack-fluentd-build.lock
flock -w 1800 9
image=localhost/mtc-hack/fluentd:1.0.0
# Host networking avoids Podman bridge/firewall interaction with Calico during bootstrap.
podman build --layers --network=host --platform linux/amd64 --pull=missing --tag "$image" "$root_dir/images/fluentd"
# Validate the actual image and its installed plugins before making it available.
podman run --rm --pull=never --network=none \
  -v "$root_dir/kubernetes/logging/fluent.conf:/fluentd/etc/fluent.conf:ro" \
  "$image" --dry-run -c /fluentd/etc/fluent.conf
archive=$(mktemp /var/tmp/mtc-fluentd.XXXXXX.tar)
trap 'rm -f "$archive"' EXIT
podman save --format docker-archive --output "$archive" "$image"
ctr -n k8s.io images import "$archive"
# CRI normalizes unqualified names to docker.io; tag the imported image explicitly.
ctr -n k8s.io images tag --force "$image" docker.io/mtc-hack/fluentd:1.0.0
ctr -n k8s.io images ls -q | grep -Fx docker.io/mtc-hack/fluentd:1.0.0
# Expose the content identity to the deployer for stable, content-based rollouts.
image_id=$(podman image inspect --format '{{.Id}}' "$image")
image_id="sha256:${image_id#sha256:}"
mkdir -p "$root_dir/.state"
state_file=$(mktemp "$root_dir/.state/fluentd-image-id.XXXXXX")
printf '%s\n' "$image_id" > "$state_file"
chmod 0644 "$state_file"
mv -f "$state_file" "$root_dir/.state/fluentd-image-id"
