#!/usr/bin/env bash
# Install the STAG skill pack for Claude Code, Codex and Cursor from anywhere.
#
#   curl -fsSL https://raw.githubusercontent.com/mridulaEA/stag/main/scripts/install-skills.sh | bash
#   # or, from a clone:
#   scripts/install-skills.sh [--target all|claude|codex|cursor] [--scope user|project] [--ref main]
#
# No Python dependencies are required beyond a Python 3 interpreter; the installer is stdlib-only.
set -euo pipefail

TARGET="all"
SCOPE="user"
REF="main"
REPO_URL="https://github.com/mridulaEA/stag.git"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="$2"; shift 2 ;;
    --scope) SCOPE="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -n "$SCRIPT_DIR" && -d "$SCRIPT_DIR/../skills" ]]; then
  SRC="$(cd "$SCRIPT_DIR/.." && pwd)"
  CLEANUP=""
else
  SRC="$(mktemp -d)"
  CLEANUP="$SRC"
  echo "Fetching STAG ($REF) ..."
  git clone --depth 1 --branch "$REF" --quiet "$REPO_URL" "$SRC"
fi

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null || { echo "python3 is required" >&2; exit 1; }

PYTHONPATH="$SRC" "$PYTHON" -m stag.skills --skills-dir "$SRC/skills" install --target "$TARGET" --scope "$SCOPE" --force

if [[ -n "$CLEANUP" ]]; then rm -rf "$CLEANUP"; fi

cat <<MSG

STAG skills installed (target=$TARGET, scope=$SCOPE).
  Claude Code : ~/.claude/skills/stag-*          (or: claude plugin marketplace add mridulaEA/stag)
  Codex       : ~/.agents/skills/stag-*          (type \$ or /skills)
  Cursor      : ~/.cursor/skills/stag-*          (type / in Agent chat)
Run 'python -m stag.skills list' from a clone to see the catalog.
MSG
