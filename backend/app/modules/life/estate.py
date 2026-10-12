"""The 'if something happens to me' package (EST-01/02): its content, as HTML or as an encrypted PDF.

One `collect()` feeds both renderings so they cannot drift apart. It reads only names, references and notes that
say where things are: no credentials, tokens or passwords exist in anything it touches.
"""

import secrets
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import AccessPermission, EncryptionMethod
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import ACCOUNT_KINDS, Entity
from app.modules.life.models import EstateItem, Policy
from app.modules.networth.service import Sheet
from app.modules.portfolio.valuation import Scope
from app.platform.db import today

FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF")
SECTIONS = {"contact": "Contacts", "instruction": "Instructions", "wish": "Wishes", "location": "Where things are"}


@dataclass
class Package:
    generated: date
    people: list[str] = field(default_factory=list)
    assets: list[dict[str, str]] = field(default_factory=list)
    debts: list[dict[str, str]] = field(default_factory=list)
    policies: list[dict[str, str]] = field(default_factory=list)
    items: list[dict[str, str]] = field(default_factory=list)


def collect(db: Session) -> Package:
    owners = {e.id: e.name for e in db.scalars(select(Entity).order_by(Entity.id))}
    pkg = Package(generated=today(), people=list(owners.values()))
    for i in Sheet(db, Scope(None)).items(today()):
        a = i.account
        row = {
            "name": a.name,
            "type": ACCOUNT_KINDS.get(a.kind, a.kind),
            "institution": a.institution.name if a.institution else "",
            "reference": a.external_ref or "",
            "value": f"{abs(i.value) if i.group == 'liabilities' else i.value:,.0f} €".replace(",", " "),
        }
        (pkg.debts if i.group == "liabilities" else pkg.assets).append(row)
    pkg.policies = [
        {
            "type": p.kind,
            "insurer": p.insurer,
            "policy": p.policy_number or "",
            "insured": owners.get(p.insured_entity_id or 0, ""),
            "beneficiaries": p.beneficiaries or "",
        }
        for p in db.scalars(select(Policy).order_by(Policy.kind))
    ]
    pkg.items = [
        {"section": e.section, "title": e.title, "body": e.body}
        for e in db.scalars(select(EstateItem).order_by(EstateItem.section, EstateItem.id))
    ]
    return pkg


def _font_file(bold: bool = False) -> Path | None:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return next((p for d in FONT_DIRS if (p := Path(d) / name).is_file()), None)


def render_pdf(pkg: Package, password: str) -> bytes:
    """The package as a PDF that needs `password` to open (AES-256); printing is allowed, editing is not."""
    pdf = FPDF(format="A4")
    pdf.set_encryption(
        owner_password=secrets.token_urlsafe(32),  # nobody needs the owner rights; it is not kept
        user_password=password,
        encryption_method=EncryptionMethod.AES_256,
        permissions=AccessPermission.PRINT_LOW_RES | AccessPermission.PRINT_HIGH_RES,
    )
    regular, bold = _font_file(), _font_file(True)
    if regular is not None:
        pdf.add_font("Body", fname=str(regular))
        pdf.add_font("Body", style="B", fname=str(bold or regular))
        family = "Body"

        def clean(s: str) -> str:
            return s
    else:  # no TTF installed: the built-in Helvetica only has Latin-1
        family = "Helvetica"

        def clean(s: str) -> str:
            return s.replace("€", "EUR").encode("latin-1", "replace").decode("latin-1")

    pdf.set_title("If something happens to me")
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()

    def h(text: str, size: int) -> None:
        pdf.set_font(family, "B", size)
        pdf.ln(3)
        pdf.cell(0, size * 0.6, clean(text), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(family, "", 10)

    def para(text: str) -> None:
        pdf.set_font(family, "", 10)
        pdf.multi_cell(0, 5, clean(text), new_x="LMARGIN", new_y="NEXT")

    def table(head: list[str], rows: list[list[str]], widths: tuple[int, ...]) -> None:
        pdf.set_font(family, "", 9)
        with pdf.table(col_widths=widths, text_align="LEFT", first_row_as_headings=True) as t:
            for r in [head, *rows]:
                row = t.row()
                for c in r:
                    row.cell(clean(c))
        pdf.set_font(family, "", 10)

    h("If something happens to me", 18)
    para(
        f"Generated {pkg.generated:%-d.%-m.%Y}. Values are estimates. This document contains no passwords; "
        "it says where things are."
    )
    if pkg.people:
        h("People", 13)
        para(", ".join(pkg.people))
    h("Accounts and assets", 13)
    heads = ["Account", "Type", "Institution", "Reference", "Value"]
    cols = ("name", "type", "institution", "reference", "value")
    widths = (45, 30, 40, 40, 25)
    table(heads, [[a[c] for c in cols] for a in pkg.assets], widths) if pkg.assets else para("None recorded.")
    h("Debts", 13)
    table(heads, [[a[c] for c in cols] for a in pkg.debts], widths) if pkg.debts else para("None recorded.")
    h("Insurance", 13)
    if pkg.policies:
        table(
            ["Type", "Insurer", "Policy", "Insured", "Beneficiaries"],
            [[p["type"], p["insurer"], p["policy"], p["insured"], p["beneficiaries"]] for p in pkg.policies],
            (25, 40, 35, 40, 40),
        )
    else:
        para("None recorded.")
    h("Contacts, instructions and wishes", 13)
    if not pkg.items:
        para("None recorded yet.")
    for it in pkg.items:
        pdf.set_font(family, "B", 10)
        pdf.multi_cell(
            0, 6, clean(f"{SECTIONS.get(it['section'], it['section'])}: {it['title']}"), new_x="LMARGIN", new_y="NEXT"
        )
        para(it["body"])
        pdf.ln(1)
    return bytes(pdf.output())
