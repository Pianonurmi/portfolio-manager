# Portfolio Manager

A self-hosted personal finance suite for a Finnish household. It keeps a double-entry ledger of investment accounts, values the portfolio daily, tracks net worth (property, loans, crypto, manual assets), syncs cash flow from YNAB or Actual Budget, checks and forecasts Finnish tax, simulates retirement and other plans, and reads bank and broker statements with a local language model. It also holds a few "life" modules (subscriptions, vehicles, home maintenance, insurance, estate package).

It runs on a home server, is reachable only from the LAN or VPN, and sends data out only to an allowlist of hosts. It is read-only towards institutions: it never moves money or places trades. It has a single owner login.

## Features

Portfolio
- Double-entry ledger with raw-first imports: every imported row is kept with a content hash, imports are idempotent, and corrections supersede rather than delete.
- Nordnet CSV import with staging, review, instrument mapping, corporate actions (splits, exchanges, redemptions and others) and row-level reconciliation.
- Holdings, FIFO cost basis, TWR and XIRR per account, person and household, dividends, and a dashboard per child.
- Allocation targets, drift and tax-aware rebalancing.
- Portfolio X-ray: true asset class, regions, countries, sectors, size, style, bond duration and credit, and fund costs (TER) in €/year, looking through every ETF and fund with Morningstar data.

Prices and FX
- Daily prices from Yahoo and Morningstar (lt.morningstar.com), ECB exchange rates, a price coverage panel in Settings, and stale or missing prices flagged instead of shown as zero.

Net worth
- Household and per-person balance sheet with ownership shares, history and change decomposition.
- Property, housing-company loan, mortgage and other manual assets and liabilities; housing value from Statistics Finland indices; loan what-if scenarios.

Cash flow
- YNAB sync (optional push back) or Actual Budget as an alternative backend, balances into net worth, spending analytics, housing costs from budget categories.

Crypto
- Kraken and Binance read-only API, public addresses, transaction history imports (Kraken ledger CSV, Binance statement, Ledger Live, generic CSV) and Finnish crypto tax views.

Tax
- Finnish tax engine driven by rule tables per year (`rules/`): gains with the deemed cost, OST, small-disposal rule, gift and inheritance tax, year-to-date estimate.

Planning
- Pension (työeläke) estimate, deterministic and Monte Carlo retirement projection, life events, inheritances, scenario comparison, stress tests, fee analysis, backtests, tax-aware withdrawal order and new-money comparison.

Local AI
- Reads uploaded statements (PDF, scans, screenshots, CSV, Excel) page by page through an OpenAI-compatible gateway on the LAN, checks the result against the page text and totals, and books it only after review.
- Extracts values from Paperless-ngx documents with the quoted source text, and explains plans in plain language.

Life and estate
- Subscriptions detected from budget transactions, vehicles, home maintenance log, insurance register, estate package, shared timeline, search and attention list.

Operations
- Alerts and a weekly digest through Gotify and SMTP, Paperless-ngx links, tags and notes, data export, nightly backups, audit log and an outbound call log.

## Architecture

A modular monolith: one FastAPI backend split into domain modules, one worker process for scheduled jobs, one Postgres database and a React single-page app. Five containers run under Docker Compose.

```
   LAN / VPN
       |
  reverse proxy on the LAN (TLS)
       | http
  +----v-----+      +-----------------------+
  | web      |----->| api (FastAPI)         |
  | Caddy:   | /api |  modules: core ledger |
  | SPA+proxy|      |  ingest pricing ...   |
  +----------+      +-----+-----------+-----+
   edge+internal          |           |
                    +-----v----+  +---v-------+
                    | db       |<-| worker    |
                    | Postgres |  | jobs      |
                    +----------+  +---+-------+
                                      |  (internal network, no route out)
                                +-----v-----+
                                | egress    |  hostname allowlist
                                | proxy     |  (HTTPS CONNECT only)
                                +-----+-----+
          ECB, Yahoo, Morningstar, Statistics Finland, YNAB, Kraken, Binance, CoinGecko
```

- `web` is the only published container: plain HTTP on the LAN address `PM_BIND:PM_PORT`. TLS ends at a reverse proxy on your LAN. It serves the built frontend and proxies `/api`.
- `api`, `worker` and `db` sit on an internal Docker network with no internet route. Their only way out is the `egress` proxy, which allows HTTPS only to the hostnames in `PM_OUTBOUND_ALLOWLIST`. The app checks the same list and logs every outbound call (Settings, Connections).
- LAN services (Gotify, Paperless-ngx, the local AI gateway) are allowed through `PM_LAN_ALLOWLIST`. The worker also has a `mail` network for SMTP to private addresses.
- The local LLM is an OpenAI-compatible gateway configured with `PM_LLM_URL`.
- Data sources: ECB (FX), Yahoo (prices), Morningstar lt.morningstar.com (fund prices and fund data), Nordnet www.nordnet.fi (its public ETF list, for the screener), Statistics Finland (housing indices), YNAB, Kraken, Binance and CoinGecko. The default allowlist is written by `deploy/init-env.sh`.
- Money is `Decimal`, never float. Positions, lots, valuations and snapshots are derived from the ledger and can be recomputed.

Backend modules are in `backend/app/modules/`: core, ledger, ingest, pricing, portfolio, networth, cashflow, crypto, tax, planning, documents, alerts, ai, exports, home, vehicles, subscriptions, life, ux. Frontend screens: Home, Portfolio, Net worth, Cash flow, Tax, Life, Imports, Planning, Kids, Household, Settings.

## Running it

Production is `deploy/compose.yaml`.

