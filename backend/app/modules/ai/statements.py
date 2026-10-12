"""Read attachments with the local model: what the document is, then its values unit by unit, then checks.

Holdings statements (any broker, bank, fund company or pension insurer; PDF, scan, screenshot, CSV or Excel)
become rows of [name, ISIN, quantity, price, price currency, value, value currency, purchase value]. Every row is
checked: its numbers against the page text when there is one, quantity × price against the value, and all rows
plus cash against the statement total. Nothing is booked until a person confirms (see `apply_holdings`).
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.ai import extract, llm
from app.modules.ai.attachments import Unit, split
from app.modules.core.models import Account, Instrument

ZERO = Decimal(0)
CENT = Decimal("0.01")
KINDS = {
    "holdings": "Holdings / portfolio statement",
    "statement": "Account balance statement",
    "loan_letter": "Loan letter",
    "pension_record": "Pension record (työeläkeote)",
    "transactions": "Transaction list",
    "factsheet": "Fund factsheet / KID",
    "other": "Something else",
}
# One holding row costs about 65 output tokens: ~28 rows fit 2 048 tokens, ~56 fit 4 096; longer parts are split.
CLASSIFY_CHARS = 6000  # the opening pages the classifier sees together (same size as one holdings part)
COLUMNS = ["name", "isin", "quantity", "price", "price_ccy", "value", "value_ccy", "cost"]

CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "institution": {"type": ["string", "null"]},
        "account": {"type": ["string", "null"]},
        "as_of": {"type": ["string", "null"]},
        "language": {"type": ["string", "null"]},
    },
    "required": ["kind", "institution", "account", "as_of", "language"],
}
HOLDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "account": {"type": ["string", "null"]},
        "as_of": {"type": ["string", "null"]},
        "rows": {
            "type": "array",
            "items": {"type": "array", "items": {"type": ["string", "number", "null"]}, "minItems": 8, "maxItems": 8},
        },
        "cash": {"type": ["number", "null"]},
        "cash_ccy": {"type": ["string", "null"]},
        "total": {"type": ["number", "null"]},
        "total_ccy": {"type": ["string", "null"]},
    },
    "required": ["account", "as_of", "rows", "cash", "cash_ccy", "total", "total_ccy"],
}
FIELDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {"value": {"type": ["string", "number", "null"]}, "quote": {"type": "string"}},
                "required": ["value", "quote"],
            },
        }
    },
    "required": ["fields"],
}

RULES = (
    "Numbers: dot as decimal separator, no thousands separators (Finnish 1 234,56 → 1234.56; a minus may be "
    "written −). In Finnish text the comma is ALWAYS the decimal separator, also with three or four decimals: "
    "fund units 1 450,000 → 1450.0 and 2 341,8820 → 2341.882; spaces separate thousands. Table columns are "
    "separate numbers: in a row 'Kone Oyj FI0009013403 468 368,85 EUR 172 621,80' the quantity is 468 and the "
    "price 368.85; never join two columns into one number. Dates: YYYY-MM-DD. "
    "Currencies: ISO codes (EUR, USD, SEK…). Copy names and identifiers exactly. "
    "Never guess: use null for anything not shown."
)
CLASSIFY_PROMPT = (
    "You classify financial documents for a Finnish household. holdings = a list of securities, funds or other "
    "investments with quantities or values (portfolio, custody or fund statement, pension or insurance savings "
    "with funds, broker app screenshot); an account overview that lists positions (osakesäästötili, "
    "arvo-osuustili, sijoitustili: Osakkeet, Rahastot, Omistus, kpl) is holdings even when it opens with the "
    "account's total value. statement = a single account balance with no positions. loan_letter = loan terms or a rate "
    "change. pension_record = an earnings-related pension record (työeläkeote). transactions = a list of trades or "
    "payments. factsheet = a one- or two-page fund document (factsheet, KID, esite) with the fund's asset allocation, "
    "top holdings, regions or sectors and ongoing charges, not a list of what the reader owns. "
    "as_of is the date the values apply to. " + RULES
)
HOLDINGS_PROMPT = (
    "Extract every holding on this part of the statement. One row per holding: "
    "[name, ISIN or null, quantity (units/shares) or null, unit price or null, price currency, market value, "
    "market value currency, purchase value (hankinta-arvo) in the market value currency or null]. "
    "Change columns (muutos, kehitys, tuotto: gains in % or in money, may be positive) are never the price, value "
    "or quantity; quantity × price must equal the market value. A lone dash (-) is an empty cell, not a minus. "
    "Skip subtotal, total and header lines. Cash (käteinen, likvidit varat, saldo) is never a holding row: put "
    "cash balances in cash, the statement's grand total (if shown on "
    "this part) in total. account = the account or portfolio number this part belongs to. " + RULES
)


@dataclass
class Result:
    kind: str
    header: dict[str, Any]
    rows: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    fields: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    notes: list[str] = field(default_factory=list)


def _messages(system: str, unit: Unit, context: str) -> list[dict[str, Any]]:
    if unit.is_image:
        content: Any = [
            {"type": "text", "text": f"{context}\nThis is {unit.label} of the document (an image)."},
            {"type": "image_url", "image_url": {"url": unit.data_url()}},
        ]
    else:
        content = f"{context}\nThis is {unit.label} of the document.\n\nTEXT:\n{unit.text}"
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


def _ask(
    db: Session, system: str, unit: Unit, context: str, schema: dict[str, Any], client: Any
) -> tuple[dict[str, Any], llm.Reply]:
    r = llm.complete(
        db,
        _messages(system, unit, context),
        vision=unit.is_image,
        schema=schema,
        temperature=0.0,
        background=True,
        label=f"statement {unit.label}",
        client=client,
    )
    return llm.parse_json(r.content), r


def classify(db: Session, units: list[Unit], client: Any = None) -> tuple[dict[str, Any], str]:
    """What the document is, from its opening: a bank's account overview often shows only the account total on
    page 1 and lists the positions from page 2 on, so text pages are read together up to CLASSIFY_CHARS, and a
    scan whose first page looks like a single balance gets its second page read too."""
    first = units[0]
    unit = first
    if not first.is_image:
        texts = [first.text or ""]
        for u in units[1:]:
            if u.is_image or sum(map(len, texts)) + len(u.text or "") > CLASSIFY_CHARS:
                break
            texts.append(u.text or "")
        if len(texts) > 1:
            unit = Unit(f"the first {len(texts)} parts", text="\n".join(texts))
    data, r = _ask(db, CLASSIFY_PROMPT, unit, "Classify this document.", CLASSIFY_SCHEMA, client)
    if data.get("kind") in ("statement", "other") and first.is_image and len(units) > 1:
        more, r2 = _ask(db, CLASSIFY_PROMPT, units[1], "Classify this document.", CLASSIFY_SCHEMA, client)
        if more.get("kind") == "holdings":
            data = {**more, **{k: v for k, v in data.items() if v and k != "kind"}}
            r = r2
    if data.get("kind") not in KINDS:
        data["kind"] = "other"
    return data, r.model


# ---- numbers ---------------------------------------------------------------------------------------


def num(v: Any) -> Decimal | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return Decimal(str(v))
    return extract.parse_number(v)


GROUPED = re.compile(r"(?<![\d,.])[-−]?\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d+)?(?![\d])")
PLAIN = re.compile(r"(?<![\d])[-−]?\d[\d.,']*\d|(?<![\d])\d")


def numbers_in(text: str) -> list[Decimal]:
    """Every number on the page: single tokens, and space-grouped thousands (Finnish 6 397,30) as well."""
    out = []
    for rx in (PLAIN, GROUPED):
        for m in rx.finditer(text):
            n = extract.parse_number(m.group(0).replace("'", ""))
            if n is not None:
                out.append(abs(n))
    return out


def _in_text(v: Decimal | None, nums: list[Decimal]) -> bool:
    if v is None:
        return True
    a = abs(v)
    return any(abs(x - a) <= Decimal("0.005") for x in nums)


NUMBERS_ONLY = re.compile(r"(?:[\d\s.,%+−-]|€|\$|\b(?:EUR|USD|SEK|NOK|DKK|GBP|CHF|kpl|pcs|st)\b)*", re.I)


def _named_in(row: dict[str, Any], text: str) -> bool:
    """The holding is on the page: its ISIN, or the first word of its name as a whole word."""
    low = unicodedata.normalize("NFKC", text).lower()  # ligatures (ﬀ) as letters
    if row["isin"] and row["isin"].lower() in low.replace(" ", ""):
        return True
    word = next((w for w in re.findall(r"\w+", row["name"].lower()) if len(w) >= 2), "")
    return bool(word) and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", low) is not None


def _repair_from_line(x: dict[str, Any], text: str) -> None:
    """Pages without column headers confuse the model about which number is which. Find the holding's own line
    and pick the printed numbers that fit together: quantity × price = value (quantity may be shown rounded)."""
    key = x["isin"] or x["name"][:24]
    lines = text.splitlines()
    i = next((i for i, ln in enumerate(lines) if key and key.lower() in ln.lower()), None)
    if i is None:
        return
    rest = lines[i][lines[i].lower().find(key.lower()) + len(key) :]
    if len(numbers_in(rest)) < 3:  # a row printed over several lines (app views: one number per line)
        for ln in lines[i + 1 : i + 9]:
            if not NUMBERS_ONLY.fullmatch(ln):
                break
            rest += "\n" + ln
    cand = sorted(set(numbers_in(rest)), reverse=True)
    if len(cand) < 3:
        return
    q0, p0, v0 = x["quantity"], x["price"], x["value"]
    tagged = {
        n
        for m in re.finditer(
            r"(?<![\d,.])([-−]?\d{1,3}(?:[ \u00a0]\d{3})*(?:[.,]\d+)?)\s*(?:EUR|USD|SEK|NOK|DKK|GBP|CHF|€|\$)", rest
        )
        for n in numbers_in(m.group(1))
    }
    if (
        q0 is not None
        and p0 is not None
        and v0 is not None
        and _in_text(q0, cand)
        and _in_text(v0, cand)
        and abs(q0 * p0 - v0) <= max(Decimal(1), v0 * Decimal("0.005"))
        and not (q0 in tagged and p0 not in tagged)  # quantity and price not swapped
    ):
        return  # already consistent and printed
    # a number followed by a currency code ("165,36 EUR") is a price or value, never a quantity
    best: tuple[Decimal, Decimal, Decimal] | None = None
    best_score: tuple[Any, ...] = ()
    for v in cand:
        for p in cand:
            if p == v or p == 0:
                continue
            for q in cand:
                if q in (v, p) or q == 0:
                    continue
                # a rounded quantity (83 for 83.003) still multiplies to within half a unit of price
                # allow a rounded quantity (83 for 83.003) and a price shown to the cent (11,89 for 11.886)
                if abs(q * p - v) > Decimal("0.01") + min(p * Decimal("0.5"), v * Decimal("0.005")) + q * Decimal(
                    "0.005"
                ):
                    continue
                score = (v, q not in tagged, p in tagged, q == q0, -abs(q.as_tuple().exponent))  # type: ignore[arg-type]
                if best is None or score > best_score:
                    best, best_score = (q, p, v), score
    if best is not None and (best[0], best[1], best[2]) != (q0, p0, v0):
        x["quantity"], x["price"], x["value"] = best
        x["fixed"] = f"quantity, price and value re-read from the line: {best[0]} × {best[1]} = {best[2]}"


def _derive_from_page(x: dict[str, Any], nums: list[Decimal]) -> None:
    """A field the page does not show (e.g. quantity and price glued into 468.36885) is rebuilt from the other two
    when exactly that number is printed on the page: quantity = value / price, price = value / quantity."""
    if (x["price_ccy"] or "EUR") != (x["value_ccy"] or "EUR"):
        return
    q, p, v = x["quantity"], x["price"], x["value"]
    for name, derived in (
        ("quantity", v / p if v is not None and p else None),
        ("price", v / q if v is not None and q else None),
        ("value", q * p if q is not None and p is not None else None),
    ):
        if derived is None or (x[name] is not None and _in_text(x[name], nums)):
            continue
        tol = max(Decimal("0.006"), abs(derived) * Decimal("0.0005"))
        hits = {n for n in nums if abs(n - abs(derived)) <= tol}
        if len(hits) == 1:
            found = hits.pop()
            x["fixed"] = f"{name} {x[name]} corrected to {found} (printed on the page, matches the other columns)"
            x[name] = found
            return


def _snap(v: Decimal, nums: list[Decimal]) -> Decimal | None:
    """A dropped or flipped last digit (147850.22 read as 147850.2): the one page number within 0.01 % of it."""
    a = abs(v)
    near = {x for x in nums if abs(x - a) <= max(Decimal("0.1"), a * Decimal("0.0001"))}
    if len(near) == 1:
        x = near.pop()
        return x if v >= 0 else -x
    return None


# ---- holdings ---------------------------------------------------------------------------------------


CASH_NAME = re.compile(r"^(k[aä]te|cash|likvid|rahatili|tilin saldo|saldo|kassa|käyttövara)", re.I)


def is_cash(row: dict[str, Any]) -> bool:
    """Models sometimes list the cash balance as a holding; it has a cash-like name and no ISIN or units."""
    return bool(CASH_NAME.search(row["name"])) and not row["isin"] and row["quantity"] in (None, row["value"])


def _row(raw: list[Any]) -> dict[str, Any] | None:
    raw = (list(raw) + [None] * 8)[:8]
    name = str(raw[0] or "").strip()
    if not name or re.search(r"^(yhteensä|total|summa|totalt|subtotal)\b", name, re.I):
        return None
    isin = str(raw[1] or "").strip().upper().replace(" ", "") or None
    if isin and not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", isin):
        isin = None
    return {
        "name": name[:255],
        "isin": isin,
        "quantity": num(raw[2]),
        "price": num(raw[3]),
        "price_ccy": (str(raw[4]).upper()[:3] if raw[4] else None),
        "value": num(raw[5]),
        "value_ccy": (str(raw[6]).upper()[:3] if raw[6] else None),
        "cost": num(raw[7]),
    }


def holdings(
    db: Session, units: list[Unit], header: dict[str, Any], client: Any = None, progress: Any = None
) -> Result:
    res = Result("holdings", dict(header))
    context = (
        f"A holdings statement from {header.get('institution') or 'an unknown institution'}"
        + (f", account {header['account']}" if header.get("account") else "")
        + (f", values as of {header['as_of']}" if header.get("as_of") else "")
        + "."
    )
    queue = list(units)
    rendered: set[str] = set()
    done = 0
    cash: dict[str, tuple[Decimal, str]] = {}
    totals: dict[str, tuple[Decimal, str]] = {}
    while queue:
        unit = queue.pop(0)
        data, r = _ask(db, HOLDINGS_PROMPT, unit, context, HOLDINGS_SCHEMA, client)
        res.model = r.model
        if r.finish_reason == "length":  # ran out of output room: read the halves instead
            parts = split(unit)
            if len(parts) > 1:
                queue = parts + queue
                continue
            res.notes.append(f"{unit.label}: too many rows for one answer; some may be missing")
        rows = [x for x in (_row(raw) for raw in data.get("rows") or []) if x]
        if not rows and unit.as_image and len(numbers_in(unit.text or "")) >= 12 and unit.label not in rendered:
            # many numbers but no rows: the text layer may have scrambled a table; read the page as an image
            rendered.add(unit.label)
            queue = unit.as_image() + queue
            continue
        acct = account_key(data.get("account") or header.get("account"))
        nums = numbers_in(unit.text) if unit.text else []
        for x in [r for r in rows if is_cash(r)]:
            rows.remove(x)
            if data.get("cash") is None and x["value"] is not None:
                data["cash"], data["cash_ccy"] = x["value"], x["value_ccy"] or "EUR"
        # small models copy the prompt's example or invent a holding: neither its name nor its value is printed
        # (a value check too, as text layers break names: "Wul�-Yhtiöt" for Wulff-Yhtiöt)
        for x in [r for r in rows if unit.text is not None and not _named_in(r, unit.text)]:
            if x["value"] is None or _in_text(x["value"], nums):
                continue
            rows.remove(x)
            res.notes.append(f"{unit.label}: {x['name']} is not on the page; left out")
        for x in rows:
            x["account"] = acct
            x["source"] = unit.label
            x["read_from"] = "image" if unit.is_image else "text"
            if any(x[k] is not None and x[k] < 0 for k in ("price", "value")) and (x["quantity"] or 1) > 0:
                x["price"], x["value"] = (abs(x[k]) if x[k] is not None else None for k in ("price", "value"))
                x["fixed"] = "price and value cannot be negative (a dash in an empty column is not a minus)"
            if not unit.is_image:
                _repair_from_line(x, unit.text or "")
                for k in ("quantity", "price", "value", "cost"):
                    if x[k] is not None and not _in_text(x[k], nums) and (fixed := _snap(x[k], nums)) is not None:
                        x["fixed"] = f"{k} {x[k]} corrected to {fixed} as printed on the page"
                        x[k] = fixed
                _derive_from_page(x, nums)
            x["in_text"] = (
                None if unit.is_image else all(_in_text(x[k], nums) for k in ("quantity", "price", "value", "cost"))
            )
            res.rows.append(x)
        if data.get("cash") is not None:
            cash[acct] = (num(data["cash"]) or ZERO, str(data.get("cash_ccy") or "EUR").upper()[:3])
        if data.get("total") is not None:
            totals[acct] = (num(data["total"]) or ZERO, str(data.get("total_ccy") or "EUR").upper()[:3])
        if not res.header.get("as_of") and data.get("as_of"):
            res.header["as_of"] = data["as_of"]
        done += 1
        if progress:
            progress(done, done + len(queue))
    keys = {x["account"] for x in res.rows} | set(cash) | set(totals)
    if keys and not any(re.fullmatch(r"\d{4,}", k) for k in keys):  # names only ("AOT", "AOT Virtanen…"): one account
        main = account_key(header.get("account")) or next(iter(keys))
        for x in res.rows:
            x["account"] = main
        cash = {main: next(iter(cash.values()))} if cash else {}
        totals = {main: next(iter(totals.values()))} if totals else {}
    res.rows = _dedupe(res.rows)
    res.header["cash"] = {a: {"amount": str(v), "ccy": c} for a, (v, c) in cash.items()}
    res.header["totals"] = {a: {"amount": str(v), "ccy": c} for a, (v, c) in totals.items()}
    d = statement_date(res.header.get("as_of"))
    res.header["as_of"] = d.isoformat() if d else None
    check(db, res)
    match_instruments(db, res.rows)
    return res


def statement_date(v: Any) -> date | None:
    """The date values apply to is never in the future: a browser print footer (10/11/26) is month/day, so a
    future date is read again with day and month swapped, and dropped (to be given by hand) if that fails too."""
    d = extract.parse_date(v)
    today = date.today()  # noqa: DTZ011
    if d is None or d <= today:
        return d
    if d.day <= 12 and (swapped := date(d.year, d.day, d.month)) <= today:
        return swapped
    return None


def account_key(v: Any) -> str:
    """'Arvo-osuustili 12345678' and '12345678' are the same account: compare by the number when there is one."""
    text = str(v or "").strip()
    digits = re.sub(r"\D", "", text)
    return digits if len(digits) >= 4 else text


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overlapping tiles and tables continued on the next page repeat rows, sometimes partly: keep one, filling
    gaps from the other reading. Same holding with two different quantities = two lots, both kept."""
    out: list[dict[str, Any]] = []
    for x in rows:
        key = (x["account"], x["isin"] or re.sub(r"\W+", "", x["name"].lower()))
        prev = next(
            (
                p
                for p in out
                if (p["account"], p["isin"] or re.sub(r"\W+", "", p["name"].lower())) == key
                and (p["quantity"] is None or x["quantity"] is None or p["quantity"] == x["quantity"])
            ),
            None,
        )
        if prev is None:
            out.append(x)
            continue
        for k, v in x.items():
            if prev.get(k) is None and v is not None:
                prev[k] = v
        if x.get("in_text"):  # confirmed by the text of either part
            prev["in_text"] = True
    return out


