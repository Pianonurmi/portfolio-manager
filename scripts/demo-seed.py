"""Fill an empty Portfolio Manager with an invented demo household, to try the app or take screenshots.

Creates the owner login, three people (Alex, partner Sam, Child A), Nordnet accounts with about seven years of
monthly ETF saving and a few Finnish shares (as a generated Nordnet transaction export), a home, a mortgage and a
savings account, then queues the price and fund-data jobs. Every number is invented.

Only for a fresh, empty instance such as the throwaway test stack; it stops if an owner already exists.

    cd deploy && PM_BIND=127.0.0.1 ./init-env.sh && ./e2e.sh up && cd ..
    python3 scripts/demo-seed.py http://127.0.0.1:8650 [password]
    # sign in as "demo"; prices arrive within a few minutes (Settings → Jobs)
"""

import http.cookiejar
import json
import sys
import time
import urllib.request
import uuid
from collections import defaultdict
from datetime import date
from decimal import ROUND_DOWN
from decimal import Decimal as D

if len(sys.argv) < 2:
    raise SystemExit(__doc__)
BASE = sys.argv[1].rstrip("/")
PASSWORD = sys.argv[2] if len(sys.argv) > 2 else "demo-password-123"
AOT, OST, KID = "11112222", "33334444", "55556666"
INST = {
    "IWDA": ("iShares Core MSCI World UCITS ETF", "IE00B4L5Y983"),
    "EMIM": ("iShares Core MSCI EM IMI UCITS ETF", "IE00BKM4GZ66"),
    "AGGH": ("iShares Core Global Aggregate Bond UCITS ETF EUR Hedged", "IE00BDBRDM35"),
    "NDA": ("Nordea Bank Abp", "FI4000297767"),
    "KNEBV": ("Kone Oyj B", "FI0009013403"),
}
# rough price paths (EUR), linearly interpolated between anchors; only used for invented purchase prices
PATH = {
    "IWDA": [("2019-01", 46), ("2020-02", 57), ("2020-03", 45), ("2020-12", 62), ("2021-12", 80), ("2022-10", 68),
             ("2023-12", 82), ("2024-12", 102), ("2025-04", 92), ("2026-09", 110)],
    "EMIM": [("2019-01", 25), ("2020-03", 21), ("2021-02", 32), ("2022-10", 26), ("2023-12", 29), ("2024-12", 33),
             ("2026-09", 38)],
    "AGGH": [("2019-01", 5.1), ("2021-06", 5.3), ("2022-10", 4.4), ("2024-06", 4.7), ("2026-09", 4.85)],
    "NDA": [("2019-01", 7.5), ("2020-03", 5.5), ("2021-06", 9.5), ("2023-06", 10.2), ("2024-06", 11.2),
            ("2026-09", 13.0)],
    "KNEBV": [("2019-01", 44), ("2021-01", 68), ("2022-10", 42), ("2024-06", 46), ("2026-09", 55)],
}


def price(tk: str, y: int, m: int) -> D:
    pts = [(int(a[:4]) * 12 + int(a[5:]) - 1, b) for a, b in PATH[tk]]
    t = y * 12 + m - 1
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            return D(str(round(p0 + (p1 - p0) * (t - t0) / (t1 - t0), 2)))
    return D(str(pts[-1][1] if t > pts[-1][0] else pts[0][1]))


events = []  # (date, portfolio, type, ticker, qty, price, fee, amount_override, text)
nid = 5000


def ev(d, pf, typ, tk=None, q=None, px=None, fee=D(0), amt=None, text=""):
    events.append((d, pf, typ, tk, q, px, fee, amt, text))


