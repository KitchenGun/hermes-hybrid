#!/usr/bin/env bash
# Rollback named marker blocks. Pass --names W4,W6a,W6b,W10,W11_curator,W11_promoter,W12,W3
# Default: all
#
# Each block is delimited by `# --- <NAME> ... ---` ... `# --- end ---`.

set -euo pipefail

NAMES="W4,W6a,W6b,W10,W11_curator,W11_promoter,W12,W3"
ALL=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --names) NAMES="$2"; shift 2 ;;
    --all) ALL=true; shift ;;
    *) shift ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

for name in ${NAMES//,/ }; do
  case "$name" in
    W4) marker="# --- W4 SOUL injection ---" ; files="src/orchestrator/hermes_master.py" ;;
    W6a) marker="# --- W6a MCP tool registry extensions ---" ; files="src/mcp/server.py" ;;
    W6b) marker="# --- W6b MCP tool dispatch extensions ---" ; files="src/mcp/server.py" ;;
    W10) marker="# --- W10 recurring-request detector ---" ; files="src/orchestrator/hermes_master.py" ;;
    W11_curator) marker="# --- W11 self-review candidates glob ---" ; files="src/jobs/curator_job.py" ;;
    W11_promoter) marker="# --- W11 self-review drafts scan ---" ; files="src/jobs/skill_promoter.py" ;;
    W12) marker="# --- W12 delegation bias ---" ; files="src/orchestrator/hermes_master.py" ;;
    W3) marker="# --- W3 growth-loop timer extensions ---" ; files="src/cli/timer_handlers/windows.py src/cli/timer_handlers/linux.py src/cli/timer_handlers/darwin.py" ;;
    *) echo "unknown: $name" ; continue ;;
  esac
  for file in $files; do
    [ -f "$file" ] || continue
    python3 - "$file" "$marker" <<'PY'
import sys
path = sys.argv[1]
marker = sys.argv[2]
end = "# --- end ---"
text = open(path, "r", encoding="utf-8").read()
if marker not in text:
    print(f"  - {path}: {marker} absent")
    sys.exit(0)
start = text.index(marker)
# walk back to the indent
line_start = text.rfind("\n", 0, start) + 1
end_idx = text.index(end, start) + len(end)
end_nl = text.find("\n", end_idx)
if end_nl == -1:
    end_nl = end_idx
new = text[:line_start] + text[end_nl + 1 :]
open(path, "w", encoding="utf-8").write(new)
print(f"  + {path}: removed {marker}")
PY
  done
done

echo "Done."
