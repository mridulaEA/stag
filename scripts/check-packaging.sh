#!/usr/bin/env bash
# Build a wheel, install it into a clean virtualenv *outside* the checkout and verify that the
# skill and crew catalogs are discoverable from the installed package (review finding R28).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

python -m pip wheel --no-deps -w "$WORK/dist" "$ROOT" >/dev/null
python -m venv "$WORK/venv"
# shellcheck disable=SC1091
source "$WORK/venv/bin/activate"
pip install -q "$WORK"/dist/stag-*.whl pydantic pydantic-settings pyyaml structlog

cd "$WORK"   # not the checkout: the package must find its own catalogs
python - <<'PY'
import json, subprocess, sys
from stag._resources import asset_source
from stag.skills import discover_skills, SKILLS_DIR
from stag.crews import discover_crews, CREWS_DIR
assert asset_source() == "package", asset_source()
skills = discover_skills(SKILLS_DIR); crews = discover_crews(CREWS_DIR)
print(f"installed package: {len(skills)} skills, {len(crews)} crews from {asset_source()}")
assert len(skills) >= 21, "skills missing from installed package"
assert len(crews) >= 8, "crews missing from installed package"
PY
python -m stag.skills validate | tail -1
python -m stag.crews validate | tail -1
python -m stag.crews run-all --auto-approve >/dev/null && echo "installed crews run end-to-end"
