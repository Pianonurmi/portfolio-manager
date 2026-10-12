"""Generate synthetic Nordnet transaction exports (anonymised, invented numbers) for golden tests.

Run: python3 fixtures/nordnet/make_fixtures.py
Writes transactions-fi.csv (UTF-16, tab, Finnish headers) and transactions-en.csv (UTF-8, ';', English).
Running 'Kokonaismäärä' (quantity after) and 'Saldo' (cash after) are computed like Nordnet reports them.
"""

from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path

AOT, OST = "12345678", "87654321"
I = {
    "IWDA": ("iShares Core MSCI World UCITS ETF", "IE00B4L5Y983"),
    "SAMPO": ("Sampo Oyj A", "FI4000552500"),
    "KNEBV": ("Kone Oyj B", "FI0009013403"),
    "NNSUOMI": ("Nordnet Indeksirahasto Suomi", "FI4000415252"),
    "NOKIA": ("Nokia Oyj", "FI0009000681"),
    "NVDA": ("NVIDIA Corp", "US67066G1040"),
}
# (id, date, portfolio, type, ticker, qty, price, fees, amount_override, fx, text, cancelled)
events = [
    (1001, "2021-03-02", AOT, "TALLETUS", None, None, None, "0", "8000", None, "Talletus", None),
    (1002, "2021-03-03", AOT, "OSTO", "IWDA", "40", "62,10", "9", None, None, "", None),
    (1003, "2021-03-03", AOT, "OSTO", "SAMPO", "50", "38,20", "9", None, None, "", None),
    (1004, "2021-05-20", AOT, "OSINKO", "SAMPO", "50", "1,70", "0", None, None, "Osinko 1,70 EUR/kpl", None),
    (1005, "2021-05-20", AOT, "ENNAKKOPIDÄTYS", "SAMPO", None, None, "0", "-21,68", None, "Ennakkopidätys 25,5 %", None),
    (1006, "2021-09-01", OST, "TALLETUS", None, None, None, "0", "15000", None, "", None),
    (1007, "2021-09-02", OST, "OSTO", "NNSUOMI", "40,123", "149,52", "0", None, None, "Merkintä", None),
    (1008, "2021-09-02", OST, "OSTO", "NOKIA", "800", "5,10", "9", None, None, "", None),
    (1009, "2022-01-10", AOT, "OSTO", "KNEBV", "30", "57,40", "9", None, None, "", None),
    (1010, "2022-01-10", AOT, "OSTO", "KNEBV", "30", "57,40", "9", None, None, "virheellinen", "2022-01-11"),
    (1011, "2022-06-15", OST, "OSTO", "NVDA", "20", "160,00", "15", "-3055", "0,9500", "USD-kauppa", None),
    (1012, "2022-12-31", AOT, "TALLETUSKORKO", None, None, None, "0", "1,23", None, "Korko 2022", None),
    (1013, "2023-03-15", AOT, "MYYNTI", "IWDA", "10", "71,50", "9", None, None, "", None),
    (1014, "2023-05-02", AOT, "OSINKO", "SAMPO", "50", "1,80", "0", None, None, "", None),
    (1015, "2023-05-02", AOT, "ENNAKKOPIDÄTYS", "SAMPO", None, None, "0", "-22,95", None, "", None),
    (1016, "2023-07-01", OST, "LÄHDEVERO", "NVDA", None, None, "0", "-0,15", None, "US withholding", None),
    (1017, "2023-07-01", OST, "OSINKO", "NVDA", None, None, "0", "1,02", None, "", None),
    (1018, "2024-02-01", AOT, "TALLETUS", None, None, None, "0", "2000", None, "", None),
    (1019, "2024-02-02", AOT, "OSTO", "IWDA", "25", "88,04", "9", None, None, "", None),
    (1020, "2024-06-01", OST, "PALKKIO", None, None, None, "0", "-2,50", None, "Säilytysmaksu", None),
    (1021, "2024-09-30", AOT, "OMISTUSMUUTOS", "SAMPO", None, None, "0", "0", None, "Tuntematon tapahtuma", None),
    (1022, "2025-03-10", OST, "MYYNTI", "NOKIA", "300", "4,40", "9", None, None, "", None),
    (1023, "2025-04-01", AOT, "NOSTO", None, None, None, "0", "-500", None, "", None),
]

