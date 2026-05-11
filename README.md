# hermes-hybrid (Deprecated 2026-05-11)

이 저장소는 공식 [Nous Hermes Agent v0.13.0+](https://hermes-agent.nousresearch.com/)
의 **R0 18-profile roster** (default + 17 specialist + kk_job)로 일원화되었습니다.
운영은 `~/.hermes/profiles/`로 이동했고, 본 저장소의 자체 구현 코드는 retire/self-implementation
branch에서 `git rm` 되었습니다 (commit `99c3bc8`).

> History만 git에 보존. 2026-05-11 dry-run 조기 종료 + `retire/self-implementation` → `main`
> fast-forward merge 완료 (commit `a9f06b6`). Phase D cleanup (`_archive/`, `data/`, `.env.example`,
> `.agents/`) 도 함께 적용됨.

---

## 새 환경 (공식 Hermes Agent)

### Profile roster
- **`default`** (`~/.hermes/`) — Master Orchestrator (kanban-orchestrator skill 활성). Discord/Telegram gateway 부착.
- **17 specialist** (`~/.hermes/profiles/<name>/`, opencode-hermes-multiagent 17-agent 기반):
  - 🔍 Research: `finder` / `analyst` / `researcher`
  - 📐 Planning: `architect` / `planner`
  - 💻 Implementation: `coder` / `editor` (alias `hermes-editor`) / `fixer` / `refactorer`
  - ✅ Quality: `reviewer` / `tester` / `debugger` / `security`
  - 📚 Documentation: `documenter` / `commenter`
  - 🔧 Infrastructure: `devops` / `optimizer`
- **`kk_job`** — 사용자 도메인 main (구인 리서치 / 이력 관리 / 이력서·자소서). 절대 보존.

### Provider
- Active: `openai-codex` (ChatGPT Plus OAuth, `gpt-5.5`)
- credential_pool: `anthropic-oauth-2`, `anthropic-oauth-3` (필요 시 fallback)

### Memory
- `~/.hermes/memories/USER.md` (511 chars) — hermes-hybrid memos `user_feedback_style` 5 rows 압축
- `~/.hermes/memories/MEMORY.md` (2158 chars) — hermes-hybrid memos `generated_candidates` 28 rows 압축

### Kanban
- `~/.hermes/kanban.db` (공식 협업 보드)
- 멀티 에이전트 협업: orchestrator (default)가 `kanban_create --assignee <specialist>` 로 task 분배

---

## Migration scripts (one-off, history)

`one_off_migration/` 디렉터리에 1회용 마이그레이션 스크립트 보존:

| Script | 역할 |
|---|---|
| `migrate_opencode_agents_to_hermes_profiles.py` | opencode-hermes-multiagent `.md` → SOUL.md (frontmatter strip) |
| `migrate_memos_to_hermes_official.py` | hermes-hybrid `state.db` `memos` → MEMORY.md/USER.md (자수 제한 압축) |
| `precheck.sh` | 사고 후 nested 데이터 검증 |
| `block_c.sh` | 10 stopped profile 복구 |
| `block_ab.sh` | advisor_ops + calendar_ops gateway 정지 + 복구 |
| `archive_11_profiles.sh` | Phase A.5 archive (개별 mv + 가드, kk_job 보호) |
| `create_16_specialists.sh` | 16 specialist 일괄 생성 + opencode 적용 |
| `append_env.sh` | Phase B.1 Discord token + webhook URL append |
| `check_env_keys.sh` | `~/.hermes/.env` key 점검 |
| `phase_c_retire.sh` / `phase_c_retire.ps1` | hermes-hybrid 자체 코드 git rm + commit |

---

## Branch

- `main` / `retire/self-implementation` / `origin/main`: 모두 동일 commit으로 정렬됨 (2026-05-11 dry-run 조기 종료 시점 동기화).
- 적용된 commit:
  - `99c3bc8` — 자체 구현 제거 (Phase A–C)
  - `78757df` — README rewrite + `one_off_migration/` 추가
  - `a9f06b6` — Phase D cleanup (tracked: `_archive/`, `data/`, `.env.example`, `.agents/` 제거)
  - `1ee33be` — README dry-run 종료 + merge 완료 반영
  - 본 commit — Cleanup history 섹션 추가

---

## Cleanup history (2026-05-11)

Dry-run 조기 종료 후 main worktree 최종 정리. 본 저장소는 이후 **마이그레이션 history archive** 상태로 유지됨 (운영은 `~/.hermes/`로 일원화).

### Tracked 정리 (`a9f06b6`)
- `_archive/` — 11-profile .env/MEMORY 백업. ~/.hermes/profiles_archive/가 actual archive (README §복귀 참조).
- `data/` — bench/memory/processed_memory/source_manifests/job_factory. 호출 코드(자체 구현)는 `99c3bc8`에서 git rm됨. memory 컨텐츠는 이미 ~/.hermes/memories/에 import 완료.
- `.env.example` — hermes-hybrid 자체 구현 환경변수 템플릿 (공식 Hermes Agent는 ~/.hermes/.env 사용).
- `.agents/skills/` — `.claude/skills/`와 중복 (Codex 포팅 변종, 외부 도구 미사용).

### Untracked 정리 (.gitignore'd로 git status 미노출, 디스크에만 잔존했던 dead artifact)
- 운영 DB: `data/state.db`, `data/kanban.db`, `data/recurring_request_log.jsonl`, `data/growth_metrics.generated.yaml`, `data/pipelines.yaml`, `data/memory/ingest.log`
- 운영 로그: `logs/` (60+ files: `bot-*.log`, `run-*.log`, `*.err.log`, `experience/*.jsonl`), `bot_stderr.log`, `bot_stdout.log`
- 백업 (.bak/.pre_v42 패턴): `data/*.pre_v42_20260509T144427Z` (4), `data/job_factory/score_matrix.bak.20260503-noise.json`, `data/benchmarks/claude_family_2026-05-03*.log` (3), `secrets/gmail_kitchen_token.json.bak.20260504`, `.env.bak`, `.env.bak.20260501`
- Build artifacts: `.venv/` (Windows venv), `.venv-linux/` (WSL venv), `tests/__pycache__/`, `scripts/__pycache__/`, `.pytest_cache/`
- 자체 구현 source dirs (retire git rm 후 .gitignore'd로 잔존): `profiles/`, `src/`, `state/`, `_archive/`
- 벤치 결과: `data/benchmarks/*.json` (8)
- 활성 `.env` — DISCORD_BOT_TOKEN, MASTER_*, OLLAMA_* 등 자체 구현용. 운영 token은 이미 ~/.hermes/.env에 import 완료.

### 보존 (KEEP)
- **Git 운영**: `.gitignore`, `.gitattributes`, `README.md`
- **Claude Code 도구**: `.claude/` (settings + commands + skills)
- **Migration history**: `one_off_migration/` (11 scripts — README §Migration scripts 명시)
- **OAuth credentials**: `secrets/` — ~/.hermes/ grep 결과 미참조이나 재발급 비용 회피 위해 보존. `.gitignore`'d이므로 repo cleanliness 무관.
  - `google_oauth_client.json` / `google_calendar_token.json` / `gmail_kk_token.json` / `gmail_kitchen_token.json`

### 권한 시스템 ping-pong (참고)
정리 과정에서 Claude Code 권한 시스템이 destructive action을 여러 번 차단 — `state.db/kanban.db backups` 자동 inference 거부, `git push origin main` 명시 미인가 거부, `Remove-Item` recursive 거부. 모두 사용자 명시 확인(AskUserQuestion) 후 진행. 결과적으로 OAuth credential 같은 sensitive 자료가 보존되는 안전망 역할.

---

## 복귀 (마이그레이션 reverse)

- `~/.hermes/profiles_archive/` 11개 → `~/.hermes/profiles/` mv 복원
- `~/.hermes/profiles_placeholder/{kk_job,advisor_ops,calendar_ops}.placeholder` 보존 (사고 복구 backup)
- `~/.hermes/SOUL.md.backup-pre-r0` (537 bytes) → `~/.hermes/SOUL.md` 복원
- `~/.hermes/.env.backup-pre-r0-b1` → `~/.hermes/.env` 복원
- main 복원: `git reset --hard 6dc99a5` (pre-retire main) 또는 그 이전 commit으로 reset

---

## §0 절대 금지 (이후에도 유효)

- `~/.hermes/auth.json` / `~/.hermes/config.yaml`의 provider·credential_pool 변경 X
- `hermes auth login`, `hermes model set` 등 provider 명령 호출 X
- 새 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 추가 X (Discord/Telegram token만)
- **`kk_job` profile 디렉터리·.env·SOUL.md·kanban.db 절대 수정·이동 금지**
- `~/.config/opencode/` 디렉터리 만들지 X

---

## 참조

- Plan: `C:\Users\kang9\.claude\plans\https-hermes-agent-nousresearch-com-docs-glistening-melody.md`
- 사용자 메모리: `C:\Users\kang9\.claude\projects\E--hermes-hybrid\memory\project_official_hermes_migration.md`
- 공식 docs: <https://hermes-agent.nousresearch.com/docs/>
- 참조 (profile 구조): <https://github.com/1ilkhamov/opencode-hermes-multiagent>
