"""Pipeline workflows — Phase 12 (2026-05-07).

1ilkhamov/opencode-hermes-multiagent 의 sequential agent pipeline 패턴 흡수.
사용자 메시지의 keyword 가 pipeline trigger 와 매치하면 master 가
``@finder → @analyst → ...`` 순서로 자동 sequential 실행.

매치 우선순위:
  1. @handle 명시 mention (Phase 9, 우선)
  2. pipeline trigger_keyword (이 모듈)
  3. fallthrough — master 단일 호출

단계 사이 hand-off:
  단계 N 의 응답이 단계 N+1 의 prompt 에 ``[prior:@handle]`` prefix 로 prepend.
  master 가 SKILL.md frontmatter + 이전 단계 결과 + 사용자 원 메시지를 보고 응답.

단계 사이 checkpoint:
  ``checkpoint_after`` 에 명시된 단계 끝나면 Discord 에 progress message 강조.
  실제 사용자 cancel 버튼은 Phase 16+ 에서. 지금은 message text 만.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.obs import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Pipeline:
    """One sequential workflow definition."""

    pipeline_id: str
    description: str
    trigger_keywords: tuple[str, ...]
    sequence: tuple[str, ...]              # ("@finder", "@analyst", ...)
    checkpoint_after: tuple[str, ...]       # subset of sequence

    def matches(self, user_message: str) -> bool:
        """Case-insensitive substring match against trigger keywords."""
        if not user_message:
            return False
        msg = user_message.lower()
        return any(kw.lower() in msg for kw in self.trigger_keywords)


class PipelineCatalog:
    """Lazy YAML loader. Single instance per process is fine."""

    def __init__(self, yaml_path: Path | None = None):
        self._yaml_path = (
            Path(yaml_path)
            if yaml_path is not None
            else Path(__file__).resolve().parents[2] / "data" / "pipelines.yaml"
        )
        self._pipelines: dict[str, Pipeline] | None = None

    def all(self) -> dict[str, Pipeline]:
        if self._pipelines is None:
            self._pipelines = self._load()
        return dict(self._pipelines)

    def get(self, pipeline_id: str) -> Pipeline | None:
        return self.all().get(pipeline_id)

    def match(self, user_message: str) -> Pipeline | None:
        """Return the first pipeline whose trigger_keywords match.

        Order is the YAML insertion order — feature_dev / bug_fix /
        security_review / refactor. Author the YAML so most-specific
        matchers come first if there's overlap.
        """
        for p in self.all().values():
            if p.matches(user_message):
                return p
        return None

    def _load(self) -> dict[str, Pipeline]:
        if not self._yaml_path.exists():
            log.warning(
                "pipelines.yaml not found, pipeline routing disabled",
                path=str(self._yaml_path),
            )
            return {}
        try:
            data = yaml.safe_load(self._yaml_path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError) as e:
            log.warning("pipelines.yaml load failed", err=str(e))
            return {}
        if not isinstance(data, dict):
            return {}

        out: dict[str, Pipeline] = {}
        for pid, body in data.items():
            if not isinstance(body, dict):
                continue
            try:
                out[pid] = Pipeline(
                    pipeline_id=str(pid),
                    description=str(body.get("description") or "").strip(),
                    trigger_keywords=tuple(
                        str(k) for k in (body.get("trigger_keywords") or [])
                    ),
                    sequence=tuple(
                        str(h) for h in (body.get("sequence") or [])
                    ),
                    checkpoint_after=tuple(
                        str(h) for h in (body.get("checkpoint_after") or [])
                    ),
                )
            except (TypeError, ValueError) as e:
                log.warning("pipeline yaml row malformed", pipeline_id=pid, err=str(e))
        return out


__all__ = ["Pipeline", "PipelineCatalog"]
