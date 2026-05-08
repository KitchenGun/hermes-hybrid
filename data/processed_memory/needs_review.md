<!-- Hermes processed memory: needs_review
     Holds items that ingestion flagged but did not auto-confirm:
       - conflict between old and new instructions (both retained, neither active)
       - PII candidates flagged by src/memory/ingestion/pii.py
       - security risks (medium/high) flagged by src/memory/ingestion/security_scan.py
       - update candidates from ExperienceLog where confidence is too low
         to flip status=active automatically.
     Items here are EXCLUDED from MemoryCurator compile (data/memory/USER.md
     and MEMORY.md). They surface only when a human reviews and resolves
     each entry by editing the meta block (status=active or status=superseded).
     WARNING: Track only in private repos. -->

# NEEDS_REVIEW

Quarantine for ingestion candidates pending human decision. Items are
excluded from compile until status is flipped to active or superseded.

<!-- Items appended below this line as ## {title} sections with meta blocks
     including reason=conflict|pii|security_risk|low_confidence. -->
