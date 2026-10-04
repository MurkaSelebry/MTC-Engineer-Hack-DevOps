#!/usr/bin/env bash
# Static checks require Python 3 + PyYAML; optional local tools are explicit skips.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ -x .venv/bin/python3 ]]; then
  export PATH="$PWD/.venv/bin:$PATH"
fi
while IFS= read -r -d '' file; do
  bash -n "$file"
done < <(find scripts -type f -name '*.sh' -print0)
python3 - <<'PY'
import ast
from pathlib import Path
try:
    import yaml
except ImportError:
    raise SystemExit('PyYAML is required for lint: python3 -m pip install PyYAML==6.0.3')
roots = [Path(x) for x in ('scripts', 'tests', 'kubernetes', 'helm', 'observability', '.github')]
files = [file for root in roots if root.exists() for file in root.rglob('*') if file.is_file()]
for file in files:
    if file.suffix == '.py':
        ast.parse(file.read_text(), filename=str(file))
    if file.suffix in ('.yaml', '.yml'):
        list(yaml.safe_load_all(file.read_text()))
print('Python syntax and YAML parsing passed')
PY
python3 -m unittest discover -s tests -p 'test_*.py'
if command -v shellcheck >/dev/null 2>&1; then
  find scripts -type f -name '*.sh' -print0 | xargs -0 shellcheck
else
  echo 'SKIP: shellcheck is not installed (CI installs it)'
fi
if command -v kubectl >/dev/null 2>&1; then
  while IFS= read -r -d '' file; do
    kubectl kustomize "$(dirname "$file")" >/dev/null
  done < <(find kubernetes -type f -name kustomization.yaml -print0)
else
  echo 'SKIP: kubectl kustomize is not installed (CI installs it)'
fi