def check(db: Session, res: Result, fx_client: Any = None) -> None:
    """Arithmetic and totals checks; each row gets `status` ok | check and the reasons."""
    from app.modules.pricing.service import PriceBook, ensure_fx

    on = extract.parse_date(res.header.get("as_of")) or date.today()  # noqa: DTZ011
    ccys = {c for x in res.rows for c in (x["price_ccy"], x["value_ccy"]) if c}
    ccys |= {t["ccy"] for t in (res.header.get("totals") or {}).values()}
    ensure_fx(db, ccys, on, fx_client)
    book = PriceBook(db, set())

    def eur(amount: Decimal | None, ccy: str | None) -> Decimal | None:
        if amount is None:
            return None
        if not ccy or ccy == "EUR":
            return amount
        try:
            rate, _ = book.eur_per(ccy, on)
        except LookupError:
            return None
        return amount * rate

    for x in res.rows:
        reasons = []
        if x["in_text"] is False:
            reasons.append("a number is not in the page text")
        q, p, v = x["quantity"], x["price"], x["value"]
        if q is not None and p is not None and v is not None and q != 0 and p != 0:
            # "1 450,000" units read as 1450000: value ÷ price says where the decimal comma was
            implied_q = v / p
            for k in range(1, 7):
                scaled = q / (Decimal(10) ** k)
                if abs(scaled - implied_q) <= max(Decimal("0.001"), abs(implied_q) * Decimal("0.002")):
                    x["fixed"] = f"quantity {q} read as {scaled.normalize():f} (decimal comma)"
                    x["quantity"] = q = scaled.normalize()
                    break
        if q is not None and p is not None and v is not None and q != 0:
            pc, vc = x["price_ccy"] or "EUR", x["value_ccy"] or "EUR"
            local = q * p
            in_eur = eur(local, pc)

            def near(a: Decimal | None, b: Decimal, rel: str) -> bool:
                return a is not None and abs(a - b) <= max(Decimal(1), abs(b) * Decimal(rel))

            if pc == vc and not near(local, v, "0.01") and pc != "EUR" and near(in_eur, v, "0.04"):
                x["value_ccy"], vc = "EUR", "EUR"  # the value column is in euros, labelled with the trading currency
                x["fixed"] = "value currency read as EUR (matches the ECB rate)"
            elif pc != vc and not near(eur(local, pc), eur(v, vc) or v, "0.04") and near(local, v, "0.01"):
                x["value_ccy"], vc = pc, pc  # the value is in the trading currency after all
                x["fixed"] = f"value currency read as {pc}"
            target = eur(v, vc)
            if in_eur is not None and target is not None:
                same = pc == vc
                if not near(local if same else in_eur, v if same else target, "0.01" if same else "0.04"):
                    shown = (local if same else in_eur).quantize(CENT)
                    reasons.append(f"quantity × price = {shown}, value says {(v if same else target).quantize(CENT)}")
            elif pc == vc and not near(local, v, "0.01"):
                reasons.append(f"quantity × price = {local.quantize(CENT)}, value says {v.quantize(CENT)}")
        if v is None and q is None:
            reasons.append("no quantity or value")
        elif q is None and any(o["quantity"] is not None for o in res.rows if o["account"] == x["account"]):
            reasons.append("no quantity (the row may continue on the next page)")
        x["value_eur"] = eur(v, x["value_ccy"])
        x["status"] = "check" if reasons else "ok"
        x["reasons"] = reasons
    for acct, t in (res.header.get("totals") or {}).items():
        rows = [x for x in res.rows if x["account"] == acct]
        cash = (res.header.get("cash") or {}).get(acct)
        parts = [eur(x["value"], x["value_ccy"]) for x in rows]
        if any(pp is None for pp in parts):
            continue
        s = sum((pp for pp in parts if pp is not None), ZERO)
        total = eur(Decimal(t["amount"]), t["ccy"])
        if total is None:
            continue

        def matches(a: Decimal, b: Decimal) -> bool:
            return abs(a - b) <= max(Decimal(1), abs(b) * Decimal("0.005"))

        what = "holdings + cash = statement total"
        if cash and matches(s, total) and not matches(s + (eur(Decimal(cash["amount"]), cash["ccy"]) or ZERO), total):
            what = "holdings = statement total (cash shown separately)"  # e.g. Nordea's "Osakkeet, arvo yhteensä"
        elif cash:
            s += eur(Decimal(cash["amount"]), cash["ccy"]) or ZERO
        ok = matches(s, total)
        res.checks.append(
            {
                "account": acct,
                "what": what,
                "computed": str(s.quantize(CENT)),
                "stated": str(total.quantize(CENT)),
                "ok": ok,
            }
        )
        if not ok:
            res.notes.append(
                f"Account {acct or '(unnamed)'}: rows add up to {s.quantize(CENT)} but the statement total is "
                f"{total.quantize(CENT)}; a row may be missing or misread"
            )


