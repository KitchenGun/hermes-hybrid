"""Print last N journal_ops sessions: tool-call sequence + timing.

Run inside WSL: python3 /mnt/e/hermes-hybrid/scripts/inspect_journal_sessions.py
"""
import json
import os
import sys

D = '/home/kang/.hermes/profiles/journal_ops/sessions/'

n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
for f in sorted(os.listdir(D), reverse=True)[:n]:
    p = D + f
    try:
        s = json.load(open(p))
        msgs = s['messages']
        tcs = sum(len(m.get('tool_calls') or []) for m in msgs)
        print(f'{f}: start={s["session_start"][-8:]} last={s["last_updated"][-8:]} msgs={len(msgs)} tools={tcs}')
        for i, m in enumerate(msgs):
            if m.get('tool_calls'):
                for tc in m['tool_calls']:
                    fn = tc.get('function', {}).get('name', '?')
                    print(f'  [{i}] {fn}')
            elif m['role'] == 'user':
                c = m.get('content', '')
                print(f'  [{i}] user: {str(c)[:80]}')
    except Exception as e:
        print(f, '->', e)