ev("2019-01-02", AOT, "TALLETUS", amt=D(6000), text="Talletus")
ev("2019-01-03", AOT, "OSTO", "NDA", D(300), price("NDA", 2019, 1), D(9))
ev("2019-01-03", AOT, "OSTO", "KNEBV", D(40), price("KNEBV", 2019, 1), D(9))
ev("2020-09-01", OST, "TALLETUS", amt=D(20000), text="Talletus")
ev("2020-09-02", OST, "OSTO", "IWDA", D(250), price("IWDA", 2020, 9), D(0))
kid_cash = [D(0)]
for y in range(2019, 2027):
    for m in range(1, 13):
        if (y, m) > (2026, 9):
            break
        d = f"{y}-{m:02d}-05"
        ev(d, AOT, "TALLETUS", amt=D(500), text="Kuukausisäästö")
        for tk, eur in (("IWDA", 300), ("EMIM", 100), ("AGGH", 100)):
            p = price(tk, y, m)
            q = (D(eur) / p).quantize(D(1), rounding=ROUND_DOWN)
            if q:
                ev(d, AOT, "OSTO", tk, q, p, D(0))
        if y >= 2020:
            ev(f"{y}-{m:02d}-06", KID, "TALLETUS", amt=D(75), text="Lapsen säästö")
            kid_cash[0] += D(75)
            p = price("IWDA", y, m)
            q = (kid_cash[0] / p).quantize(D(1), rounding=ROUND_DOWN)
            if q:
                kid_cash[0] -= q * p
                ev(f"{y}-{m:02d}-06", KID, "OSTO", "IWDA", q, p, D(0))
    for tk, dps, mo in (("NDA", {2019: "0.69", 2020: "0.40", 2021: "0.72", 2022: "0.69", 2023: "0.80", 2024: "0.92",
                                 2025: "0.94", 2026: "0.96"}, 3), ("KNEBV", {y: "1.75"}, 3)):
        if y in dps:
            n = D(300) if tk == "NDA" else D(40)
            if tk == "NDA" and y >= 2023:
                n = D(220)
            gross = (n * D(dps[y])).quantize(D("0.01"))
            ev(f"{y}-{mo:02d}-28", AOT, "OSINKO", tk, n, D(dps[y]), text=f"Osinko {dps[y]} EUR/kpl")
            ev(f"{y}-{mo:02d}-28", AOT, "ENNAKKOPIDÄTYS", tk, amt=-(gross * D("0.255")).quantize(D("0.01")),
               text="Ennakkopidätys 25,5 %")
ev("2022-11-15", AOT, "MYYNTI", "NDA", D(80), D("10.10"), D(9))
ev("2024-03-12", OST, "OSTO", "EMIM", D(150), price("EMIM", 2024, 3), D(0))
ev("2024-03-11", OST, "TALLETUS", amt=D(5000), text="Talletus")
events.sort(key=lambda e: e[0])

qty, cash, rows = defaultdict(D), defaultdict(D), []
for d, pf, typ, tk, q, px, fee, amt, text in events:
    nid += 1
    name, isin = INST[tk] if tk else ("", "")
    if amt is None:
        if typ == "OSTO":
            amt = -(q * px + fee).quantize(D("0.01"))
        elif typ == "MYYNTI":
            amt = (q * px - fee).quantize(D("0.01"))
        elif typ == "OSINKO":
            amt = (q * px).quantize(D("0.01"))
    if typ == "OSTO":
        qty[(pf, isin)] += q
    elif typ == "MYYNTI":
        qty[(pf, isin)] -= q
    cash[pf] += amt
    fmt = lambda x: str(x).replace(".", ",") if x is not None else ""  # noqa: E731
    rows.append([str(nid), d, d, d, pf, typ, name, isin, fmt(q), fmt(px), "", fmt(fee), "EUR", fmt(amt), "EUR", "", "",
                 "", "", fmt(qty[(pf, isin)]) if typ in ("OSTO", "MYYNTI") else "", fmt(cash[pf]), "", text, "", "",
                 f"V{nid}", fmt(fee), "EUR"])
