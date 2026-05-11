#!/usr/bin/env bash
# Phase A.5 step 1 (개정판): 11 profile archive
# - kk_job 절대 보존 (§0)
# - 변수 사용 X, 개별 mv 11줄 + test -d 가드
# - advisor_ops / calendar_ops gateway 정지 우선
set -euo pipefail

echo "=== preflight ==="
mkdir -p ~/.hermes/profiles_archive
echo "  profiles_archive ready"

echo
echo "=== stop running gateways (advisor_ops, calendar_ops) ==="
systemctl --user stop hermes-gateway-advisor_ops.service
echo "  advisor_ops gateway stopped"
systemctl --user stop hermes-gateway-calendar_ops.service
echo "  calendar_ops gateway stopped"

echo
echo "=== archive 11 profiles (kk_job NOT touched) ==="
test -d ~/.hermes/profiles/advisor_ops      && mv ~/.hermes/profiles/advisor_ops      ~/.hermes/profiles_archive/advisor_ops      && echo "  archived advisor_ops"
test -d ~/.hermes/profiles/calendar_ops     && mv ~/.hermes/profiles/calendar_ops     ~/.hermes/profiles_archive/calendar_ops     && echo "  archived calendar_ops"
test -d ~/.hermes/profiles/documentation    && mv ~/.hermes/profiles/documentation    ~/.hermes/profiles_archive/documentation    && echo "  archived documentation"
test -d ~/.hermes/profiles/implementation   && mv ~/.hermes/profiles/implementation   ~/.hermes/profiles_archive/implementation   && echo "  archived implementation"
test -d ~/.hermes/profiles/infrastructure   && mv ~/.hermes/profiles/infrastructure   ~/.hermes/profiles_archive/infrastructure   && echo "  archived infrastructure"
test -d ~/.hermes/profiles/installer_ops    && mv ~/.hermes/profiles/installer_ops    ~/.hermes/profiles_archive/installer_ops    && echo "  archived installer_ops"
test -d ~/.hermes/profiles/journal_ops      && mv ~/.hermes/profiles/journal_ops      ~/.hermes/profiles_archive/journal_ops      && echo "  archived journal_ops"
test -d ~/.hermes/profiles/mail_ops         && mv ~/.hermes/profiles/mail_ops         ~/.hermes/profiles_archive/mail_ops         && echo "  archived mail_ops"
test -d ~/.hermes/profiles/planning         && mv ~/.hermes/profiles/planning         ~/.hermes/profiles_archive/planning         && echo "  archived planning"
test -d ~/.hermes/profiles/quality          && mv ~/.hermes/profiles/quality          ~/.hermes/profiles_archive/quality          && echo "  archived quality"
test -d ~/.hermes/profiles/research         && mv ~/.hermes/profiles/research         ~/.hermes/profiles_archive/research         && echo "  archived research"

echo
echo "=== kk_job protection guard ==="
test ! -e ~/.hermes/profiles_archive/kk_job || { echo "GUARD VIOLATION: kk_job in archive — abort"; exit 1; }
test -d ~/.hermes/profiles/kk_job || { echo "GUARD VIOLATION: kk_job missing — abort"; exit 1; }
echo "  kk_job preserved at original location ✓"

echo
echo "=== verify ==="
echo "-- ~/.hermes/profiles/ --"
ls ~/.hermes/profiles/
echo
echo "-- ~/.hermes/profiles_archive/ --"
ls ~/.hermes/profiles_archive/
echo
echo "-- hermes profile list --"
hermes profile list 2>&1 | head -10
