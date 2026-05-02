#!/usr/bin/env python3
"""Print Ollama model capabilities for every installed model.

`capabilities` includes things like 'completion', 'tools', 'vision',
'embedding'. For our use, 'tools' (= function calling) is the gating
property for journal_ops / calendar_ops profiles — a model without it
will hallucinate tool calls instead of invoking them.
"""
import json
import urllib.request

with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5) as r:
    d = json.load(r)

print(f"{'name':<36s} {'params':>8s}  {'context':>8s}  {'capabilities'}")
print("-" * 90)
for m in sorted(d.get("models", []), key=lambda x: x["name"]):
    name = m["name"]
    try:
        body = json.dumps({"name": name}).encode()
        req = urllib.request.Request(
            "http://localhost:11434/api/show",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as rr:
            info = json.load(rr)
    except Exception as e:  # noqa: BLE001
        print(f"{name:<36s} ERROR: {e}")
        continue
    det = info.get("details", {}) or {}
    mi = info.get("model_info", {}) or {}
    family = det.get("family", "?")
    param = det.get("parameter_size", "?")
    # Find context_length in model_info — key is family-prefixed, e.g. "gemma3.context_length"
    ctx = "?"
    for k, v in mi.items():
        if k.endswith("context_length"):
            ctx = str(v)
            break
    caps = ",".join(info.get("capabilities", []))
    print(f"{name:<36s} {param:>8s}  {ctx:>8s}  {caps}")
