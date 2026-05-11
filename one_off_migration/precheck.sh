#!/usr/bin/env bash
# Phase A.5 pre-check (variable-safe via script file)
set -uo pipefail

echo "=== 1. nested profile 진짜 데이터 확인 ==="
for n in advisor_ops calendar_ops documentation finder implementation infrastructure installer_ops journal_ops mail_ops planning quality research; do
    echo "--- $n ---"
    ls ~/.hermes/profiles/profiles/$n/ 2>&1 | head -5
done

echo
echo "=== 2. advisor_ops / calendar_ops placeholder 오염 점검 ==="
echo "-- advisor_ops --"
ls -la ~/.hermes/profiles/advisor_ops/
echo "-- calendar_ops --"
ls -la ~/.hermes/profiles/calendar_ops/

echo
echo "=== 3. C 블록 destination 충돌 점검 ==="
for n in documentation finder implementation infrastructure installer_ops journal_ops mail_ops planning quality research; do
    if test -e ~/.hermes/profiles/$n; then
        echo "CONFLICT: $n"
    else
        echo "ok: $n"
    fi
done
