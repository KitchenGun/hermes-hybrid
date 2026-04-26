"""JobFactory: scans profiles/, matches requests, creates new profiles.

Matching strategy (cheap → expensive):
  1. Keyword overlap against each profile's ``SOUL.md`` Scope section
     and ``on_demand/*.yaml`` trigger.patterns.
  2. If top score ≥ ``strong_match_threshold`` → return immediately.
  3. If multiple profiles tie within ``ambiguous_margin`` → caller should
     ask user which one; we surface all candidates.
  4. If max score < ``weak_match_threshold`` → no-match, caller can
     request profile creation via :meth:`create_profile`.

Profile creation uses a template (see ``profile_template.py``) and writes
a minimal skeleton: ``config.yaml``, ``SOUL.md``, ``intent_schema.json``,
and empty ``skills/``, ``cron/``, ``on_demand/``, ``watchers/``, ``memories/``
directories — matching the standard layout calendar_ops and kk_job follow.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

from src.job_factory.profile_template import render_new_profile
from src.obs import get_logger

log = get_logger(__name__)

_TOKEN_RE = re.compile(r"[a-zA-Z가-힣0-9_]+")
_STOPWORDS_KR = {
    "해줘", "해", "좀", "그리고", "하고", "에", "을", "를", "이", "가", "는", "은",
    "의", "와", "과", "에서", "으로", "로", "에게", "한테",
}
_STOPWORDS_EN = {"the", "a", "an", "to", "for", "of", "in", "on", "and", "or"}


class JobFactoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProfileMatch:
    profile_id: str
    score: float
    matched_terms: tuple[str, ...]
    matched_pattern: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "score": round(self.score, 3),
            "matched_terms": list(self.matched_terms),
            "matched_pattern": self.matched_pattern,
        }


@dataclass
class _ProfileIndex:
    """In-memory searchable view of a single profile."""

    profile_id: str
    scope_terms: frozenset[str]
    trigger_patterns: tuple[tuple[str, str], ...]  # (on_demand_name, pattern_text)
    schema_actions: frozenset[str]


@dataclass
class JobFactory:
    profiles_dir: Path
    strong_match_threshold: float = 0.60
    weak_match_threshold: float = 0.25
    ambiguous_margin: float = 0.10
    cache_ttl_seconds: float = 30.0
    allow_profile_creation: bool = False  # gated; flip on when template vetted

    def __post_init__(self) -> None:
        self.profiles_dir = Path(self.profiles_dir)
        if not self.profiles_dir.exists():
            raise JobFactoryError(f"profiles_dir missing: {self.profiles_dir}")
        self._index_cache: list[_ProfileIndex] = []
        self._cache_mtime: float = 0.0

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def list_profiles(self) -> list[str]:
        return sorted(
            p.name
            for p in self.profiles_dir.iterdir()
            if p.is_dir() and (p / "config.yaml").exists()
        )

    def _build_index(self) -> list[_ProfileIndex]:
        idx: list[_ProfileIndex] = []
        for profile_id in self.list_profiles():
            pdir = self.profiles_dir / profile_id
            scope_terms = self._load_scope_terms(pdir)
            patterns = self._load_trigger_patterns(pdir)
            actions = self._load_schema_actions(pdir)
            idx.append(
                _ProfileIndex(
                    profile_id=profile_id,
                    scope_terms=scope_terms,
                    trigger_patterns=patterns,
                    schema_actions=actions,
                )
            )
        return idx

    def _index(self) -> list[_ProfileIndex]:
        now = time.monotonic()
        if not self._index_cache or (now - self._cache_mtime) > self.cache_ttl_seconds:
            self._index_cache = self._build_index()
            self._cache_mtime = now
        return self._index_cache

    def invalidate_cache(self) -> None:
        self._cache_mtime = 0.0

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    def _load_scope_terms(self, pdir: Path) -> frozenset[str]:
        soul = pdir / "SOUL.md"
        if not soul.exists():
            return frozenset()
        try:
            text = soul.read_text(encoding="utf-8")
        except OSError:
            return frozenset()
        # Focus on the Scope / Identity section where the "허용" / "금지" lines live.
        # Cheap: tokenize the whole file; the schema/keyword signal dominates noise.
        return frozenset(_tokenize(text))

    def _load_trigger_patterns(self, pdir: Path) -> tuple[tuple[str, str], ...]:
        out: list[tuple[str, str]] = []
        od_dir = pdir / "on_demand"
        if not od_dir.exists():
            return ()
        for yml in sorted(od_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as e:
                log.info("factory.pattern_load_failed", path=str(yml), err=str(e))
                continue
            trigger = data.get("trigger") or {}
            patterns = trigger.get("patterns") or []
            for pat in patterns:
                if isinstance(pat, str) and pat.strip():
                    out.append((yml.stem, pat.lower()))
        return tuple(out)

    def _load_schema_actions(self, pdir: Path) -> frozenset[str]:
        schema = pdir / "intent_schema.json"
        if not schema.exists():
            return frozenset()
        try:
            import json

            data = json.loads(schema.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return frozenset()
        props = (data.get("properties") or {}).get("action") or {}
        enum_vals = props.get("enum") or []
        return frozenset(str(v).lower() for v in enum_vals)

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def match(self, message: str) -> list[ProfileMatch]:
        """Score every profile against ``message``. Sorted desc by score."""
        tokens = set(_tokenize(message))
        msg_lower = message.lower()
        results: list[ProfileMatch] = []
        for idx in self._index():
            best_pattern: Optional[str] = None
            pattern_hit = 0.0
            for _, pat in idx.trigger_patterns:
                if pat in msg_lower:
                    pattern_hit = max(pattern_hit, 1.0)
                    best_pattern = pat
                    break

            scope_overlap = _overlap_ratio(tokens, idx.scope_terms)
            action_hit = 0.0
            for act in idx.schema_actions:
                if act in msg_lower:
                    action_hit = max(action_hit, 0.6)
                    break

            # Weighted blend: explicit trigger pattern dominates; schema
            # action hits are a mid signal; raw scope overlap breaks ties.
            score = max(pattern_hit, action_hit * 0.9, scope_overlap * 0.8)
            matched_terms = tuple(sorted(tokens & idx.scope_terms))[:6]
            results.append(
                ProfileMatch(
                    profile_id=idx.profile_id,
                    score=score,
                    matched_terms=matched_terms,
                    matched_pattern=best_pattern,
                )
            )
        results.sort(key=lambda m: m.score, reverse=True)
        return results

    def decide(self, message: str) -> dict:
        """High-level resolver.

        Returns::

          {
            "status": "match" | "ambiguous" | "no_match",
            "profile_id": str?,            # only on "match"
            "candidates": list[ProfileMatch]
          }
        """
        candidates = self.match(message)
        if not candidates:
            return {"status": "no_match", "profile_id": None, "candidates": []}

        top = candidates[0]
        if top.score >= self.strong_match_threshold:
            # Check tie: if second is within margin, flag ambiguous.
            if len(candidates) > 1 and (top.score - candidates[1].score) < self.ambiguous_margin:
                if candidates[1].score >= self.weak_match_threshold:
                    return {
                        "status": "ambiguous",
                        "profile_id": None,
                        "candidates": candidates[:3],
                    }
            return {
                "status": "match",
                "profile_id": top.profile_id,
                "candidates": candidates[:3],
            }
        if top.score < self.weak_match_threshold:
            return {"status": "no_match", "profile_id": None, "candidates": candidates[:3]}
        # Medium-confidence: ambiguous — ask user or let caller escalate
        return {
            "status": "ambiguous",
            "profile_id": None,
            "candidates": candidates[:3],
        }

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_profile(
        self,
        profile_id: str,
        *,
        role: str,
        scope_allowed: Iterable[str],
        scope_forbidden: Iterable[str] = (),
        actions: Iterable[str] = (),
        dry_run: bool = False,
    ) -> Path:
        """Materialize a new profile skeleton on disk.

        Uses the standard directory layout (see
        :func:`render_new_profile`). Returns the profile directory path.

        Raises :class:`JobFactoryError` if creation is disabled or if the
        profile already exists — overwriting is always explicit (the
        caller must delete the old dir first).
        """
        if not self.allow_profile_creation and not dry_run:
            raise JobFactoryError(
                "profile creation disabled (set allow_profile_creation=True "
                "on JobFactory to enable)"
            )
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", profile_id):
            raise JobFactoryError(
                f"invalid profile_id `{profile_id}` — use lowercase_snake_case"
            )
        target = self.profiles_dir / profile_id
        if target.exists():
            raise JobFactoryError(f"profile `{profile_id}` already exists at {target}")

        files = render_new_profile(
            profile_id=profile_id,
            role=role,
            scope_allowed=list(scope_allowed),
            scope_forbidden=list(scope_forbidden),
            actions=list(actions),
        )
        if dry_run:
            log.info(
                "factory.create_profile.dry_run",
                profile_id=profile_id,
                file_count=len(files),
            )
            return target

        target.mkdir(parents=True, exist_ok=False)
        for rel_path, content in files.items():
            dest = target / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        self.invalidate_cache()
        log.info("factory.create_profile.ok", profile_id=profile_id, path=str(target))
        return target


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text.lower()):
        if len(tok) < 2:
            continue
        if tok in _STOPWORDS_KR or tok in _STOPWORDS_EN:
            continue
        out.append(tok)
    return out


def _overlap_ratio(a: set[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    hits = len(a & b)
    # Normalize by the smaller side so a huge SOUL.md doesn't drown a
    # short user message.
    return hits / max(1, min(len(a), 8))
