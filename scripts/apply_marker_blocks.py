"""P0c.3 / P0c.5b — Idempotent marker-block insertion.

Inserts named marker blocks into 7 existing source files. Each block is
delimited by `# --- W{N} <description> ---` ... `# --- end ---` and honors
HERMES_DISABLE_GROWTH_BLOCKS=true short-circuit.

Usage:
    python scripts/apply_marker_blocks.py --dry-run                # show diff
    python scripts/apply_marker_blocks.py --apply                  # all (W4/W6/W10/W11/W12)
    python scripts/apply_marker_blocks.py --apply --timers-only    # only W3 timers
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
END = "# --- end ---"


def _has_block(text: str, marker_start: str) -> bool:
    return marker_start in text


def _insert_after_pattern(text: str, pattern: str, block: str) -> str | None:
    """Insert `block` immediately after the first match of `pattern` (regex)."""
    m = re.search(pattern, text, re.MULTILINE)
    if not m:
        return None
    insert_at = m.end()
    while insert_at < len(text) and text[insert_at] == "\n":
        insert_at += 1
    return text[:insert_at] + block + "\n" + text[insert_at:]


# ---- W4 SOUL injection ---------------------------------------------------

W4_BLOCK = """\
        # --- W4 SOUL injection ---
        if not __import__("os").environ.get("HERMES_DISABLE_GROWTH_BLOCKS"):
            try:
                from src.orchestrator.soul_loader_generated import compose_soul_block
                soul = compose_soul_block()
                if soul:
                    parts.append(soul)
            except Exception as _w4_err:  # noqa: BLE001
                log.warning("w4.soul_inject_failed", err=str(_w4_err))
        # --- end ---"""


