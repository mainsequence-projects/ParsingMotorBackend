#!/usr/bin/env bash
set -euo pipefail
export AWS_REGION=eu-central-1 AWS_DEFAULT_REGION=eu-central-1
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# prefer hack venv
if [[ -f /workspace/hack/venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source /workspace/hack/venv/bin/activate
fi
exec python3 "$ROOT/deploy/deploy.py"
