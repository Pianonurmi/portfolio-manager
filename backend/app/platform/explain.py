"""Provenance for every displayed figure (UX-01, ADR-16).

A figure travels as {"value": "123.45", "currency": "EUR", "ref": "kind:arg:arg"}.
`GET /api/explain/{ref}` resolves the ref through the explainer its module registered
and returns a tree: formula, inputs (each with its own ref), and source rows.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

Source = Literal["ledger", "price", "fx", "import", "manual", "rule", "derived"]


class Figure(BaseModel):
    value: Decimal
    currency: str | None = "EUR"
    ref: str | None = None
    stale: str | None = None  # human text, e.g. "NAV 7.10."
    estimated: bool = False


class ExplainNode(BaseModel):
    label: str
    value: str | None = None
    formula: str | None = None
    source: Source | None = None
    ref: str | None = None  # expandable lazily via /api/explain/{ref}
    stale: bool = False
    note: str | None = None
    children: list["ExplainNode"] = []


Explainer = Callable[..., ExplainNode]
EXPLAINERS: dict[str, Explainer] = {}


def explainer(kind: str) -> Callable[[Explainer], Explainer]:
    def deco(fn: Explainer) -> Explainer:
        EXPLAINERS[kind] = fn
        return fn

    return deco


def fig(value: Decimal, ref: str | None = None, currency: str | None = "EUR", **kw: object) -> Figure:
    return Figure(value=value, ref=ref, currency=currency, **kw)