def patch_hermes_master(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    # W4 — _compose_prompt: insert after `parts: list[str] = [_SYSTEM_PROMPT]`
    if not _has_block(text, "# --- W4 SOUL injection ---"):
        new = _insert_after_pattern(
            text,
            r"^        parts: list\[str\] = \[_SYSTEM_PROMPT\]\s*$",
            W4_BLOCK,
        )
        if new is not None:
            text = new
            notes.append("W4 inserted")
        else:
            notes.append("W4 skipped (anchor not found)")

    # W10 — _dispatch_master: insert immediately after `prompt = self._compose_prompt(...)`
    W10_BLOCK = """\
        # --- W10 recurring-request detector ---
        if not __import__("os").environ.get("HERMES_DISABLE_GROWTH_BLOCKS"):
            try:
                from src.orchestrator.recurring_request_detector_generated import (
                    maybe_enqueue_skill_draft,
                )
                await maybe_enqueue_skill_draft(
                    user_text=task.user_message, user_id=task.user_id,
                )
            except Exception as _w10_err:  # noqa: BLE001
                log.warning("w10.detector_failed", err=str(_w10_err))
        # --- end ---"""
    if not _has_block(text, "# --- W10 recurring-request detector ---"):
        new = _insert_after_pattern(
            text,
            r"^        prompt = self\._compose_prompt\(task, intent\)\s*$",
            W10_BLOCK,
        )
        if new is not None:
            text = new
            notes.append("W10 inserted")
        else:
            notes.append("W10 skipped (anchor not found)")

    # W12 — _dispatch_master: insert after W10 (or after prompt = …)
    W12_BLOCK = """\
        # --- W12 delegation bias ---
        if not __import__("os").environ.get("HERMES_DISABLE_GROWTH_BLOCKS"):
            try:
                from src.orchestrator.delegation_bias_loader_generated import (
                    classify_intent_cluster, suggest_agents,
                )
                _w12_cluster = classify_intent_cluster(task.user_message)
                _w12_suggested = suggest_agents(_w12_cluster)
                if _w12_suggested:
                    log.info(
                        "w12.delegation_suggestion",
                        user_id=task.user_id,
                        task_id=task.task_id,
                        intent_cluster=_w12_cluster,
                        suggested=_w12_suggested,
                        resolved_handles=task.agent_handles,
                    )
            except Exception as _w12_err:  # noqa: BLE001
                log.warning("w12.suggestion_failed", err=str(_w12_err))
        # --- end ---"""
    if not _has_block(text, "# --- W12 delegation bias ---"):
        # Anchor: end of W10 block, else after prompt = self._compose_prompt
        anchor = "# --- end ---"
        if "# --- W10 recurring-request detector ---" in text and anchor in text:
            # Insert after the *first* end marker that follows W10 in _dispatch_master
            idx = text.index("# --- W10 recurring-request detector ---")
            end_idx = text.index(anchor, idx) + len(anchor)
            text = text[:end_idx] + "\n" + W12_BLOCK + text[end_idx:]
            notes.append("W12 inserted (after W10)")
        else:
            new = _insert_after_pattern(
                text,
                r"^        prompt = self\._compose_prompt\(task, intent\)\s*$",
                W12_BLOCK,
            )
            if new is not None:
                text = new
                notes.append("W12 inserted (after prompt)")
            else:
                notes.append("W12 skipped (anchor not found)")
    return text, notes


# ---- W6a / W6b in src/mcp/server.py --------------------------------------

W6A_BLOCK = """\
# --- W6a MCP tool registry extensions ---
try:
    if not __import__("os").environ.get("HERMES_DISABLE_GROWTH_BLOCKS"):
        from src.mcp.server_extensions_generated import register_extensions as _w6_register
        _w6_register(_TOOLS)
except Exception as _w6a_err:
    log.warning("w6a.register_failed", err=str(_w6a_err))
# --- end ---"""


W6B_BLOCK_RAW = """\
        # --- W6b MCP tool dispatch extensions ---
        if not __import__("os").environ.get("HERMES_DISABLE_GROWTH_BLOCKS"):
            try:
                from src.mcp.server_extensions_generated import dispatch as _w6_dispatch
                _w6_resp = await _w6_dispatch(name, arguments)
                if _w6_resp is not None:
                    return _w6_resp
            except Exception as _w6b_err:  # noqa: BLE001
                log.warning("w6b.dispatch_failed", err=str(_w6b_err))
        # --- end ---"""


def patch_mcp_server(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    if not _has_block(text, "# --- W6a MCP tool registry extensions ---"):
        # Anchor: closing `]` of `_TOOLS: list[_Tool] = [ ... ]`
        m = re.search(
            r"^_TOOLS: list\[_Tool\] = \[\n.*?\n\]",
            text, re.MULTILINE | re.DOTALL,
        )
        if m:
            insert_at = m.end()
            text = text[:insert_at] + "\n\n" + W6A_BLOCK + "\n" + text[insert_at:]
            notes.append("W6a inserted")
        else:
            notes.append("W6a skipped (anchor not found)")

    if not _has_block(text, "# --- W6b MCP tool dispatch extensions ---"):
        # Anchor: just before `if name != "hybrid.handle":`
        m = re.search(r'^        if name != "hybrid\.handle":\s*$',
                      text, re.MULTILINE)
        if m:
            insert_at = m.start()
            text = text[:insert_at] + W6B_BLOCK_RAW + "\n" + text[insert_at:]
            notes.append("W6b inserted")
        else:
            notes.append("W6b skipped (anchor not found)")
    return text, notes


# ---- W11_curator (src/jobs/curator_job.py) -------------------------------

W11_CURATOR_BLOCK = """\
        # --- W11 self-review candidates glob ---
        if not __import__("os").environ.get("HERMES_DISABLE_GROWTH_BLOCKS"):
            try:
                from pathlib import Path as _W11_Path
                _w11_root = _W11_Path(__file__).resolve().parent.parent.parent
                for _w11_y in sorted((_w11_root / "memory").glob("candidates_from_self_review_*.yaml")):
                    pass  # marker only; ingest is handled by scripts/ingest_memory_candidates.py
            except Exception:
                pass
        # --- end ---"""


def patch_curator_job(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    if _has_block(text, "# --- W11 self-review candidates glob ---"):
        return text, ["W11_curator already present"]
    # Anchor: top of CuratorJob.run()
    m = re.search(
        r"^    def run\(\s*\n        self,\s*\n        \*,\s*\n        since.*?\n        until.*?\n    \) -> JobResult:\s*\n",
        text, re.MULTILINE | re.DOTALL,
    )
    if m:
        insert_at = m.end()
        text = text[:insert_at] + W11_CURATOR_BLOCK + "\n" + text[insert_at:]
        notes.append("W11_curator inserted")
    else:
        # Fallback: after `class CuratorJob`
        m = re.search(r"^class CuratorJob\(BaseJob\):", text, re.MULTILINE)
        if m:
            insert_at = m.end()
            text = text[:insert_at] + "\n" + W11_CURATOR_BLOCK + "\n" + text[insert_at:]
            notes.append("W11_curator inserted (fallback)")
        else:
            notes.append("W11_curator skipped (anchor not found)")
    return text, notes


# ---- W11_promoter (src/jobs/skill_promoter.py) ---------------------------

W11_PROMOTER_BLOCK = """\
        # --- W11 self-review drafts scan ---
        if not __import__("os").environ.get("HERMES_DISABLE_GROWTH_BLOCKS"):
            try:
                from pathlib import Path as _W11_Path
                _w11_root = _W11_Path(__file__).resolve().parent.parent.parent
                _w11_src = _w11_root / "skills" / "generated_from_self_review"
                if _w11_src.exists():
                    self.draft_dir.mkdir(parents=True, exist_ok=True)
                    for _w11_md in _w11_src.glob("**/*.md"):
                        _w11_target = self.draft_dir / f"sr_{_w11_md.parent.name}_{_w11_md.name}"
                        if not _w11_target.exists():
                            try:
                                _w11_target.write_text(_w11_md.read_text(encoding="utf-8"), encoding="utf-8")
                            except OSError:
                                pass
            except Exception as _w11p_err:  # noqa: BLE001
                log.warning("w11.promoter_scan_failed", err=str(_w11p_err))
        # --- end ---"""


def patch_skill_promoter(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    if _has_block(text, "# --- W11 self-review drafts scan ---"):
        return text, ["W11_promoter already present"]
    # Anchor: first line of run_weekly() body — `result = SkillPromoterResult()`
    m = re.search(r"^        result = SkillPromoterResult\(\)\s*$",
                  text, re.MULTILINE)
    if m:
        insert_at = m.end()
        text = text[:insert_at] + "\n" + W11_PROMOTER_BLOCK + text[insert_at:]
        notes.append("W11_promoter inserted")
    else:
        notes.append("W11_promoter skipped (anchor not found)")
    return text, notes


# ---- W3 timers -----------------------------------------------------------

W3_WINDOWS_TASKS = """\
    # --- W3 growth-loop timer extensions ---
    # HERMES_DISABLE_GROWTH_BLOCKS short-circuit handled by hermes-setup runtime
    ("HermesSelfReview",       "21:00", "scripts/migration_self_review.py",       ()),
    ("HermesDialectic",        "06:00", "scripts/dialectic_user_modeling.py",     ("--apply",)),
    ("HermesSkillSelfModify",  "23:00", "scripts/skill_self_modify.py",           ()),
    ("HermesDelegationPattern","12:00", "scripts/delegation_pattern_extractor.py",("--apply",)),
    ("HermesSkillDraftQueueDrainer","00:00", "scripts/process_skill_draft_queue.py", ("--apply",)),
    # --- end ---"""


W3_DARWIN_TASKS = """\
    # --- W3 growth-loop timer extensions ---
    ("dev.hermes.self_review",        21, 0,  "scripts/migration_self_review.py",       ()),
    ("dev.hermes.dialectic",           6, 0,  "scripts/dialectic_user_modeling.py",     ("--apply",)),
    ("dev.hermes.skill_self_modify",  23, 0,  "scripts/skill_self_modify.py",           ()),
    ("dev.hermes.delegation_pattern", 12, 0,  "scripts/delegation_pattern_extractor.py",("--apply",)),
    ("dev.hermes.skill_draft_queue_drainer", 0, 0, "scripts/process_skill_draft_queue.py", ("--apply",)),
    # --- end ---"""


W3_LINUX_INSTALLERS = """\
    # --- W3 growth-loop timer extensions ---
    # NOTE: install scripts may not exist yet — graceful skip in register().
    ("hermes-self-review.timer",        "scripts/install_self_review_timer.sh"),
    ("hermes-dialectic.timer",          "scripts/install_dialectic_timer.sh"),
    ("hermes-skill-self-modify.timer",  "scripts/install_skill_self_modify_timer.sh"),
    ("hermes-delegation-pattern.timer", "scripts/install_delegation_pattern_timer.sh"),
    ("hermes-skill-draft-queue-drainer.timer", "scripts/install_skill_draft_queue_drainer_timer.sh"),
    # --- end ---"""


def patch_windows_timers(text: str) -> tuple[str, list[str]]:
    if _has_block(text, "# --- W3 growth-loop timer extensions ---"):
        return text, ["W3 windows already present"]
    # Insert before the closing `)` of _TASKS tuple
    m = re.search(
        r"(\(\"HermesPromoter\",\s*\"23:30\",.*?\),)\s*\n(\))",
        text, re.DOTALL,
    )
    if m:
        insert_at = m.end(1)
        text = text[:insert_at] + "\n" + W3_WINDOWS_TASKS + text[insert_at:]
        return text, ["W3 windows inserted"]
    return text, ["W3 windows skipped (anchor not found)"]


def patch_darwin_timers(text: str) -> tuple[str, list[str]]:
    if _has_block(text, "# --- W3 growth-loop timer extensions ---"):
        return text, ["W3 darwin already present"]
    m = re.search(
        r"(\(\"dev\.hermes\.promoter\",\s*23, 30,.*?\),)\s*\n(\))",
        text, re.DOTALL,
    )
    if m:
        insert_at = m.end(1)
        text = text[:insert_at] + "\n" + W3_DARWIN_TASKS + text[insert_at:]
        return text, ["W3 darwin inserted"]
    return text, ["W3 darwin skipped (anchor not found)"]


def patch_linux_timers(text: str) -> tuple[str, list[str]]:
    if _has_block(text, "# --- W3 growth-loop timer extensions ---"):
        return text, ["W3 linux already present"]
    m = re.search(
        r"(\(\"hermes-curator\.timer\",\s*\"scripts/install_curator_timer\.sh\"\),)\s*\n(\))",
        text,
    )
    if m:
        insert_at = m.end(1)
        text = text[:insert_at] + "\n" + W3_LINUX_INSTALLERS + text[insert_at:]
        return text, ["W3 linux inserted"]
    return text, ["W3 linux skipped (anchor not found)"]


# ---- driver --------------------------------------------------------------

PATCHES_NON_TIMER = [
    ("src/orchestrator/hermes_master.py", patch_hermes_master),
    ("src/mcp/server.py", patch_mcp_server),
    ("src/jobs/curator_job.py", patch_curator_job),
    ("src/jobs/skill_promoter.py", patch_skill_promoter),
]

PATCHES_TIMER = [
    ("src/cli/timer_handlers/windows.py", patch_windows_timers),
    ("src/cli/timer_handlers/darwin.py", patch_darwin_timers),
    ("src/cli/timer_handlers/linux.py", patch_linux_timers),
]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True)
    g.add_argument("--apply", action="store_true")
    p.add_argument("--timers-only", action="store_true")
    args = p.parse_args()
    if args.apply:
        args.dry_run = False

    selected = PATCHES_TIMER if args.timers_only else (PATCHES_NON_TIMER + PATCHES_TIMER)
    overall_changes = 0
    for rel, fn in selected:
        path = REPO_ROOT / rel
        if not path.exists():
            print(f"  ! missing: {rel}")
            continue
        original = path.read_text(encoding="utf-8")
        new_text, notes = fn(original)
        print(f"\n# {rel}")
        for n in notes:
            print(f"  - {n}")
        if new_text != original:
            overall_changes += 1
            if args.dry_run:
                print(f"  (dry-run) +{len(new_text) - len(original)} chars")
            else:
                path.write_text(new_text, encoding="utf-8")
                print("  WRITTEN")

    print(f"\nTotal files changed: {overall_changes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