def match_instruments(db: Session, rows: list[dict[str, Any]]) -> None:
    for x in rows:
        inst = None
        if x["isin"]:
            inst = db.scalar(select(Instrument).where(Instrument.isin == x["isin"]))
        if inst is None:
            inst = db.scalar(
                select(Instrument).where(func.lower(Instrument.name) == x["name"].lower(), Instrument.type != "cash")
            )
        x["instrument_id"] = inst.id if inst else None
        x["instrument"] = inst.name if inst else None


def suggest_account(db: Session, header: dict[str, Any], rows: list[dict[str, Any]]) -> int | None:
    hints = {re.sub(r"\D", "", str(header.get("account") or ""))} | {re.sub(r"\D", "", r["account"]) for r in rows}
    hints.discard("")
    inst_name = str(header.get("institution") or "").lower()
    best, score = None, 0
    for a in db.scalars(select(Account).where(Account.closed_on.is_(None))):
        s = 0
        ref = re.sub(r"\D", "", a.external_ref or "")
        if ref and any(ref in h or h.endswith(ref[-6:]) for h in hints if len(h) >= 4):
            s += 5
        name = f"{a.name} {a.institution.name if a.institution else ''}".lower()
        s += sum(1 for w in re.findall(r"\w{4,}", inst_name) if w in name)
        if s > score:
            best, score = a.id, s
    return best


