# Phase C: hermes-hybrid 폐기 (retire worktree, PowerShell version)
$ErrorActionPreference = 'Continue'
$wt = "E:\hermes-hybrid\hermes-hybrid-retire"

Write-Host "=== cwd: $wt ==="
Write-Host "=== branch: $(git -C $wt branch --show-current) ==="
Write-Host ""

Write-Host "=== step 1-4: git rm self-implementation ==="
git -C $wt rm -r src tests scripts docs config 2>&1 | Select-Object -Last 5
git -C $wt rm -r agents 2>&1 | Select-Object -Last 3
git -C $wt rm -r jobs memory skills profiles 2>&1 | Select-Object -Last 3
git -C $wt rm pyproject.toml run_all.bat discord_conflict_notifier.py start.bat start.ps1 uv.lock ARCHITECTURE.md 2>&1 | Select-Object -Last 3
Write-Host ""

Write-Host "=== step 5: data/ subdirs ==="
git -C $wt rm -r data/processed_memory data/source_manifests data/external_memory data/ingest_staging data/job_factory data/benchmarks 2>&1 | Select-Object -Last 5
# remaining data/* tracked files
git -C $wt rm data/state.db data/kanban.db data/external_memory.gitkeep -- 2>$null
git -C $wt rm -r logs 2>&1 | Select-Object -Last 3
Write-Host ""

Write-Host "=== step 6: README.md replace ==="
$readme = @'
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
'@
Set-Content -Path "$wt\README.md" -Value $readme -Encoding utf8
git -C $wt add README.md
Write-Host "  README.md replaced"
Write-Host ""

Write-Host "=== step 7: status ==="
git -C $wt status --short | Select-Object -First 30
Write-Host ""

Write-Host "=== step 8: commit ==="
$msg = @'
chore: retire self-implementation, migrate to official Hermes Agent v0.13.0 with 18-profile roster

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

Note: main branch retained for 1-week dry-run (decision F). main merge decision after D+7.
'@
git -C $wt commit -m $msg 2>&1 | Select-Object -Last 10
Write-Host ""

Write-Host "=== step 9: verify ==="
git -C $wt log --oneline -3
Write-Host "--- branch ---"
git -C $wt branch --show-current