```sh
cd deploy
PM_BIND=192.168.1.10 ./init-env.sh  # once: writes deploy/.env with fresh secrets (never committed)
docker compose up -d --build        # deploy or upgrade all services together; migrations run when the api starts
docker compose ps                   # health
docker compose logs -f worker       # scheduled jobs
```

- `init-env.sh` generates `PM_MASTER_KEY` and the database password and writes the bind address (`PM_BIND`, this host's LAN address), port, outbound allowlist and optional LLM URL (`PM_LLM_URL`). Edit `.env` for your network: LAN services such as Gotify, Paperless-ngx or the LLM gateway go into `PM_LAN_ALLOWLIST`. Back `.env` up separately from the database dumps: without `PM_MASTER_KEY` the stored API tokens cannot be decrypted.
- Binding: `PM_BIND` (required) and `PM_PORT` (default `8640`). Do not put the app behind an internet-facing proxy.
- Auth modes (`PM_AUTH_MODE`): `login` is the default (username and password, optional TOTP). `network` signs requests in as the single owner when the connecting address and every `X-Forwarded-For` hop are in private ranges; a public address anywhere in the chain still requires sign-in.
- Reset a password: `docker compose exec api python -m app.cli reset-password <username>`.
- Ingress is restricted to private ranges by `deploy/firewall/pm-lan-only.sh` (systemd unit `pm-firewall.service`; set the path in `ExecStart` to your checkout).
- First run: open the app, create the owner account, then add people and accounts in Settings and import a Nordnet transaction export under Imports.

## Development

`deploy/compose.dev.yaml` (project `pm-dev`) starts a throwaway Postgres on tmpfs and a `tools` container with the backend source mounted at `/srv/app`.

```sh
docker compose -f deploy/compose.dev.yaml up -d --build
docker exec -w /srv/app pm-dev-tools-1 ruff check .
docker exec -w /srv/app pm-dev-tools-1 mypy app
docker exec -w /srv/app pm-dev-tools-1 pytest
cd frontend && npx tsc --noEmit     # frontend type check
# vitest, in the same Node image as the web build (node_modules here are built for Alpine)
docker run --rm -u 1000:1000 -v "$PWD":/repo -w /repo/frontend -e HOME=/tmp node:22-alpine npx vitest run
```

- `deploy/e2e.sh up` starts a throwaway copy of the production stack as project `pm-e2e`, bound to `127.0.0.1:8650` with its own volumes and subnets, so it can run next to production. `deploy/e2e.sh down` removes it, including volumes.
- Backend: Python 3.12+, FastAPI, SQLAlchemy 2, Alembic (`backend/migrations/`). Frontend: Vite, React, TypeScript.

## Data, backups and restore

- `deploy/backup.sh` writes a `pg_dump` custom-format file to `$PM_BACKUP_DIR` (default `~/backups/portfolio-manager`) and removes dumps older than 30 days. Run it nightly from cron, for example at 02:00.
- `deploy/restore-test.sh` restores the newest dump into a throwaway `postgres:16-alpine` container (no ports, never the live database), checks `alembic_version` and the core tables have rows, and fails if the dump is more than 2 days old. It prints one `restore-test ok` or `restore-test FAILED` line. Run it monthly from cron: `30 3 * * 0 [ "$(date +\%d)" -le 07 ] && /bin/bash $HOME/portfolio-manager/deploy/restore-test.sh >> $HOME/backups/portfolio-manager/restore-test.log 2>&1` (first Sunday, 03:30).
- `deploy/restore.sh <file.dump>` stops the app, drops and recreates the database, restores the dump and starts the app again. It destroys current data.
- Keep a copy of the dumps off the host as well.
- Back up `deploy/.env` separately.

## Versioning and releases

The project uses [Semantic Versioning](https://semver.org/). A release is a git tag `vX.Y.Z` on `main`. [CHANGELOG.md](CHANGELOG.md) is the single source of release notes; the app shows it under "What's new", so keep its format exact (see the header of that file).

To release:

1. Move the entries under `## [Unreleased]` to a new heading `## [X.Y.Z] - YYYY-MM-DD` and leave an empty `## [Unreleased]` above it.
2. Bump the version in `frontend/package.json` (a frontend test checks it matches the newest changelog release), `backend/pyproject.toml` and `backend/app/__init__.py`.
3. Commit.
4. Tag: `git tag vX.Y.Z`.
5. Push `main` and the tag: `git push origin main vX.Y.Z`, then deploy (`cd deploy && docker compose up -d --build`, all services together).

Write entries for the app's user (what they can now do), as single `- ` lines under `### Added`, `### Changed`, `### Fixed`, `### Removed` or `### Security`.

## Repository layout

```
backend/     FastAPI app (app/modules, app/platform), Alembic migrations, tests
frontend/    Vite + React + TypeScript SPA (src/screens, src/components)
deploy/      compose files, Caddyfile, egress proxy, firewall unit, backup/restore/e2e scripts
rules/       Finnish tax rule tables per year (YAML)
fixtures/    sample exports used by tests
```

## Disclaimer

This is a personal project, shared as is. Tax, pension and planning figures are estimates built from published Finnish rules and your own inputs; they are not financial, tax or legal advice, and the rule tables in `rules/` may be out of date. Check anything that matters against vero.fi, your bank or an adviser.

The app reads prices and fund data from public but unofficial endpoints (Yahoo Finance, Morningstar, Nordnet). They can change or stop working at any time, and their terms of use apply to you. The app never moves money or places trades.

## Licence

[PolyForm Noncommercial 1.0.0](LICENSE): free to use, change and share for any noncommercial purpose, including personal use. Commercial use is not permitted.
