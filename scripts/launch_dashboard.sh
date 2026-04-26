#!/bin/bash -l
# Launch Hermes dashboard for calendar_ops profile.
# -l flag loads login profile so ~/.local/bin (hermes) is on PATH.
# Stdout/stderr redirect internally to avoid cmd.exe quote-stripping issues
# when called from run_all.bat.
exec > /tmp/hermes-dashboard.log 2>&1
exec hermes -p calendar_ops dashboard --no-open
