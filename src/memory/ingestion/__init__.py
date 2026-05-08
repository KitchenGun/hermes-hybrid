"""Hermes Growing Agent Memory Architecture — ingestion layer (P0-A).

This package houses the storage / safety foundation for the memory growth
loop. P0-A modules:

- :mod:`manifest`        — sha16/content_sha256, source_manifests jsonl with
                           ``record_type`` discriminator, dedup, malformed skip.
- :mod:`pii`             — regex + keyword PII detection.
- :mod:`security_scan`   — prompt-injection / exfiltration scan with low /
                           medium / high severity, context-aware downgrade,
                           credential carve-out.
- :mod:`profile_paths`   — slug validator and root resolver. Production
                           paths are NOT switched in P0-A; a True
                           ``memory_profile_scoped`` only logs an
                           experimental no-op warning.

P0-B will add ``writer`` and ``normalizer`` and wire MemoryCurator into the
USER.md / MEMORY.md split compile. P0-A is callable but does not yet
perform any writes against ``data/processed_memory`` or
``data/source_manifests`` outside of explicit scripts.
"""
from __future__ import annotations

from .manifest import (
    SCHEMA_VERSION,
    ALLOWED_SOURCES,
    ALLOWED_RETENTIONS,
    ManifestEntry,
    ManifestStore,
    content_sha256,
    sha16,
)
from .pii import PIIScanner, PIIFinding
from .security_scan import (
    SecurityScanner,
    SecurityFinding,
    SecuritySeverity,
)
from .profile_paths import (
    InvalidProfileNameError,
    ProfileScopedExperimentalWarning,
    resolve_profile_root,
    validate_profile_slug,
)

__all__ = [
    "SCHEMA_VERSION",
    "ALLOWED_SOURCES",
    "ALLOWED_RETENTIONS",
    "ManifestEntry",
    "ManifestStore",
    "content_sha256",
    "sha16",
    "PIIScanner",
    "PIIFinding",
    "SecurityScanner",
    "SecurityFinding",
    "SecuritySeverity",
    "InvalidProfileNameError",
    "ProfileScopedExperimentalWarning",
    "resolve_profile_root",
    "validate_profile_slug",
]
