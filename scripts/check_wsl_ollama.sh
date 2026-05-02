#!/bin/bash
# Probe Ollama reachability from WSL.
# Tries (in order): localhost (mirrored networking), default-gateway IP, /etc/resolv.conf nameserver.
set -u

probe() {
  local label="$1" host="$2"
  if [ -z "$host" ]; then
    echo "$label: <empty>"
    return 1
  fi
  if curl -s -m 3 "http://$host:11434/api/tags" >/dev/null; then
    echo "$label: $host -> OK"
    return 0
  else
    echo "$label: $host -> FAIL"
    return 1
  fi
}

echo "--- WSL Ollama probe ---"
probe "localhost" "localhost"
GW=$(ip route show default | awk '/default/{print $3; exit}')
probe "default-gw" "$GW"
NS=$(grep -m1 '^nameserver' /etc/resolv.conf | awk '{print $2}')
probe "resolv-ns" "$NS"
echo "--- end ---"
