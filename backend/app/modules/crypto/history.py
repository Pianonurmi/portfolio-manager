"""Crypto transaction history (ING-11): exchange and wallet exports → one staged row per economic event.

Supported: Kraken ledger export (CSV and the Ledgers API), Binance "Transaction History" statement, Ledger Live
operations export and a generic CSV for anything else. Each parser emits events with signed legs per asset:

  trade       assets in and out (fiat, crypto or both); Finnish tax treats every swap as a disposal
  deposit     assets arriving from outside the exchange (fiat, or crypto from another wallet)
  withdrawal  assets leaving (a network fee is its own disposal)
  reward      staking, earn interest, airdrops: capital income at market value, which is also the cost
  internal    moves between wallets of the same venue (no effect; ignored)

Booking turns events into ledger transactions valued in EUR, see `specs_for`.
"""

import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.modules.crypto.providers import kraken_asset

ZERO = Decimal(0)
SOURCES = {"kraken", "binance", "ledgerlive", "crypto-csv"}
KRAKEN_IGNORED_ASSETS = {"KFEE"}  # fee credits, not an asset


class FormatError(ValueError):
    pass


@dataclass
class Row:
    """Same shape as the Nordnet adapter's rows, so staging treats every source alike."""

    row_no: int
    raw: dict[str, Any]
    external_id: str | None
    content_hash: str
    parsed: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class Leg:
    asset: str
    amount: Decimal  # signed change before the fee
    fee: Decimal = ZERO  # positive, in the same asset
    is_fee: bool = False  # the whole leg is a fee (e.g. Binance BNB fee rows)

    @property
    def net(self) -> Decimal:
        return self.amount - self.fee


def _dec(v: Any) -> Decimal:
    s = str(v if v is not None else "").strip().replace(" ", "")
    if not s:
        return ZERO
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise FormatError(f"not a number: {v!r}") from exc


def _dt(v: str) -> datetime:
    v = v.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y",
    ):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError as exc:
        raise FormatError(f"unrecognised time {v!r}") from exc


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _event(
    when: datetime, event: str, legs: list[Leg], raw_type: str, description: str, **extra: Any
) -> dict[str, Any]:
    merged: dict[tuple[str, bool], Leg] = {}
    for leg in legs:
        k = (leg.asset, leg.is_fee)
        if k in merged:
            m = merged[k]
            m.amount += leg.amount
            m.fee += leg.fee
        else:
            merged[k] = Leg(leg.asset, leg.amount, leg.fee, leg.is_fee)
    return {
        "date": when.date().isoformat(),
        "time": when.isoformat(),
        "seq": when.hour * 3600 + when.minute * 60 + when.second,
        "event": event,
        "legs": [
            {"asset": x.asset, "amount": str(x.amount), "fee": str(x.fee), "is_fee": x.is_fee}
            for x in merged.values()
            if x.amount or x.fee
        ],
        "raw_type": raw_type,
        "portfolio": "",
        "description": description,
        **extra,
    }


def legs_of(parsed: dict[str, Any]) -> list[Leg]:
    return [Leg(x["asset"], Decimal(x["amount"]), Decimal(x["fee"]), bool(x.get("is_fee"))) for x in parsed["legs"]]


