"""Small registries that modules plug into: search providers and undo handlers."""

from collections.abc import Callable
from datetime import date
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session


class SearchHit(BaseModel):
    kind: str  # account | instrument | person | transaction | scenario | ...
    id: str
    title: str
    subtitle: str | None = None
    route: str  # client route to open, e.g. "/portfolio?inspect=acct:3"
    ref: str | None = None


SearchProvider = Callable[[Session, str, int], list[SearchHit]]
SEARCH_PROVIDERS: list[SearchProvider] = []


def search_provider(fn: SearchProvider) -> SearchProvider:
    SEARCH_PROVIDERS.append(fn)
    return fn


UndoHandler = Callable[[Session, dict[str, Any]], None]
UNDO_HANDLERS: dict[str, UndoHandler] = {}


def undo_handler(action: str) -> Callable[[UndoHandler], UndoHandler]:
    def deco(fn: UndoHandler) -> UndoHandler:
        UNDO_HANDLERS[action] = fn
        return fn

    return deco


# Shared timeline (LIFE-02) and "needs attention" items: modules contribute without the core knowing them.
# Timeline item: {"date", "kind", "title", "ref", "route"}; attention item: {"severity", "title", "detail", "route"}.
TimelineProvider = Callable[[Session, int], list[dict[str, Any]]]
TIMELINE_PROVIDERS: list[TimelineProvider] = []
AttentionProvider = Callable[[Session], list[dict[str, Any]]]
ATTENTION_PROVIDERS: list[AttentionProvider] = []


def timeline_provider(fn: TimelineProvider) -> TimelineProvider:
    TIMELINE_PROVIDERS.append(fn)
    return fn


def attention_provider(fn: AttentionProvider) -> AttentionProvider:
    ATTENTION_PROVIDERS.append(fn)
    return fn


# Calendar (LIFE-04): modules contribute dated items. A provider gets the inclusive date range and returns
# {"date", "title", "source", "category", "route"} plus optional "detail" and "private" (kept out of exports to
# outside services). Categories: finance | health | home | family | other.
CalendarProvider = Callable[[Session, date, date], list[dict[str, Any]]]
CALENDAR_PROVIDERS: list[CalendarProvider] = []


def calendar_provider(fn: CalendarProvider) -> CalendarProvider:
    CALENDAR_PROVIDERS.append(fn)
    return fn