qty = defaultdict(D)
cash = defaultdict(D)
rows = []
for (id_, d, pf, typ, tk, q, px, fee, amt, fx, text, cancel) in events:
    name, isin = I[tk] if tk else ("", "")
    fq = D(q.replace(",", ".")) if q else D(0)
    fp = D(px.replace(",", ".")) if px else D(0)
    ff = D(fee.replace(",", "."))
    if amt is not None:
        a = D(amt.replace(",", "."))
    elif typ == "OSTO":
        a = -(fq * fp + ff).quantize(D("0.01"))
    elif typ == "MYYNTI":
        a = (fq * fp - ff).quantize(D("0.01"))
    elif typ == "OSINKO":
        a = (fq * fp).quantize(D("0.01"))
    else:
        a = D(0)
    if not cancel:
        if typ == "OSTO":
            qty[(pf, isin)] += fq
        elif typ == "MYYNTI":
            qty[(pf, isin)] -= fq
        cash[pf] += a
    rows.append({
        "id": id_, "date": d, "pf": pf, "typ": typ, "name": name, "isin": isin,
        "q": q or "", "px": px or "", "fee": fee, "amt": str(a).replace(".", ","),
        "totq": str(qty[(pf, isin)]).replace(".", ",") if tk and typ in ("OSTO", "MYYNTI") else "",
        "saldo": str(cash[pf]).replace(".", ","), "fx": fx or "", "text": text, "cancel": cancel or "",
        "ccy": "EUR",
    })

FI = ["Id", "Kirjauspäivä", "Kauppapäivä", "Maksupäivä", "Salkku", "Tapahtumatyyppi", "Arvopaperi", "ISIN", "Määrä", "Kurssi",
      "Korko", "Kokonaiskulut", "Valuutta", "Summa", "Valuutta", "Hankinta-arvo", "Valuutta", "Tulos", "Valuutta",
      "Kokonaismäärä", "Saldo", "Vaihtokurssi", "Tapahtumateksti", "Mitätöintipäivä", "Laskelman numero", "Vahvistusnumero",
      "Välityspalkkio", "Valuutta"]
EN = ["Id", "Booking day", "Trade day", "Settlement date", "Portfolio", "Transaction type", "Security", "ISIN", "Quantity",
      "Price", "Interest", "Total fees", "Currency", "Amount", "Currency", "Purchase value", "Currency", "Result", "Currency",
      "Total quantity", "Balance", "Exchange rate", "Transaction text", "Cancellation date", "Verification number",
      "Brokerage", "Currency"]
EN_TYPES = {"TALLETUS": "DEPOSIT", "OSTO": "BUY", "OSINKO": "DIVIDEND", "ENNAKKOPIDÄTYS": "WITHHOLDING TAX", "MYYNTI": "SELL",
            "TALLETUSKORKO": "INTEREST", "LÄHDEVERO": "FOREIGN TAX", "PALKKIO": "FEE", "NOSTO": "WITHDRAWAL",
            "OMISTUSMUUTOS": "OWNERSHIP CHANGE"}


def line(r, en=False):
    base = [str(r["id"]), r["date"], r["date"], r["date"], r["pf"], EN_TYPES[r["typ"]] if en else r["typ"], r["name"], r["isin"],
            r["q"], r["px"], "", r["fee"], r["ccy"], r["amt"], r["ccy"], "", "", "", "", r["totq"], r["saldo"], r["fx"],
            r["text"], r["cancel"]]
    if en:
        return base + [f"V{r['id']}", r["fee"], r["ccy"]]
    return base + ["", f"V{r['id']}", r["fee"], r["ccy"]]


here = Path(__file__).parent
fi = "\t".join(FI) + "\n" + "".join("\t".join(line(r)) + "\n" for r in reversed(rows))
(here / "transactions-fi.csv").write_bytes(fi.encode("utf-16"))
en = ";".join(EN) + "\n" + "".join(";".join(line(r, en=True)) + "\n" for r in rows)
(here / "transactions-en.csv").write_text(en, encoding="utf-8")
print("expected final positions:", {k: str(v) for k, v in qty.items() if v})
print("expected cash:", {k: str(v) for k, v in cash.items()})