def nets(parsed: dict[str, Any]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = defaultdict(Decimal)
    for leg in legs_of(parsed):
        out[leg.asset] += leg.net
    return dict(out)


def _read(text: str) -> tuple[list[str], list[dict[str, str]]]:
    first = text.splitlines()[0] if text else ""
    delim = ";" if first.count(";") > first.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    heads = [h.strip().strip('"').lower() for h in (reader.fieldnames or [])]
    rows = []
    for r in reader:
        rows.append({h.strip().strip('"').lower(): (v or "").strip() for h, v in r.items() if h is not None})
    return heads, rows


def sniff(text: str) -> str | None:
    first = (text.splitlines()[0] if text else "").lower().replace('"', "")
    heads = {h.strip() for h in re.split(r"[;,]", first)}
    if {"txid", "refid", "time", "type", "asset", "amount", "fee", "balance"} <= heads:
        return "kraken"
    if {"utc_time", "operation", "coin", "change"} <= heads:
        return "binance"
    if {"operation date", "currency ticker", "operation type", "operation amount"} <= heads:
        return "ledgerlive"
    if {"date", "type", "asset", "amount"} <= heads:
        return "crypto-csv"
    return None


# ---- Kraken ------------------------------------------------------------------------------------------

KRAKEN_TRADE = {"trade", "spend", "receive", "sale", "conversion", "margin trade"}
KRAKEN_REWARD = {"staking", "dividend", "airdrop", "reward"}
KRAKEN_INTERNAL_SUB = {
    "allocation",
    "deallocation",
    "autoallocation",
    "migration",
    "spottostaking",
    "stakingfromspot",
    "stakingtospot",
    "spotfromstaking",
    "spottofutures",
    "spotfromfutures",
}


def kraken_events(entries: list[dict[str, Any]]) -> list[Row]:
    """Kraken ledger entries (CSV rows or API entries; keys: txid, refid, time, type, subtype, asset, wallet,
    amount, fee, balance) grouped by refid into events."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    order: list[str] = []
    for e in entries:
        if not str(e.get("txid") or "").strip():  # pending duplicates of deposits/withdrawals have no txid
            continue
        ref = str(e.get("refid") or e["txid"])
        if ref not in groups:
            order.append(ref)
        groups[ref].append(e)
    rows: list[Row] = []
    for n, ref in enumerate(order, start=1):
        es = groups[ref]
        try:
            rows.append(_kraken_event(n, ref, es))
        except (FormatError, KeyError) as exc:
            rows.append(Row(n, {"rows": es}, f"kraken:{ref}", _hash(es), error=str(exc)))
    return rows


def _kraken_when(v: Any) -> datetime:
    if isinstance(v, int | float) or re.fullmatch(r"\d+(\.\d+)?", str(v)):
        return datetime.fromtimestamp(float(v), UTC)
    return _dt(str(v))


def _kraken_event(n: int, ref: str, es: list[dict[str, Any]]) -> Row:
    when = min(_kraken_when(e["time"]) for e in es)
    types = {str(e.get("type") or "").lower() for e in es}
    subs = {str(e.get("subtype") or "").lower() for e in es}
    legs = []
    balances: dict[str, str] = {}
    for e in es:
        raw_asset = str(e["asset"])
        asset = kraken_asset(raw_asset)
        if asset in KRAKEN_IGNORED_ASSETS:
            continue
        legs.append(Leg(asset, _dec(e.get("amount")), _dec(e.get("fee"))))
        if str(e.get("balance") or "").strip():
            balances[f"{raw_asset}|{e.get('wallet') or ''}"] = str(_dec(e["balance"]))
    raw_type = "/".join(sorted(t for t in types if t)) + (
        f" ({'/'.join(sorted(s for s in subs if s))})" if any(subs) else ""
    )
    net: dict[str, Decimal] = defaultdict(Decimal)
    for leg in legs:
        net[leg.asset] += leg.net
    all_zero = all(v == 0 for v in net.values())
    if types & {"margin", "rollover", "settled", "margin trade"}:
        event = "unsupported"
    elif types & KRAKEN_TRADE:
        event = "trade"
    elif types & KRAKEN_REWARD or ("earn" in types and "reward" in subs):
        event = "reward"
    elif all_zero or subs & KRAKEN_INTERNAL_SUB:  # both wallets belong to the same account here
        event = "internal"
    elif "deposit" in types:
        event = "deposit"
    elif "withdrawal" in types:
        event = "withdrawal"
    elif types & {"adjustment", "transfer", "credit", "earn"}:
        event = "deposit" if all(v >= 0 for v in net.values()) else "withdrawal"
    else:
        event = "unsupported"
    parsed = _event(when, event, legs, raw_type, f"Kraken {raw_type}", balances=balances)
    return Row(n, {"rows": es}, f"kraken:{ref}", _hash(es), parsed)


def parse_kraken(text: str) -> list[Row]:
    heads, rows = _read(text)
    missing = {"txid", "refid", "time", "type", "asset", "amount", "fee"} - set(heads)
    if missing:
        raise FormatError(f"not a Kraken ledger export (missing {', '.join(sorted(missing))})")
    return kraken_events(rows)


# ---- Binance -----------------------------------------------------------------------------------------

BINANCE_DEPOSIT = re.compile(r"^(fiat )?deposit$|crypto box|p2p.*buy", re.I)
BINANCE_WITHDRAW = re.compile(r"^(fiat )?withdraw(al)?$", re.I)
BINANCE_REWARD = re.compile(
    r"interest|reward|distribution|airdrop|cashback|rebate|bonus|mining|launchpool|"
    r"commission|referral|dividend",
    re.I,
)
BINANCE_INTERNAL = re.compile(
    r"transfer|subscription|redemption|purchase|lock|unlock|staking (purchase|redemption)", re.I
)
BINANCE_FEE = re.compile(r"^(transaction )?fee$", re.I)


def parse_binance(text: str) -> list[Row]:
    heads, rows = _read(text)
    if not {"utc_time", "operation", "coin", "change"} <= set(heads):
        raise FormatError("not a Binance transaction history (missing UTC_Time / Operation / Coin / Change)")
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    order: list[tuple[str, str, str]] = []
    for r in rows:
        op = r.get("operation", "")
        if BINANCE_DEPOSIT.search(op):
            cat = "deposit"
        elif BINANCE_WITHDRAW.search(op):
            cat = "withdrawal"
        elif BINANCE_REWARD.search(op) and not BINANCE_FEE.search(op):
            cat = "reward"
        elif BINANCE_INTERNAL.search(op) and not re.search(r"convert|exchange|buy|sell|spend|revenue|sold", op, re.I):
            cat = "internal"
        else:
            cat = "trade"
        # one trade spreads over several rows with the same second; rewards and transfers are one row per coin
        key = (r["utc_time"], cat, "" if cat == "trade" else f"{r.get('coin')}|{op}|{r.get('change')}")
        if key not in groups:
            order.append(key)
        groups[key].append(r)
    out: list[Row] = []
    for n, key in enumerate(order, start=1):
        rs = groups[key]
        try:
            when = _dt(key[0])
            legs = [
                Leg(r["coin"].upper(), _dec(r["change"]), is_fee=bool(BINANCE_FEE.search(r.get("operation", ""))))
                for r in rs
            ]
            ops = sorted({r.get("operation", "") for r in rs})
            event = key[1]
            parsed = _event(when, event, legs, " + ".join(ops), "Binance " + " + ".join(ops))
            out.append(Row(n, {"rows": rs}, f"binance:{_hash(rs)[:40]}", _hash(rs), parsed))
        except FormatError as exc:
            out.append(Row(n, {"rows": rs}, None, _hash(rs), error=str(exc)))
    return out


# ---- Ledger Live -------------------------------------------------------------------------------------


def parse_ledgerlive(text: str) -> list[Row]:
    """Ledger Live → Accounts → Export operations. Assumes "Operation Amount" excludes the network fee
    (verify against a real export: reconciliation with the address balances shows it either way)."""
    _, rows = _read(text)
    out: list[Row] = []
    for n, r in enumerate(rows, start=1):
        try:
            when = _dt(r["operation date"])
            asset = r["currency ticker"].upper()
            typ = r["operation type"].upper()
            amt, fee = abs(_dec(r.get("operation amount"))), abs(_dec(r.get("operation fees")))
            if typ == "IN":
                event, legs = "deposit", [Leg(asset, amt)]
            elif typ == "OUT":
                event, legs = "withdrawal", [Leg(asset, -amt, fee)]
            elif typ == "FEES":
                event, legs = "withdrawal", [Leg(asset, ZERO, fee or amt)]
            elif typ in ("REWARD", "REWARD_PAYOUT"):
                event, legs = "reward", [Leg(asset, amt)]
            else:
                event, legs = "internal", [Leg(asset, ZERO, fee)] if fee else []
                if fee:
                    event = "withdrawal"
            ext = f"ledgerlive:{r.get('operation hash', '')}:{typ}:{asset}"
            parsed = _event(when, event, legs, typ, f"Ledger {typ} {r.get('account name', '')}".strip())
            out.append(Row(n, r, ext if r.get("operation hash") else None, _hash(r), parsed))
        except (FormatError, KeyError) as exc:
            out.append(Row(n, r, None, _hash(r), error=str(exc)))
    return out


# ---- generic CSV -------------------------------------------------------------------------------------

GENERIC_HELP = (
    "date,type,asset,amount,quote_asset,quote_amount,fee_asset,fee,id,note — type is buy, sell, swap, deposit, "
    "withdrawal or reward; amount is always positive; for swap, asset is what you gave and quote_* what you got"
)


def parse_generic(text: str) -> list[Row]:
    heads, rows = _read(text)
    if not {"date", "type", "asset", "amount"} <= set(heads):
        raise FormatError("generic crypto CSV needs columns: " + GENERIC_HELP)
    out: list[Row] = []
    for n, r in enumerate(rows, start=1):
        try:
            when = _dt(r["date"])
            typ = r["type"].lower()
            a, q = r["asset"].upper(), abs(_dec(r["amount"]))
            qa, qq = (r.get("quote_asset") or "").upper(), abs(_dec(r.get("quote_amount")))
            fa, fee = (r.get("fee_asset") or "").upper(), abs(_dec(r.get("fee")))
            if typ == "buy":
                event, legs = "trade", [Leg(a, q), Leg(qa or "EUR", -qq)]
            elif typ == "sell":
                event, legs = "trade", [Leg(a, -q), Leg(qa or "EUR", qq)]
            elif typ == "swap":
                event, legs = "trade", [Leg(a, -q), Leg(qa, qq)]
            elif typ in ("deposit", "withdrawal", "reward"):
                event, legs = typ, [Leg(a, q if typ != "withdrawal" else -q)]
            else:
                raise FormatError(f"unknown type {typ!r} (use buy, sell, swap, deposit, withdrawal or reward)")
            if fee:
                legs.append(Leg(fa or a, -fee, is_fee=event == "trade" and (fa or a) not in {x.asset for x in legs}))
            ext = f"csv:{r['id']}" if r.get("id") else None
            parsed = _event(when, event, legs, typ, r.get("note") or f"{typ} {a}")
            out.append(Row(n, r, ext, _hash(r), parsed))
        except (FormatError, KeyError) as exc:
            out.append(Row(n, r, None, _hash(r), error=str(exc)))
    return out


PARSERS = {
    "kraken": parse_kraken,
    "binance": parse_binance,
    "ledgerlive": parse_ledgerlive,
    "crypto-csv": parse_generic,
}
ADAPTER_NAMES = {
    "kraken": "kraken-ledgers v1",
    "binance": "binance-statement v1",
    "ledgerlive": "ledger-live-operations v1 (verify)",
    "crypto-csv": "generic-crypto-csv v1",
}


def parse(source: str, text: str) -> tuple[list[Row], dict[str, Any]]:
    rows = PARSERS[source](text)
    for r in rows:  # duplicate rows within one file are harmless for event ids built from content
        if r.error is None and r.parsed.get("event") == "unsupported":
            r.error = f"{r.parsed['raw_type']}: margin and futures entries are not supported"
    return rows, {"adapter": ADAPTER_NAMES[source], "rows": len(rows)}
