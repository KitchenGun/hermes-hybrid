#!/usr/bin/env python3
"""Print a one-screen summary of the most recent Hermes session.

Lists every message with role/tool_call/content-preview so we can see
whether the agent actually invoked a tool (e.g. sheets_append) or just
hallucinated a "✅ saved"-style reply without doing the side effect.
"""
import json
import os
import sys
from pathlib import Path

home = Path(os.path.expanduser("~"))
profiles_root = home / ".hermes" / "profiles"
sessions: list[Path] = []
# Every per-profile sessions dir + the global sessions dir.
for prof_dir in profiles_root.glob("*/sessions"):
    sessions.extend(prof_dir.glob("session_*.json"))
sessions.extend((home / ".hermes" / "sessions").glob("session_*.json"))
if not sessions:
    sys.exit("no sessions found")
sessions.sort(key=lambda p: p.stat().st_mtime)
path = sessions[-1]
print(f"file: {path}")
with path.open() as f:
    d = json.load(f)

print(f"session_id: {d.get('session_id')}")
print(f"model:      {d.get('model')}")
print(f"messages:   {len(d.get('messages', []))}")
print(f"tools_decl: {len(d.get('tools', []) or [])}")
print("-" * 60)
for i, m in enumerate(d.get("messages", []) or []):
    role = m.get("role")
    tcs = m.get("tool_calls") or []
    content = m.get("content")
    cs = (
        content[:240].replace("\n", " ⏎ ")
        if isinstance(content, str)
        else (json.dumps(content)[:240] if content else "")
    )
    print(f"#{i:02d} role={role:<10s} tool_calls={len(tcs)}")
    if cs:
        print(f"     content: {cs}")
    for tc in tcs:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        args = str(fn.get("arguments", ""))[:300].replace("\n", " ⏎ ")
        print(f"     -> tool: {fn.get('name')}  args: {args}")
