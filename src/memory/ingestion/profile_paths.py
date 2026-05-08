"""Profile-scoped path policy helper (P0-A: validator only).

The Growing Agent Memory Architecture treats ``processed_memory``,
compiled ``memory``, and ``source_manifests`` as **profile-scoped**: a
``default`` profile uses ``./data/...`` while named profiles would use
``./data/profiles/{profile_name}/...``. P0-A only ships the policy
helper — production read/write paths are NOT switched yet. Setting
``memory_profile_scoped=True`` in P0-A logs an experimental no-op
warning so it cannot silently change behaviour.

The validator enforces a strict slug: ``^[a-z0-9_-]{1,64}$``. This
blocks path traversal (``../escape``), absolute paths, Unicode names,
and empty strings. Future P-x migration scripts can rely on the slug
shape to stay safe across OSes (Windows vs POSIX path semantics).
"""
from __future__ import annotations

import re
import warnings
from pathlib import Path

_SLUG_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


class InvalidProfileNameError(ValueError):
    """Raised when a profile name fails slug validation."""


class ProfileScopedExperimentalWarning(UserWarning):
    """Emitted when ``profile_scoped=True`` is requested under P0-A.

    P0-A keeps production paths on default mode regardless of the flag.
    This warning makes the no-op visible so callers don't assume
    ``./data/profiles/...`` is being used.
    """


def validate_profile_slug(name: str) -> str:
    """Return ``name`` if it matches ``^[a-z0-9_-]{1,64}$``.

    Otherwise raise :class:`InvalidProfileNameError`. Empty strings,
    paths, and unicode names are all rejected — the slug shape is the
    same shape filesystems and migration scripts will have to handle.
    """
    if not isinstance(name, str):
        raise InvalidProfileNameError(
            f"profile name must be str, got {type(name).__name__}"
        )
    if not name:
        raise InvalidProfileNameError("profile name must not be empty")
    if not _SLUG_RE.fullmatch(name):
        raise InvalidProfileNameError(
            f"profile name {name!r} must match ^[a-z0-9_-]{{1,64}}$"
        )
    return name


def resolve_profile_root(
    name: str = "default",
    scoped: bool = False,
    base: Path | str = Path("./data"),
) -> Path:
    """Return the profile root directory.

    P0-A semantics:

    - ``scoped=False`` → ``{base}`` (default mode, what production uses).
    - ``scoped=True``  → emit :class:`ProfileScopedExperimentalWarning`
      and **still return ``{base}``**. A real switch to
      ``{base}/profiles/{name}`` will land in a follow-up PR.

    Validation: ``name`` always passes through
    :func:`validate_profile_slug`, even when ``scoped=False``, so a bad
    profile name fails fast regardless of the runtime flag. This is
    intentional — tests and tooling that pre-validate profile names
    benefit from the same error path.
    """
    name = validate_profile_slug(name)
    base_path = Path(base)
    if scoped:
        warnings.warn(
            "memory_profile_scoped=True is experimental and currently "
            "a no-op — P0-A keeps reads/writes on the default-mode root "
            f"({base_path}). Profile-scoped paths will activate in a "
            "later PR; until then, the request was logged but the "
            "returned root is the default-mode root.",
            ProfileScopedExperimentalWarning,
            stacklevel=2,
        )
    return base_path
