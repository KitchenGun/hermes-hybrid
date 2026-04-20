"""Structured logging with task_id propagation (R14).

Use `get_logger(__name__)` anywhere. Task-scoped fields are picked up from
contextvars so callers never have to thread them through function args.
"""
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

import structlog

task_id_var: ContextVar[str] = ContextVar("task_id", default="-")
user_id_var: ContextVar[str] = ContextVar("user_id", default="-")


def _add_context(_, __, event_dict):  # pragma: no cover - simple processor
    event_dict.setdefault("task_id", task_id_var.get())
    event_dict.setdefault("user_id", user_id_var.get())
    return event_dict


def setup_logging(level: str = "INFO", json: bool = False) -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
    )
    renderer = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(colors=False)
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            _add_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    return structlog.get_logger(name)


class bind_task_id:
    """Context manager that pins task_id + user_id into contextvars for the duration."""

    def __init__(self, task_id: str, user_id: str = "-"):
        self.task_id = task_id
        self.user_id = user_id
        self._tok_t = None
        self._tok_u = None

    def __enter__(self):
        self._tok_t = task_id_var.set(self.task_id)
        self._tok_u = user_id_var.set(self.user_id)
        return self

    def __exit__(self, *exc):
        if self._tok_t is not None:
            task_id_var.reset(self._tok_t)
        if self._tok_u is not None:
            user_id_var.reset(self._tok_u)
