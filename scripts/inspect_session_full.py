"""Dump every message of a session in detail (role, tool name, args, content)."""
import json
import sys

p = sys.argv[1]
d = json.load(open(p))
print(f"== {p}")
print(f"start={d['session_start']}  last={d['last_updated']}")
print(f"tools_decl ({len(d.get('tools', []))}): {sorted(t.get('function',{}).get('name','?') for t in d.get('tools',[]))}")
print('-' * 60)
for i, m in enumerate(d['messages']):
    role = m.get('role', '?')
    if m.get('tool_calls'):
        for tc in m['tool_calls']:
            fn = tc.get('function', {})
            print(f'[{i}] {role} -> tool={fn.get("name","?")}  args={str(fn.get("arguments",""))[:200]}')
    elif role == 'tool':
        print(f'[{i}] {role}: {str(m.get("content",""))[:300]}')
    elif role == 'assistant' and m.get('content'):
        c = m['content']
        if isinstance(c, str) and c.strip():
            print(f'[{i}] {role}: {c[:300]}')
    elif role == 'user':
        print(f'[{i}] {role}: {str(m.get("content",""))[:300]}')
