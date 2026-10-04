#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[[ -x .venv/bin/ansible-playbook ]] || { echo 'Run ./scripts/bootstrap.sh first.' >&2; exit 1; }
./scripts/preflight.sh
mkdir -p artifacts
export ANSIBLE_CONFIG="$PWD/ansible/ansible.cfg"
export ANSIBLE_NOCOLOR=1
export ANSIBLE_LOCAL_TEMP="$PWD/.state/ansible-tmp"
mkdir -p "$ANSIBLE_LOCAL_TEMP"
chmod 700 .state "$ANSIBLE_LOCAL_TEMP"
.venv/bin/ansible-playbook -i ansible/inventory.example.yml ansible/site.yml "$@" 2>&1 | tee artifacts/deploy.log
echo 'Deployment finished. Run make verify for independent acceptance checks.'
