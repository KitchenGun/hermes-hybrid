"""Watcher runtime — polls watcher YAMLs and dispatches notifications.

See ``runner.py`` for the entry point. Watcher YAMLs live under
``profiles/<id>/watchers/*.yaml`` and are loaded by ``ProfileLoader``.
"""
from __future__ import annotations

from src.watcher.runner import WatcherRunner

__all__ = ["WatcherRunner"]
