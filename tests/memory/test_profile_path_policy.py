"""Tests for src.memory.ingestion.profile_paths (P0-A).

Coverage:
- valid slugs accepted; bad slugs rejected
- default mode returns the base path unchanged
- ``scoped=True`` raises ``ProfileScopedExperimentalWarning`` and STILL
  returns the base path (P0-A no-op)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.memory.ingestion.profile_paths import (
    InvalidProfileNameError,
    ProfileScopedExperimentalWarning,
    resolve_profile_root,
    validate_profile_slug,
)


def test_validate_profile_slug_accepts_lowercase_alnum_underscore_hyphen() -> None:
    for name in ("default", "kk_job", "advisor-ops", "abc123", "x"):
        assert validate_profile_slug(name) == name


@pytest.mark.parametrize(
    "bad",
    [
        "",                # empty
        "../escape",        # path traversal
        "./relative",       # path-like
        "/absolute",        # absolute
        "Default",          # uppercase
        "한국어",            # unicode (rejected for cross-OS safety)
        "name with space",  # whitespace
        "x" * 65,           # too long
        "name.dot",         # dot disallowed
    ],
)
def test_validate_profile_slug_rejects_unsafe_names(bad: str) -> None:
    with pytest.raises(InvalidProfileNameError):
        validate_profile_slug(bad)


def test_validate_profile_slug_rejects_non_string() -> None:
    with pytest.raises(InvalidProfileNameError):
        validate_profile_slug(123)  # type: ignore[arg-type]


def test_resolve_default_mode_returns_base_path() -> None:
    base = Path("./data")
    assert resolve_profile_root("default", scoped=False, base=base) == base
    # Different valid profile name, default mode → still base path
    assert resolve_profile_root("kk_job", scoped=False, base=base) == base


def test_resolve_default_mode_validates_profile_slug() -> None:
    """Even when scoped=False, an invalid slug should still raise."""
    with pytest.raises(InvalidProfileNameError):
        resolve_profile_root("../escape", scoped=False)


def test_resolve_scoped_emits_experimental_warning_and_no_ops() -> None:
    base = Path("./data")
    with pytest.warns(ProfileScopedExperimentalWarning):
        result = resolve_profile_root("default", scoped=True, base=base)
    # P0-A is a no-op — the returned root must still be the base.
    assert result == base


def test_resolve_scoped_invalid_name_raises_before_warning() -> None:
    # Bad name fails validation first — no warning leaked.
    import warnings as _warn
    with _warn.catch_warnings():
        _warn.simplefilter("error")
        with pytest.raises(InvalidProfileNameError):
            resolve_profile_root("../escape", scoped=True)


def test_resolve_accepts_str_base() -> None:
    base = "./tmp_data"
    assert resolve_profile_root("default", scoped=False, base=base) == Path(base)