# ---- other document kinds ----------------------------------------------------------------------------


def fields_from_units(db: Session, kind: str, units: list[Unit], client: Any = None) -> tuple[dict[str, Any], str]:
    """Statement / loan letter / pension record values (see extract.FIELDS), from text or from page images."""
    spec = "; ".join(f"{n} ({t}: {d})" for n, (t, d) in extract.FIELDS[kind].items())
    system = (
        f"Extract these fields from a {KINDS[kind].lower()}: {spec}. Reply as "
        '{"fields": {name: {"value": ..., "quote": "exact text the value was read from"}}}. ' + RULES
    )
    text_units = [u for u in units if not u.is_image]
    if text_units:
        text = "\n".join(u.text or "" for u in text_units)[: extract.MAX_CHARS]
        data, r = _ask(db, system, Unit("the whole document", text=text), "", FIELDS_SCHEMA, client)
        return extract.verify(kind, dict(data.get("fields") or {}), text), r.model
    merged: dict[str, Any] = {}
    model = ""
    for u in units[:4]:
        data, r = _ask(db, system, u, "", FIELDS_SCHEMA, client)
        model = r.model
        for k, v in (data.get("fields") or {}).items():
            merged.setdefault(k, v)
    out = extract.verify(kind, merged, "")
    for v in out.values():
        v["found"] = None  # read from an image: no text to check against
    return out, model
