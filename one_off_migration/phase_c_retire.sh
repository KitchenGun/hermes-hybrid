#!/usr/bin/env bash
# Phase C: hermes-hybrid 폐기 (retire worktree에서)
# - retire/self-implementation branch에 git rm + commit
# - main branch는 그대로 유지 (사용자 결정 F = 1주 dry-run)
set -euo pipefail

WT=/mnt/e/hermes-hybrid/hermes-hybrid-retire
cd "$WT"
echo "=== cwd: $(pwd) ==="
echo "=== branch: $(git branch --show-current) ==="

echo
echo "=== step 1: git rm self-implementation source code ==="
git rm -r src tests scripts docs config 2>&1 | tail -5
echo "  src/tests/scripts/docs/config removed"

echo
echo "=== step 2: git rm agents (27 SKILL.md) ==="
git rm -r agents 2>&1 | tail -3
echo "  agents/ removed"

echo
echo "=== step 3: git rm jobs/memory/skills/profiles (rest of self-impl) ==="
git rm -r jobs memory skills profiles 2>&1 | tail -3 || true
echo "  jobs/memory/skills/profiles removed"

echo
echo "=== step 4: git rm root-level self-impl files ==="
git rm pyproject.toml run_all.bat 2>&1 | tail -3 || true
git rm discord_conflict_notifier.py 2>&1 | tail -3 || true
git rm start.bat start.ps1 2>&1 | tail -3 || true
git rm uv.lock 2>&1 | tail -3 || true
git rm ARCHITECTURE.md 2>&1 | tail -3 || true
echo "  root-level self-impl files removed"

echo
echo "=== step 5: archive data/ + remove data subdirs ==="
# data/processed_memory data/source_manifests data/external_memory data/ingest_staging
# data/job_factory data/benchmarks → 공식 docs 미명시 → 삭제
git rm -r data/processed_memory 2>&1 | tail -1 || true
git rm -r data/source_manifests 2>&1 | tail -1 || true
git rm -r data/external_memory 2>&1 | tail -1 || true
git rm -r data/ingest_staging 2>&1 | tail -1 || true
git rm -r data/job_factory 2>&1 | tail -1 || true
git rm -r data/benchmarks 2>&1 | tail -1 || true
# 남은 data/* 파일들은 archive로 (전체 mv 대신 git mv 각각)
git rm data/*.* 2>&1 | tail -3 || true
echo "  data/ subdirs and files removed"

echo
echo "=== step 6: README.md replace ==="
cat > README.md <<'EOF'
# hermes-hybrid (Deprecated 2026-05-11)

이 저장소는 공식 [Nous Hermes Agent v0.13.0+](https://hermes-agent.nousresearch.com/)
에서 멀티 에이전트 profile roster (default + 17 specialist + kk_job)로 일원화됨.
운영은 `~/.hermes/profiles/`로 이동.

History만 git에 보존. main branch는 1주 dry-run 기간 동안 유지.

## Migration scripts (one-off, history)
- `one_off_migration/migrate_opencode_agents_to_hermes_profiles.py`
- `one_off_migration/migrate_memos_to_hermes_official.py`
- `one_off_migration/precheck.sh` / `block_c.sh` / `block_ab.sh`
- `one_off_migration/archive_11_profiles.sh` / `create_16_specialists.sh`
- `one_off_migration/append_env.sh` / `check_env_keys.sh`

## Branch
- `main`: 1주 dry-run 기간 hermes-hybrid 운영 유지 (2026-05-11 ~)
- `retire/self-implementation`: 자체 구현 제거 commit. 1주 후 main 머지 결정.

See plan: `C:\Users\kang9\.claude\plans\https-hermes-agent-nousresearch-com-docs-glistening-melody.md`
EOF
git add README.md
echo "  README.md replaced"

echo
echo "=== step 7: git status (pre-commit) ==="
git status --short | head -30

echo
echo "=== step 8: git commit ==="
git commit -m "chore: retire self-implementation, migrate to official Hermes Agent v0.13.0 with 18-profile roster

- Discord/Telegram gateway → ~/.hermes/.env (default profile)
- Memory → ~/.hermes/memories/MEMORY.md (2158 chars) + USER.md (511 chars)
- Kanban → ~/.hermes/kanban.db (hermes-hybrid kanban.db all tables empty, no data migration needed)
- Skills → kanban-orchestrator/kanban-worker builtin, opencode 17-agent .md as SOUL.md
- Curator/Promoter/Reflection: removed, replaced by hermes curator + autonomous skill creation
- A/B experiment, sha16 privacy, daily token cap, per-user in-flight: removed (not in official docs)
- 9 schtasks: TO BE removed in Phase D (manual)
- W marker blocks (W3-W12): removed (not in official docs)

Profile roster (R0):
- default (~/.hermes/) — orchestrator, kanban-orchestrator skill
- finder/analyst/researcher (Research)
- architect/planner (Planning)
- coder/editor/fixer/refactorer (Implementation)
- reviewer/tester/debugger/security (Quality)
- documenter/commenter (Documentation)
- devops/optimizer (Infrastructure)
- kk_job (사용자 도메인, preserved)

= default + 17 specialist + kk_job = 19 profile

Note: main branch retained for 1-week dry-run (decision F). main merge decision after D+7." 2>&1 | tail -10

echo
echo "=== step 9: final verify ==="
git log --oneline -3
echo "--- branch ---"
git branch --show-current
