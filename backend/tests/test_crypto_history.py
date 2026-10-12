"""Crypto transaction history: Kraken/Binance/Ledger Live/generic imports, swaps, rewards, transfers, tax (ING-11)."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.crypto import history
from app.modules.crypto.assets import crypto_instrument
from app.modules.ledger.models import Transaction
from app.modules.portfolio import lots as lotmod
from app.modules.pricing.models import Price
from app.platform.jobs import run_now

H = '"txid","refid","time","type","subtype","aclass","asset","wallet","amount","fee","balance"'
KRAKEN = "\n".join(
    [
        H,
        '"L1","D1","2023-01-10 09:00:00","deposit","","currency","ZEUR","spot / main",2500.0000,0,2500.0000',
        '"","D1","2023-01-10 08:59:00","deposit","","currency","ZEUR","spot / main",2500.0000,0,""',  # pending copy
        '"L2","T1","2023-01-11 10:00:00","trade","","currency","ZEUR","spot / main",-2000.0000,3.2000,496.8000',
        '"L3","T1","2023-01-11 10:00:00","trade","","currency","XXBT","spot / main",0.1000000000,0,0.1000000000',
        '"L4","T2","2023-03-01 12:00:00","trade","","currency","XXBT","spot / main",-0.0600000000,0,0.0400000000',
        '"L5","T2","2023-03-01 12:00:00","trade","","currency","XETH","spot / main",0.7000000000,0.0014,0.6986000000',
        '"L6","A1","2023-03-15 12:00:00","earn","allocation","currency","XETH","spot / main",-0.6986,0,0',
        '"L7","A1","2023-03-15 12:00:00","earn","allocation","currency","ETH","earn / flexible",0.6986,0,0.6986',
        '"L8","R1","2023-04-01 01:00:00","earn","reward","currency","ETH","earn / flexible",0.0020,0,0.7006',
        '"L9","A2","2023-05-01 12:00:00","earn","deallocation","currency","ETH","earn / flexible",-0.7006,0,0',
        '"L10","A2","2023-05-01 12:00:00","earn","deallocation","currency","XETH","spot / main",0.7006,0,0.7006',
        '"L11","W1","2023-05-02 08:00:00","withdrawal","","currency","XETH","spot / main",-0.7000,0.0006,0',
        '"L12","T3","2024-02-01 15:00:00","trade","","currency","ZEUR","spot / main",1600.0000,2.5600,2094.2400',
        '"L13","T3","2024-02-01 15:00:00","trade","","currency","XXBT","spot / main",-0.0400000000,0,0',
    ]
)
LEDGER_LIVE = (
    "Operation Date,Currency Ticker,Operation Type,Operation Amount,Operation Fees,Operation Hash,Account Name,"
    "Account xpub,Countervalue Ticker,Countervalue at Operation Date,Countervalue at CSV Export\n"
    "2023-05-02T09:30:00.000Z,ETH,IN,0.7,0,0xabc,Ethereum 1,0xme,EUR,1190,2000\n"
)


def _setup(c: TestClient, db: Session) -> dict[str, int]:
    run_now("rules.load")
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    owners = [{"entity_id": me["entity_id"], "share": "1"}]
    kraken = c.post("/api/accounts", json={"name": "Kraken", "kind": "crypto_exchange", "owners": owners}).json()
    wallet = c.post("/api/accounts", json={"name": "Ledger", "kind": "crypto_wallet", "owners": owners}).json()
    for sym, rows in {
        "BTC": [("2023-01-01", "20000"), ("2024-02-01", "40000")],
        "ETH": [("2023-01-01", "1200"), ("2023-03-01", "1500"), ("2023-04-01", "1600"), ("2023-05-02", "1700")],
    }.items():
        inst = crypto_instrument(db, sym)
        for d, close in rows:
            db.add(
                Price(
                    instrument_id=inst.id,
                    date=date.fromisoformat(d),
                    close=Decimal(close),
                    currency="EUR",
                    source="manual",
                )
            )
    db.commit()
    return {"entity": me["entity_id"], "kraken": kraken["id"], "wallet": wallet["id"]}


def _upload(c: TestClient, name: str, text: str, account: int) -> dict:
    r = c.post("/api/imports", files={"file": (name, text.encode())}, data={"account_id": str(account)})
    assert r.status_code == 201, r.text
    return r.json()


def test_kraken_events_are_grouped_and_classified() -> None:
    rows, meta = history.parse("kraken", KRAKEN)
    ev = {r.external_id: r.parsed["event"] for r in rows}
    assert ev == {
        "kraken:D1": "deposit",
        "kraken:T1": "trade",
        "kraken:T2": "trade",
        "kraken:A1": "internal",
        "kraken:R1": "reward",
        "kraken:A2": "internal",
        "kraken:W1": "withdrawal",
        "kraken:T3": "trade",
    }
    assert history.nets(rows[2].parsed) == {"BTC": Decimal("-0.06"), "ETH": Decimal("0.6986")}
    assert meta["adapter"].startswith("kraken")
    assert history.sniff(KRAKEN) == "kraken" and history.sniff(LEDGER_LIVE) == "ledgerlive"


def test_kraken_import_books_trades_swaps_rewards_and_reconciles(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    a = _setup(c, db)
    b = _upload(c, "kraken.csv", KRAKEN, a["kraken"])
    assert b["source"] == "kraken"
    res = c.post(f"/api/imports/{b['id']}/commit").json()
    assert res["errors"] == 0 and res["committed"] == 6, res
    assert res["reconciliation"][str(a["kraken"])]["ok"] is True
    pos = {p["instrument"]: Decimal(p["quantity"]) for p in c.get("/api/ledger/positions").json()}
    assert pos == {"Cash EUR": Decimal("2094.24")}  # all crypto gone: swapped, withdrawn, sold
    # re-import is idempotent
    b2 = _upload(c, "kraken-again.csv", KRAKEN, a["kraken"])
    assert b2["counts"] == {"duplicate": 6, "ignored": 2}

    # the hardware wallet receives the withdrawal: matched as an own transfer, dates and cost carried over
    lw = _upload(c, "ledger.csv", LEDGER_LIVE, a["wallet"])
    r = c.post(f"/api/imports/{lw['id']}/commit").json()
    assert r["committed"] == 1 and r["transfers_matched"] == 1
    book = lotmod.build(db)
    eth = crypto_instrument(db, "ETH").id
    lots = [(lot.acquired, lot.quantity, lot.cost.quantize(Decimal("0.01"))) for lot in book.lots(a["wallet"], eth)]
    assert lots == [
        (date(2023, 3, 1), Decimal("0.6986"), Decimal("1047.90")),
        (date(2023, 4, 1), Decimal("0.0014"), Decimal("2.24")),
    ]
    assert c.get("/api/crypto/transfers").json() == {"out": [], "in": []}

    # tax: the swap is a disposal at the value received; the network fee is a disposal with zero proceeds
    y23 = c.get("/api/tax/crypto", params={"scope": f"entity:{a['entity']}", "year": 2023}).json()
    sales = {(s["instrument"], s["sold"]): s for s in y23["sales"]}
    swap = sales[("BTC", "2023-03-01")]
    assert swap["gross"] == "1047.90" and swap["cost"] == "1201.92" and swap["gain"] == "-154.02"
    assert sales[("ETH", "2023-05-02")]["gain"] == "-0.96"
    assert y23["rewards_total"] == "3.20" and y23["unmatched_out"] == []
    y = c.get("/api/tax/year", params={"scope": f"entity:{a['entity']}", "year": 2023}).json()
    assert y["small_disposals_exempt"] is False and y["crypto_rewards"] == "3.20" and y["capital_income"] == "0.00"
    y24 = c.get("/api/tax/year", params={"scope": f"entity:{a['entity']}", "year": 2024}).json()
    # 0.04 BTC: proceeds 1 597,44 − cost 801,28 = 796,16; the 2023 loss of 154,98 first absorbed that year's
    # 3,20 of rewards, so 151,78 is carried forward
    assert y24["sales"][0]["gain"] == "796.16" and y24["loss_carry_in"] == "151.78"
    assert y24["capital_income"] == "644.38" and y24["tax"] == "193.31"
    csv_text = c.get("/api/tax/crypto.csv", params={"scope": f"entity:{a['entity']}", "year": 2023}).text
    assert csv_text.splitlines()[0].startswith("Virtuaalivaluutta;Määrä") and len(csv_text.splitlines()) == 3

    # undoing the wallet import unlinks the Kraken withdrawal again
    assert c.post(f"/api/undo/{r['undo_id']}").status_code == 204
    db.expire_all()
    out = db.scalar(select(Transaction).where(Transaction.external_id == "kraken:W1"))
    assert out is not None and out.link_id is None
    assert len(c.get("/api/crypto/transfers").json()["out"]) == 1


def test_balance_sync_entries_are_replaced_by_history(auth_client: TestClient, db: Session) -> None:
    from app.modules.core.models import Account
    from app.modules.crypto.service import apply_balances

    c = auth_client
    a = _setup(c, db)
    c.put("/api/connections/kraken", json={"config": {"api_key": "k", "account_id": a["kraken"]}, "secret": "c2VjcmV0"})
    acct = db.get(Account, a["kraken"])
    assert acct is not None
    apply_balances(db, acct, {"BTC": Decimal("0.5")}, "kraken", date(2023, 1, 1))
    db.commit()
    b = _upload(c, "kraken.csv", KRAKEN, a["kraken"])
    res = c.post(f"/api/imports/{b['id']}/commit").json()
    assert any("balance-sync" in w for w in res["warnings"])
    pos = {p["instrument"]: Decimal(p["quantity"]) for p in c.get("/api/ledger/positions").json()}
    assert "BTC" not in pos
    conn = {x["kind"]: x for x in c.get("/api/connections").json()}["kraken"]
    assert conn["config"]["mode"] == "history"


def test_binance_statement_groups_trades_fees_and_rewards() -> None:
    text = (
        "User_ID,UTC_Time,Account,Operation,Coin,Change,Remark\n"
        "1,2024-01-05 10:00:00,Spot,Deposit,EUR,500,\n"
        "1,2024-01-05 11:00:00,Spot,Transaction Spend,EUR,-400,\n"
        "1,2024-01-05 11:00:00,Spot,Transaction Buy,BTC,0.01,\n"
        "1,2024-01-05 11:00:00,Spot,Transaction Fee,BNB,-0.001,\n"
        "1,2024-01-06 00:00:00,Earn,Simple Earn Flexible Subscription,BTC,-0.01,\n"
        "1,2024-01-06 00:00:00,Spot,Simple Earn Flexible Subscription,BTC,0.01,\n"
        "1,2024-01-07 00:00:00,Earn,Simple Earn Flexible Interest,BTC,0.00001,\n"
        "1,2024-01-08 00:00:00,Spot,Binance Convert,BTC,-0.005,\n"
        "1,2024-01-08 00:00:00,Spot,Binance Convert,ETH,0.08,\n"
    )
    rows, _ = history.parse("binance", text)
    kinds = [r.parsed["event"] for r in rows]
    assert kinds == ["deposit", "trade", "internal", "internal", "reward", "trade"]
    trade = rows[1].parsed
    assert {(x["asset"], x["is_fee"]) for x in trade["legs"]} == {("EUR", False), ("BTC", False), ("BNB", True)}


def test_generic_csv_and_fee_only_leg(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    a = _setup(c, db)
    text = (
        "date,type,asset,amount,quote_asset,quote_amount,fee_asset,fee,id,note\n"
        "2023-01-02,buy,BTC,0.01,EUR,200,EUR,1,g1,card buy\n"
        "2023-03-01,swap,BTC,0.01,ETH,0.1,,,g2,swap\n"
    )
    b = _upload(c, "mine.csv", text, a["wallet"])
    assert b["source"] == "crypto-csv"
    res = c.post(f"/api/imports/{b['id']}/commit").json()
    assert res["errors"] == 0, res
    y = c.get("/api/tax/crypto", params={"scope": f"entity:{a['entity']}", "year": 2023}).json()
    s = y["sales"][0]
    # swap valued at the ETH received (0,1 × 1 500); cost 200 + 1 fee
    assert s["gross"] == "150.00" and s["cost"] == "201.00" and s["gain"] == "-51.00"


def test_linked_transfer_in_before_out_keeps_cost(auth_client: TestClient, db: Session) -> None:
    from app.modules.crypto import booking
    from app.modules.ledger.service import TxnSpec, record

    c = auth_client
    a = _setup(c, db)
    btc = crypto_instrument(db, "BTC").id
    record(
        db,
        TxnSpec(
            a["kraken"],
            date(2023, 1, 1),
            "buy",
            "EUR",
            amount=Decimal("-100"),
            instrument_id=btc,
            quantity=Decimal("0.01"),
        ),
    )
    t_in = record(
        db, TxnSpec(a["wallet"], date(2023, 2, 1), "security_in", "EUR", instrument_id=btc, quantity=Decimal("0.01"))
    )
    t_out = record(
        db, TxnSpec(a["kraken"], date(2023, 2, 2), "security_out", "EUR", instrument_id=btc, quantity=Decimal("0.01"))
    )
    booking.link(db, t_out.id, t_in.id)
    lots = lotmod.build(db).lots(a["wallet"], btc)
    assert [(lot.acquired, lot.cost) for lot in lots] == [(date(2023, 1, 1), Decimal("100"))]
    booking.unlink(db, t_in.id)
    assert db.get(Transaction, t_out.id).link_id is None  # type: ignore[union-attr]


def test_history_mode_pulls_kraken_ledgers_and_only_checks_balances(auth_client: TestClient, db: Session) -> None:
    from app.modules.crypto.service import sync_all
    from tests.test_crypto_alerts import Fake

    c = auth_client
    a = _setup(c, db)
    c.put(
        "/api/connections/kraken",
        json={"config": {"api_key": "k", "account_id": a["kraken"], "mode": "history"}, "secret": "c2VjcmV0"},
    )
    ledger = {
        "L1": {
            "refid": "D1",
            "time": 1673341200.0,
            "type": "deposit",
            "subtype": "",
            "asset": "ZEUR",
            "amount": "2500",
            "fee": "0",
            "balance": "2500",
        },
        "L2": {
            "refid": "T1",
            "time": 1673431200.0,
            "type": "trade",
            "subtype": "",
            "asset": "ZEUR",
            "amount": "-2000",
            "fee": "3.2",
            "balance": "496.8",
        },
        "L3": {
            "refid": "T1",
            "time": 1673431200.0,
            "type": "trade",
            "subtype": "",
            "asset": "XXBT",
            "amount": "0.1",
            "fee": "0",
            "balance": "0.1",
        },
    }
    fake = Fake(
        {
            "https://api.kraken.com/0/private/Ledgers": {"error": [], "result": {"ledger": ledger, "count": 3}},
            "https://api.kraken.com/0/private/Balance": {"error": [], "result": {"XXBT": "0.1", "ZEUR": "496.8"}},
        }
    )
    assert sync_all(db, clients={"kraken": fake})["kraken"] == 2
    db.commit()
    pos = {p["instrument"]: Decimal(p["quantity"]) for p in c.get("/api/ledger/positions").json()}
    assert pos == {"BTC": Decimal("0.1"), "Cash EUR": Decimal("496.8")}
    # the next pull sees the same entries again: nothing new is booked
    assert sync_all(db, clients={"kraken": fake})["kraken"] == 0
    conn = {x["kind"]: x for x in c.get("/api/connections").json()}["kraken"]
    assert conn["status"] == "ok" and conn["config"]["ledger_cursor"] > 0
