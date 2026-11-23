"""Which Specivo project a Hermes session recalls from and remembers to."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath

from ..config import Settings


@dataclass(frozen=True)
class Scope:
    project: str | None
    #: Which setting chose it: ``memory.project``, ``memory.cwd_projects``, ``memory.chat_projects``,
    #: ``default_project``, or ``None`` when nothing did.
    source: str | None


def _under(cwd: PurePath, prefix: PurePath) -> bool:
    return cwd == prefix or prefix in cwd.parents


def resolve_scope(settings: Settings, *, cwd: str | None, platform: str | None, chat_id: str | None) -> Scope:
    """``memory.project``; else the longest ``memory.cwd_projects`` prefix of *cwd*; else
    ``memory.chat_projects`` for ``<platform>:<chat_id>``; else ``default_project``."""
    memory = settings.memory
    if memory.project:
        return Scope(memory.project, "memory.project")
    if cwd:
        here = PurePath(cwd)
        matches = [
            (len(PurePath(prefix).parts), project)
            for prefix, project in memory.cwd_projects.items()
            if _under(here, PurePath(prefix))
        ]
        if matches:
            return Scope(max(matches)[1], "memory.cwd_projects")
    if platform and chat_id:
        project = memory.chat_projects.get(f"{platform}:{chat_id}")
        if project:
            return Scope(project, "memory.chat_projects")
    if settings.default_project:
        return Scope(settings.default_project, "default_project")
    return Scope(None, None)
