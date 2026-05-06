"""Session Importer — re-export of :mod:`src.core.session_importer`.

The diagram places the importer in the Integration Layer; the actual
implementation lives in `core/` because it depends on
:class:`~src.core.experience_logger.ExperienceLogger`. This module
re-exports the public API so callers can ``from src.integration import
import_sessions`` consistently with the other 4 components.
"""
from src.core.session_importer import import_sessions, session_to_record

__all__ = ["import_sessions", "session_to_record"]
