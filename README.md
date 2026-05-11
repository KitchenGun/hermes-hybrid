# hermes-hybrid (Deprecated 2026-05-11)

이 저장소는 공식 [Nous Hermes Agent v0.13.0+](https://hermes-agent.nousresearch.com/)
의 **R0 18-profile roster** (default + 17 specialist + kk_job)로 일원화되었습니다.
운영은 `~/.hermes/profiles/`로 이동했고, 본 저장소의 자체 구현 코드는 retire/self-implementation
branch에서 `git rm` 되었습니다 (commit `99c3bc8`).

> History만 git에 보존. `main` branch는 1주 dry-run 기간(2026-05-11 ~) 동안 유지.
> 사용자 결정 F에 따라 1주 후 `retire/self-implementation` → `main` 머지 결정.

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

- `main`: 1주 dry-run 기간 hermes-hybrid 운영 유지 (2026-05-11 ~)
- `retire/self-implementation`: 자체 구현 제거 commit (`99c3bc8`). 1주 후 main 머지 결정.

---

## 복귀 (마이그레이션 reverse)

- `~/.hermes/profiles_archive/` 11개 → `~/.hermes/profiles/` mv 복원
- `~/.hermes/profiles_placeholder/{kk_job,advisor_ops,calendar_ops}.placeholder` 보존 (사고 복구 backup)
- `~/.hermes/SOUL.md.backup-pre-r0` (537 bytes) → `~/.hermes/SOUL.md` 복원
- `~/.hermes/.env.backup-pre-r0-b1` → `~/.hermes/.env` 복원
- `git checkout main` (main branch 그대로 보존)

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
