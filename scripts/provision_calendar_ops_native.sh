#!/usr/bin/env bash
# provision_calendar_ops_native.sh — apply the 4 profile-side fixes needed to
# make the Hermes-native `calendar_ops` profile (~/.hermes/profiles/calendar_ops/)
# actually reach Google Calendar under hermes-hybrid's CalendarSkill.
#
# This is idempotent: re-running is safe. See docs/calendar_ops_runbook.md for
# the root-cause analysis of each fix.
#
# Usage (from WSL Ubuntu where Hermes lives):
#   bash /mnt/e/hermes-hybrid/scripts/provision_calendar_ops_native.sh
#
# Pre-reqs (the script refuses to continue if any is missing):
#   - `hermes` on PATH (Hermes Agent ≥ 0.10.x)
#   - google-api-python-client / google-auth installed somewhere on the user's
#     python3 site-packages (default: ~/.local/lib/python3.12/site-packages)
#   - `~/.hermes/google_token.json` and `~/.hermes/google_client_secret.json`
#     already produced by `setup.py --client-secret … --auth-url / --auth-code …`
#     for the DEFAULT hermes home — we copy them into the profile.
#
# What it does:
#   1. Copy OAuth token+client_secret from ~/.hermes/ into the profile dir
#      (because `-p calendar_ops` sets HERMES_HOME=~/.hermes/profiles/calendar_ops
#      and the skill looks there).
#   2. Symlink hermes_constants.py into the profile root so the skill's
#      setup.py parents[4] fallback resolves when PYTHONPATH isn't set.
#   3. Install the `gapi` wrapper (absolute paths, PYTHONPATH baked in) to
#      sidestep Hermes' bash tool stripping user-site and rewriting $HOME.
#   4. Patch SKILL.md so the model is told to call the absolute wrapper path,
#      not the templated GAPI="python ${HERMES_HOME:-$HOME/.hermes}/…" form.
#   5. Disable the `browser` and `web` toolsets for this profile so small
#      models (e.g. gpt-4o-mini) don't waste turns trying `browser: navigate
#      calendar.google.com` instead of calling the skill.

set -euo pipefail

PROFILE_NAME="calendar_ops"
HERMES_ROOT="${HOME}/.hermes"
PROFILE_DIR="${HERMES_ROOT}/profiles/${PROFILE_NAME}"
HERMES_AGENT_DIR="${HERMES_ROOT}/hermes-agent"
USER_SITE="${HOME}/.local/lib/python3.12/site-packages"

say() { printf '\033[1;36m[provision]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[provision]\033[0m %s\n' "$*" >&2; }

# ── Pre-flight ────────────────────────────────────────────────────────────
command -v hermes >/dev/null 2>&1 || { err "hermes CLI not on PATH"; exit 1; }
[ -d "$PROFILE_DIR" ] || { err "profile dir missing: $PROFILE_DIR  (run 'hermes profile create $PROFILE_NAME' first)"; exit 1; }
[ -f "$HERMES_AGENT_DIR/hermes_constants.py" ] || { err "hermes_constants.py missing at $HERMES_AGENT_DIR"; exit 1; }

# ── 1. OAuth tokens into the profile ──────────────────────────────────────
for f in google_token.json google_client_secret.json; do
    src="$HERMES_ROOT/$f"
    dst="$PROFILE_DIR/$f"
    if [ ! -f "$src" ]; then
        err "OAuth file missing at $src"
        err "  Run the skill's setup.py first against the default HERMES_HOME"
        err "  (see ~/.hermes/skills/productivity/google-workspace/SKILL.md)"
        exit 1
    fi
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        say "OAuth $f already in profile (skipping)"
    else
        cp "$src" "$dst"
        say "copied $f → $dst"
    fi
done

# ── 2. hermes_constants.py symlink ────────────────────────────────────────
# Rationale: the profile-local copy of setup.py falls back to
#   sys.path.insert(0, Path(__file__).resolve().parents[4])
# which under the profile tree resolves to $PROFILE_DIR — NOT the hermes-agent
# root. Symlinking hermes_constants.py into the profile makes the fallback
# find it.
link="$PROFILE_DIR/hermes_constants.py"
target="$HERMES_AGENT_DIR/hermes_constants.py"
if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then
    say "hermes_constants.py symlink already correct"
else
    ln -sf "$target" "$link"
    say "symlink: $link → $target"
fi

