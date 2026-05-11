#!/usr/bin/env bash
# Block C: 10 stopped profile (placeholder 없음, dest clear) — 직접 mv
set -euo pipefail

echo "=== block C: 10 nested → ~/.hermes/profiles/ ==="

mv ~/.hermes/profiles/profiles/documentation  ~/.hermes/profiles/documentation
echo "  restored documentation"
mv ~/.hermes/profiles/profiles/finder         ~/.hermes/profiles/finder
echo "  restored finder"
mv ~/.hermes/profiles/profiles/implementation ~/.hermes/profiles/implementation
echo "  restored implementation"
mv ~/.hermes/profiles/profiles/infrastructure ~/.hermes/profiles/infrastructure
echo "  restored infrastructure"
mv ~/.hermes/profiles/profiles/installer_ops  ~/.hermes/profiles/installer_ops
echo "  restored installer_ops"
mv ~/.hermes/profiles/profiles/journal_ops    ~/.hermes/profiles/journal_ops
echo "  restored journal_ops"
mv ~/.hermes/profiles/profiles/mail_ops       ~/.hermes/profiles/mail_ops
echo "  restored mail_ops"
mv ~/.hermes/profiles/profiles/planning       ~/.hermes/profiles/planning
echo "  restored planning"
mv ~/.hermes/profiles/profiles/quality        ~/.hermes/profiles/quality
echo "  restored quality"
mv ~/.hermes/profiles/profiles/research       ~/.hermes/profiles/research
echo "  restored research"

echo
echo "=== nested remaining (should be advisor_ops + calendar_ops only) ==="
ls ~/.hermes/profiles/profiles/ 2>&1
