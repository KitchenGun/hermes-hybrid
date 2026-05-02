#!/usr/bin/env python3
"""One-line-per-model summary of installed Ollama models."""
import json
import urllib.request

with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5) as r:
    d = json.load(r)
print(f"{'name':<35s} {'size_GB':>8s}  {'params':>8s}  {'quant':>10s}  {'family':>10s}")
print("-" * 80)
for m in sorted(d.get("models", []), key=lambda x: -x.get("size", 0)):
    size_gb = m.get("size", 0) / 1024 / 1024 / 1024
    det = m.get("details", {}) or {}
    print(
        f"{m['name']:<35s} {size_gb:>8.2f}  "
        f"{det.get('parameter_size','?'):>8s}  "
        f"{det.get('quantization_level','?'):>10s}  "
        f"{det.get('family','?'):>10s}"
    )
