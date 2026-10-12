"""Read-only crypto balance sources: Kraken, Binance, public addresses (ING-10).

API keys must be created read-only (query funds only). Seeds and private keys are never accepted.
"""

import base64
import hashlib
import hmac
import time
import urllib.parse
from decimal import Decimal
from typing import Any

from app.platform.outbound import OutboundClient

KRAKEN_ALIASES = {
    "ETH2": "ETH",
    "XBT": "BTC",
    "XXBT": "BTC",
    "XETH": "ETH",
    "ZEUR": "EUR",
    "ZUSD": "USD",
    "XXRP": "XRP",
    "XLTC": "LTC",
    "XXLM": "XLM",
    "XDG": "DOGE",
    "XXDG": "DOGE",
    "XETC": "ETC",
    "XZEC": "ZEC",
    "XXMR": "XMR",
}


class CryptoError(RuntimeError):
    pass


def kraken_asset(code: str) -> str:
    base = code.split(".")[0]  # staked/earn variants: ETH.S, DOT.F, USDC.M
    return KRAKEN_ALIASES.get(base, base[1:] if len(base) == 4 and base[0] in "XZ" and base not in ("ZRX",) else base)


def _kraken_private(key: str, secret: str, path: str, params: dict[str, Any], client: Any | None) -> dict[str, Any]:
    nonce = str(int(time.time() * 1000))
    body = urllib.parse.urlencode({"nonce": nonce, **params})
    sha = hashlib.sha256((nonce + body).encode()).digest()
    sig = base64.b64encode(hmac.new(base64.b64decode(secret), path.encode() + sha, hashlib.sha512).digest()).decode()
    c = client or OutboundClient()
    r = c.request(
        "POST",
        f"https://api.kraken.com{path}",
        content=body,
        headers={"API-Key": key, "API-Sign": sig, "Content-Type": "application/x-www-form-urlencoded"},
    )
    data: dict[str, Any] = r.json()
    if data.get("error"):
        raise CryptoError("Kraken: " + ", ".join(data["error"]))
    return dict(data.get("result") or {})


def kraken_balances(key: str, secret: str, client: Any | None = None) -> dict[str, Decimal]:
    result = _kraken_private(key, secret, "/0/private/Balance", {}, client)
    out: dict[str, Decimal] = {}
    for code, qty in result.items():
        asset = kraken_asset(code)
        if asset == "KFEE":
            continue
        out[asset] = out.get(asset, Decimal(0)) + Decimal(qty)
    return {k: v for k, v in out.items() if v != 0}


def kraken_ledgers(key: str, secret: str, start: float = 0, client: Any | None = None) -> list[dict[str, Any]]:
    """All ledger entries after `start` (unix time), oldest first. Needs the "Query ledger entries" permission."""
    entries: dict[str, dict[str, Any]] = {}
    ofs = 0
    while True:
        params: dict[str, Any] = {"ofs": ofs}
        if start:
            params["start"] = start
        res = _kraken_private(key, secret, "/0/private/Ledgers", params, client)
        page = res.get("ledger") or {}
        for txid, e in page.items():
            entries[txid] = {**e, "txid": txid}
        ofs += len(page)
        if not page or ofs >= int(res.get("count") or 0) or ofs > 100000:
            break
        time.sleep(0.0 if client is not None else 2.0)  # private endpoint rate limit
    return sorted(entries.values(), key=lambda e: float(e["time"]))


def binance_balances(key: str, secret: str, client: Any | None = None) -> dict[str, Decimal]:
    q = urllib.parse.urlencode({"timestamp": int(time.time() * 1000), "recvWindow": 10000, "omitZeroBalances": "true"})
    sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
    c = client or OutboundClient()
    r = c.get(f"https://api.binance.com/api/v3/account?{q}&signature={sig}", headers={"X-MBX-APIKEY": key})
    data = r.json()
    if r.status_code >= 400:
        raise CryptoError(f"Binance: {data.get('msg', r.status_code)}")
    out: dict[str, Decimal] = {}
    for b in data.get("balances", []):
        asset = (
            b["asset"][2:] if b["asset"].startswith("LD") and len(b["asset"]) > 3 else b["asset"]
        )  # Simple Earn "LDBTC"
        qty = Decimal(b["free"]) + Decimal(b["locked"])
        if qty:
            out[asset] = out.get(asset, Decimal(0)) + qty
    return out


def address_balances(addresses: list[dict[str, str]], client: Any | None = None) -> dict[str, Decimal]:
    """[{"chain": "btc"|"eth", "address": "..."}] → balances. Public explorers see the addresses you query."""
    c = client or OutboundClient()
    out: dict[str, Decimal] = {}
    for a in addresses:
        chain, addr = a["chain"].lower(), a["address"].strip()
        if chain == "btc":
            r = c.get(f"https://mempool.space/api/address/{addr}")
            st = r.json()["chain_stats"]
            sats = int(st["funded_txo_sum"]) - int(st["spent_txo_sum"])
            out["BTC"] = out.get("BTC", Decimal(0)) + Decimal(sats) / Decimal(10**8)
        elif chain == "eth":
            r = c.request(
                "POST",
                "https://ethereum-rpc.publicnode.com",
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [addr, "latest"]},
            )
            wei = int(r.json()["result"], 16)
            out["ETH"] = out.get("ETH", Decimal(0)) + Decimal(wei) / Decimal(10**18)
        else:
            raise CryptoError(f"unsupported chain {chain}")
    return out
