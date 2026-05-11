#!/usr/bin/env bash
# Phase A.5-3: 16 specialist profile 일괄 생성 + opencode .md → SOUL.md 적용
# - finder 는 Phase A.2 에서 이미 생성됨
# - default SOUL.md 는 backup 후 opencode hermes.md 본문으로 교체
# - kk_job 절대 안 건드림
set -euo pipefail

cd /tmp

echo "=== preflight ==="
# /tmp/ohma 확보 (이미 있으면 그대로, 없으면 clone)
if [ -d /tmp/ohma ]; then
    echo "  /tmp/ohma already exists"
elif [ -d /tmp/ohma-test ]; then
    mv /tmp/ohma-test /tmp/ohma
    echo "  renamed /tmp/ohma-test → /tmp/ohma"
else
    git clone --depth 1 https://github.com/1ilkhamov/opencode-hermes-multiagent /tmp/ohma 2>&1 | tail -3
fi

# default SOUL.md backup (kk_job 무관)
if [ ! -f ~/.hermes/SOUL.md.backup-pre-r0 ]; then
    cp -p ~/.hermes/SOUL.md ~/.hermes/SOUL.md.backup-pre-r0
    echo "  backed up ~/.hermes/SOUL.md → ~/.hermes/SOUL.md.backup-pre-r0"
else
    echo "  backup already exists (skipping re-backup)"
fi

echo
echo "=== create 16 specialist profiles (finder excluded — already exists) ==="
# 개별 호출 (변수 없음, escape 사고 차단)
hermes profile create analyst     --clone && echo "  created analyst"
hermes profile create researcher  --clone && echo "  created researcher"
hermes profile create architect   --clone && echo "  created architect"
hermes profile create planner     --clone && echo "  created planner"
hermes profile create coder       --clone && echo "  created coder"
hermes profile create editor      --clone && echo "  created editor"
hermes profile create fixer       --clone && echo "  created fixer"
hermes profile create refactorer  --clone && echo "  created refactorer"
hermes profile create reviewer    --clone && echo "  created reviewer"
hermes profile create tester      --clone && echo "  created tester"
hermes profile create debugger    --clone && echo "  created debugger"
hermes profile create security    --clone && echo "  created security"
hermes profile create documenter  --clone && echo "  created documenter"
hermes profile create commenter   --clone && echo "  created commenter"
hermes profile create devops      --clone && echo "  created devops"
hermes profile create optimizer   --clone && echo "  created optimizer"

echo
echo "=== apply opencode .md → SOUL.md (frontmatter strip, all 18) ==="
SCRIPT_DIR=/mnt/e/hermes-hybrid/.claude/worktrees/quizzical-greider-400b85/one_off_migration
python3 "$SCRIPT_DIR/migrate_opencode_agents_to_hermes_profiles.py" \
    --src /tmp/ohma \
    --dst ~/.hermes/profiles \
    --default-dst ~/.hermes/SOUL.md

echo
echo "=== verify ==="
echo "-- profile count --"
ls ~/.hermes/profiles/ | wc -l
echo "-- 17 specialist + kk_job present --"
ls ~/.hermes/profiles/
echo
echo "-- hermes profile list --"
hermes profile list 2>&1 | head -25
echo
echo "-- kk_job kanban.db unchanged --"
stat -c '%n size=%s mtime=%y' ~/.hermes/profiles/kk_job/kanban.db
echo
echo "-- ~/.hermes/SOUL.md updated, backup safe --"
wc -c ~/.hermes/SOUL.md ~/.hermes/SOUL.md.backup-pre-r0