HEAD = ["Id", "Kirjauspäivä", "Kauppapäivä", "Maksupäivä", "Salkku", "Tapahtumatyyppi", "Arvopaperi", "ISIN", "Määrä",
        "Kurssi", "Korko", "Kokonaiskulut", "Valuutta", "Summa", "Valuutta", "Hankinta-arvo", "Valuutta", "Tulos",
        "Valuutta", "Kokonaismäärä", "Saldo", "Vaihtokurssi", "Tapahtumateksti", "Mitätöintipäivä",
        "Laskelman numero", "Vahvistusnumero", "Välityspalkkio", "Valuutta"]
csv = ("\t".join(HEAD) + "\n" + "".join("\t".join(r) + "\n" for r in reversed(rows))).encode("utf-16")

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(method, path, body=None, files=None):
    headers, data = {"X-PM-Request": "1"}, None
    if files:
        b = uuid.uuid4().hex
        data = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{files[0]}\"\r\n"
                f"Content-Type: text/csv\r\n\r\n").encode() + files[1] + f"\r\n--{b}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={b}"
    elif body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with opener.open(req, timeout=600) as r:
            txt = r.read()
            return json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path}: {e.code} {e.read()[:300]!r}")


call("POST", "/api/auth/setup", {"username": "demo", "password": PASSWORD})
me = call("POST", "/api/persons", {"name": "Alex", "is_self": True, "birth_date": "1985-06-15"})
sam = call("POST", "/api/persons", {"name": "Sam", "tracking": "shared"})
kid = call("POST", "/api/persons", {"name": "Child A", "role": "child", "birth_date": "2019-04-20"})
mine = [{"entity_id": me["entity_id"], "share": "1"}]
half = [{"entity_id": me["entity_id"], "share": "0.5"}, {"entity_id": sam["entity_id"], "share": "0.5"}]
nn = call("POST", "/api/institutions", {"name": "Nordnet", "kind": "broker"})
for name, kind, ref, own in (("Nordnet AOT", "aot", AOT, mine), ("Nordnet OST", "ost", OST, mine),
                             ("Nordnet AOT · Child A", "aot", KID, [{"entity_id": kid["entity_id"], "share": "1"}])):
    call("POST", "/api/accounts", {"name": name, "kind": kind, "institution_id": nn["id"], "external_ref": ref,
                                   "owners": own})
b = call("POST", "/api/imports", files=("demo.csv", csv))
call("POST", f"/api/imports/{b['id']}/create-instruments")
res = call("POST", f"/api/imports/{b['id']}/commit")
print("imported:", res["committed"], "errors:", res["errors"])

home = call("POST", "/api/accounts", {"name": "Home (Tampere)", "kind": "property", "owners": half,
                                     "opened_on": "2019-06-01"})
loan = call("POST", "/api/accounts", {"name": "Mortgage", "kind": "mortgage", "owners": half,
                                     "opened_on": "2019-06-01"})
sav = call("POST", "/api/accounts", {"name": "Savings account", "kind": "savings", "owners": mine})
for d, v in (("2019-06-01", 285000), ("2022-06-30", 312000), ("2026-06-30", 305000)):
    call("POST", "/api/valuations", {"account_id": home["id"], "date": d, "value": str(v)})
for d, v in (("2019-06-01", 230000), ("2021-01-01", 214000), ("2023-01-01", 196500), ("2025-01-01", 178800),
             ("2026-09-30", 168200)):
    call("POST", "/api/valuations", {"account_id": loan["id"], "date": d, "value": str(v), "method": "statement"})
for d, v in (("2021-01-01", 4000), ("2023-01-01", 7500), ("2026-09-30", 11800)):
    call("POST", "/api/valuations", {"account_id": sav["id"], "date": d, "value": str(v), "method": "statement"})

for job in ("rules.load", "prices.backfill", "instruments.profile", "benchmarks.refresh"):
    run = call("POST", f"/api/jobs/{job}/run")
    print("queued", job, run)
print(f"done: sign in at {BASE} as demo; prices arrive within a few minutes (Settings → Jobs)")
