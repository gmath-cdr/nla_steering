#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

uv_bin="${UV:-uv}"
command -v "$uv_bin" >/dev/null || {
  echo "uv is required (install: https://docs.astral.sh/uv/)" >&2
  exit 1
}
"$uv_bin" sync --locked --extra analysis
.venv/bin/python run.py self-test

echo "bootstrap complete: locked analysis environment is ready in .venv"
echo "Rebuild tables and figures with: uv run --locked --extra analysis ./run.py reproduce"
echo "For GPU experiments: uv sync --locked --extra experiments"