# ── 3. Install gapi wrapper ───────────────────────────────────────────────
# Rationale: Hermes' bash tool (a) spawns a fresh shell per tool call so shell
# variables don't persist across turns, (b) rewrites $HOME to the profile
# directory, and (c) runs python3 without the user-site directory loaded. A
# self-contained wrapper with hardcoded absolute paths sidesteps all three.
wrapper="$PROFILE_DIR/gapi"
cat > "$wrapper" <<'GAPI_EOF'
#!/bin/bash
# gapi — wrapper for google_api.py with PYTHONPATH set so googleapiclient loads
# under Hermes' sanitized bash tool (user-site stripped, $HOME rewritten).
# Installed by scripts/provision_calendar_ops_native.sh — DO NOT edit manually;
# changes will be overwritten on next re-provision.
export PYTHONPATH="__USER_SITE__${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 "__PROFILE_DIR__/skills/productivity/google-workspace/scripts/google_api.py" "$@"
GAPI_EOF
sed -i "s|__USER_SITE__|${USER_SITE}|g; s|__PROFILE_DIR__|${PROFILE_DIR}|g" "$wrapper"
chmod +x "$wrapper"
say "installed wrapper: $wrapper"

# Smoke-test the wrapper itself before we depend on it.
if "$wrapper" calendar list --max 1 >/dev/null 2>&1; then
    say "gapi wrapper smoke test: OK"
else
    err "gapi wrapper smoke test FAILED — try: $wrapper calendar list --max 1"
    err "  Likely cause: googleapiclient not installed at $USER_SITE"
    err "  Install: pip install --user google-api-python-client google-auth-oauthlib google-auth-httplib2"
    exit 1
fi

# ── 4. Patch SKILL.md to point at the wrapper ─────────────────────────────
skill_md="$PROFILE_DIR/skills/productivity/google-workspace/SKILL.md"
if [ ! -f "$skill_md" ]; then
    err "SKILL.md missing: $skill_md  (did 'hermes profile create' skip it?)"
    exit 1
fi
if grep -q "hermes-hybrid calendar_ops profile" "$skill_md"; then
    say "SKILL.md already patched (skipping)"
else
    python3 - "$skill_md" "$wrapper" <<'PATCH_EOF'
import re, sys
path, wrapper_path = sys.argv[1], sys.argv[2]
txt = open(path).read()
old_line = 'GAPI="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/google_api.py"'
if old_line not in txt:
    print(f"WARN: expected GAPI line not found in {path} — leaving SKILL.md untouched", file=sys.stderr)
    sys.exit(0)
new_block = (
    "# IMPORTANT (hermes-hybrid calendar_ops profile):\n"
    "# Hermes' bash tool starts a fresh shell per invocation (shell variables\n"
    "# do NOT persist across turns) AND runs python3 without the user-site\n"
    "# directory where googleapiclient lives. Do NOT define GAPI as a shell\n"
    "# variable — call the absolute-path wrapper directly. It bakes in\n"
    "# PYTHONPATH and the absolute script path.\n"
    "#\n"
    "# Use this command as-is in every turn:\n"
    f"#   {wrapper_path} <service> <action> [args]\n"
    "#\n"
    f"# Example: {wrapper_path} calendar list --max 10\n"
    f'GAPI="{wrapper_path}"'
)
open(path, "w").write(txt.replace(old_line, new_block, 1))
print(f"patched: {path}")
PATCH_EOF
    say "patched SKILL.md"
fi

# ── 5. Disable browser + web toolsets for this profile ────────────────────
# Rationale: small models (gpt-4o-mini) will pick `browser: navigate
# calendar.google.com` over the google-workspace skill if the browser tool is
# visible. Disabling removes the temptation.
disabled_before=$(HERMES_HOME="$PROFILE_DIR" hermes tools list 2>/dev/null | grep -E 'disabled.*\b(browser|web)\b' | wc -l)
if [ "$disabled_before" -lt 2 ]; then
    HERMES_HOME="$PROFILE_DIR" hermes tools disable browser web --platform cli >/dev/null
    say "disabled browser+web toolsets for cli"
else
    say "browser+web already disabled (skipping)"
fi

# ── Done ──────────────────────────────────────────────────────────────────
cat <<SUMMARY

✓ calendar_ops profile provisioned.

Next steps:
  1. End-to-end test:
       hermes -p calendar_ops chat -q '이번주 일정 알려줘' -Q -s productivity/google-workspace --yolo
  2. Flip CALENDAR_SKILL_ENABLED=true in hermes-hybrid's .env, then restart
     the Discord bot so it picks up the new setting (pydantic-settings only
     reads .env at process start).
  3. Try @Agent-Hermes 이번주 일정 알려줘 in Discord.

Troubleshooting: see docs/calendar_ops_runbook.md
SUMMARY
