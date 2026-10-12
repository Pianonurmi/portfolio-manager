# Changelog

All notable changes to this project. Versions follow Semantic Versioning; each release is a git tag `vX.Y.Z`.

## [Unreleased]

## [1.12.1] - 2026-10-12

Fixes to the tax estimate, scheduled jobs that ran a day late, and a worker that can no longer fall silently behind.

### Added
- Settings → Jobs shows the worker's version and last heartbeat, and the attention list warns when the worker has stopped or runs an older version than the app.
- Tax: foreign withholding is shown country by country, with the credit Finland gives and the amount to reclaim from the source country.
- `deploy/restore-test.sh` restores the newest backup into a throwaway database and checks it, for a monthly cron job.

### Changed
- Routine price runs skip closed positions whose price history already reaches the sale, or that have no price source; the full price backfill still covers them.

### Fixed
- Weekly and weekday jobs ran a day late: the evening prices on Tuesday to Saturday instead of Monday to Friday, the weekly digest, deposit rates and housing indices on Tuesday, and the Sunday jobs on Monday.
- Jobs that fell due while the worker restarted were skipped; the worker now runs them when it starts.
- Settings → Connections failed to load (server error).
- Tax: a capital loss is first deducted from the same year's dividends and crypto rewards, so a smaller loss is carried forward (it was overstated, and the estimated tax too low). Losses older than five years now take their share of the gains in between.
- Tax: the foreign tax credit is limited to the treaty rate (15 %, Switzerland 10 %) and to the Finnish tax on that country's income. Withholding above it, such as Switzerland's 35 % or Germany's 26.4 %, was credited in full.
- Price runs no longer look up instruments that only appear in voided bookings.

## [1.12.0] - 2026-10-12

Benchmarks, Nordnet's real fees for monthly saving and options, and a valuation fix for split funds.

### Added
- Portfolio → Benchmarks: your time-weighted return against MSCI ACWI IMI, MSCI ACWI, FTSE All-World, MSCI World, World Small Cap, S&P 500, Nasdaq 100, MSCI Europe, MSCI EM IMI, OMX Helsinki 25, global bonds (EUR hedged) and a 60/40 mix.
  - Each index is an accumulating EUR UCITS ETF that tracks it: dividends reinvested and costs paid, in euros.
  - It shows CAGR, your excess return, volatility, deepest fall, Sharpe ratio, beta, correlation and tracking error for 1, 3, 5 and 10 years or the maximum, with a chart.
  - Price histories refresh weekly.
- Hedging: puts and collars on indices where Nordnet has no standardised options (only the Nordic exchanges have them) are marked "not at Nordnet".

### Changed
- New-money plan: Nordnet's ETF monthly-saving fee is 2.50 € per month per savings agreement of up to 4 ETFs, not per ETF bought (per Nordnet's price list).
- Hedging: option costs follow Nordnet's price list: brokerage 0.40–0.80 % of the premium (at least 15 €) plus 0.75 % clearing, on both legs of a collar.

### Fixed
- A fund whose units split (for example 1:25) was valued about 25× too low before the split, because Morningstar's price history is split-adjusted while the ledger holds the old units until the split date.
  - This affected the portfolio value history, Returns, Risk and every comparison before February 2026.
  - Valuation now scales such prices back to the units held then, and per-unit return series scale across booked splits.
- The tax-loss list no longer fails on a lot priced in a currency without exchange rates.

## [1.11.0] - 2026-10-12

Hedging without selling, tax-loss harvesting, and borrowing against the home compared with Nordnet credit.

### Added
- Portfolio → Hedging without selling. It measures your portfolio against six indices (S&P 500, Nasdaq 100, Euro Stoxx 50, DAX, OMX Helsinki 25, OMX Stockholm 30) on its own monthly history, in euros. It then compares on the same simulated paths:
  - protective puts and zero-cost collars, priced with Black–Scholes at realised volatility plus a skew premium;
  - short mini futures (financing and knock-outs) and bear certificates (daily-reset decay);
  - selling part now, with the tax that realises, selling inside the osakesäästötili first.

  It shows each strategy's expected cost, median, bad case, chance of losing over 20 %, deepest fall, upfront payment and tax. A payoff chart covers index moves from −40 % to +20 %. Your strategies are editable.
- The hedge's own gain is taxed when it is closed, while the fall it offsets stays an unrealised, non-deductible loss; the comparison counts this.
- Nordnet Markets products to do it with: about 5 000 index short products (mini shorts with their leverage and distance to the stop, bear certificates), refreshed weekly from Nordnet's public lists.
- Index levels from Yahoo, kept current by the price job.
- Monte Carlo: an optional rolling hedge (put, collar or short) on the portfolio, with its premium cost and payoff a year.
- Tax → Harvest losses before 31.12. For each holding at a loss, it shows what selling and rebuying at once saves this year after trading costs. Finland has no wash-sale rule. It also covers what carries forward, the small-disposals rule, and a warning when rebuying would restart the 10-year clock for the 40 % deemed acquisition cost.
- Leverage → "Borrow against the home instead?": a mortgage top-up (12-month Euribor plus your margin) against Nordnet credit for the same amount. It compares rate after tax, payment, room under the bank's loan-to-value limit, and margin-call against fixed-payment risk.
- Monte Carlo leverage can be set to "no margin calls" for money borrowed against the home.

## [1.10.0] - 2026-10-12

Leverage: what borrowing against the portfolio (Nordnet securities credit) does to every plan.

### Added
- Nordnet credit terms as a dated rule table (`rules/nordnet_credit-2026.yaml`, from Nordnet's public pages): the Superluotto tiers with their conditions, the rate changes from 15 October 2026 (Kulta 3.94 % → 4.44 %, Hopea 4.49 % → 4.65 %, base 4.85 %) and the diversification table that raises collateral rates up to 90 %.
- Collateral rates per holding from Nordnet's public lists (shares, funds, ETFs), refreshed weekly. You can set your own rate per holding.
- Portfolio → Leverage, for the Nordnet book-entry account:
  - collateral value with the diversification bonus, credit used, loan-to-collateral and the tier in effect, with each tier condition's actual value against its limit;
  - interest before and after the capital-income deduction;
  - the fall that would trigger a margin call, and the loan-to-collateral after a 10–50 % fall;
  - a "borrow to invest" what-if showing the same figures after borrowing.
- Monte Carlo: borrowing ratio, re-levering policy (every year, every month, or borrow once and let it drift) and collateral rate. A comparison runs 1×, 1.25×, 1.5× and 2× on the same paths.
  - Interest follows the tier the loan-to-collateral ratio earns, less the 30 % deduction.
  - A margin call sells down and realises the gain share, taken from your lots, with its tax.
  - The loan is repaid at retirement by default, with the tax on that sale shown separately from the value before it.
  - Reports the chance of a margin call and of losing everything, plus interest and forced-sale tax per year.
- Allocation advisor: "Allow borrowing" extends the efficient frontier past 100 % at Nordnet's rate after the tax deduction. Only cash-free mixes are levered, since cash in a credit account just repays it. It adds a limit on the chance of a margin call within a year. The glide path never borrows.
- New-money plan: shows the credit and its after-tax rate, and can pay the credit down before buying.
- Tax rules: interest on debt for taxable investments is deductible from capital income, with the deficit credit (30 %, at most 1 400 €).

## [1.9.0] - 2026-10-12

Targets that come from your risk limits, glide paths, real interest on cash, and plans that know what a buy costs.

### Added
- Portfolio → Allocation advisor: give the years until the money is needed, the loss you accept in a bad year (the 1-in-20 one-year loss) and the chance you accept of being below cost at that date. It picks the efficient mix of shares, bonds and cash with the highest expected return inside those limits.
  - It shows today's mix against the advised one: median and bad (P10) value at the date with your monthly saving, the chance of being below what you put in, and the deepest fall along the way.
  - Any other efficient mix can be picked from the table. The return and risk assumptions can be edited.
- Glide paths: the advisor repeats its choice year by year with the time left, so the mix moves from shares towards bonds and cash as the date nears and never adds risk back. The path can be edited point by point.
  - Saved with the targets, the path drives rebalancing (today's point), the new-money plan (month by month, can be turned off) and, optionally, the Monte Carlo simulator ("Follow my glide path").
- Planning → Cash & interest ladder.
  - Market rates refresh weekly from the ECB: Finnish household deposit averages, Euribor 3/6/12 months, the AAA 2-year yield and €STR. Defaults for a savings account and 3-, 6-, 12- and 24-month term deposits are derived from them, and your own bank's offers override them.
  - The ladder builder splits an amount over terms, keeps part liquid and renews maturing deposits into the longest term, the same term or the savings account.
  - It shows each deposit's maturity, the money becoming available month by month, and interest after the 30 % tax at source. It compares the ladder with keeping everything in the savings account or in 12- or 24-month deposits.
- Cash earns a real rate everywhere: the cash class's expected return in the advisor, the new-money plan and the Monte Carlo defaults is your own savings rate when entered, otherwise the 12-month deposit rate.
- New-money plan, costs: ETF buys cost 2.50 € each (Nordnet monthly saving) and fund buys nothing. All fees are editable.
  - An ETF is bought only when its fee is at most 1 % of the buy, so 250 € or more by default. Smaller amounts go to a fee-free fund doing the same job, or wait in cash until they are big enough.
  - The plan shows fees paid over the period and as a share of the money, and how many months the money waited.

## [1.8.0] - 2026-10-12

The ETF screener now shows what you can actually buy at your brokers.

### Added
- ETF screener: "Available at" starts on Nordnet and shows only the ETFs Nordnet Finland sells. The list comes from Nordnet's own public ETF list, refreshed weekly with the screener. It covers about 2 240 ETFs, mostly on Xetra plus a few in Stockholm and Copenhagen.
  - "Nordnet, monthly saving" narrows the list to ETFs you can set up a monthly saving plan for.
  - A sortable column shows how many Nordnet customers own each ETF.
  - The detail and compare views show Nordnet's ticker and exchange.
  - Marking an ETF as a candidate now prices it from the listing Nordnet trades.
- Nordea publishes no ETF list, so availability there cannot be checked. The screen says so and points to Nordea's 1 % fee cap on all ETFs.
- `www.nordnet.fi` is added to the outbound allowlist. Run `deploy/init-env.sh` again, or add it to `PM_OUTBOUND_ALLOWLIST`, on an existing install.

## [1.7.0] - 2026-10-12

Rebalancing with new money only, and an ETF screener whose funds feed the plans.

### Added
- Portfolio → New-money plan: reach your target mix by buying only, so nothing is sold and no gain is taxed. Enter a monthly amount, a lump sum, a yearly raise and the smallest buy worth making. It shows:
  - this month's buys per fund, and the account to buy in;
  - the class weights month by month against the targets;
  - when the portfolio gets inside the band and on target, and the monthly amount that would get there within a year;
  - what rebalancing by selling instead would cost in tax.

  Funds count by what they hold, so an 80/20 fund adds both shares and bonds with every purchase, and the plan says when your funds cannot make the target mix at all. Similar funds share the money in proportion to what you own, or equally. You can also steer the regions of the equity part.
- Scenarios for the plan: the same buying under expected returns, a 30 % share crash, a 20 % rally, a bond sell-off and flat markets. You can compare up to three sets of funds side by side, for example your current ETFs against a single 80/20 fund.
- ETF screener: about 4 000 European-listed UCITS ETFs on the exchanges Finnish brokers reach (Xetra, Helsinki, Euronext, Nordic exchanges), refreshed weekly from Morningstar.
  - Filter by category, provider, TER, size, share of equities, accumulating or distributing, currency hedging and age.
  - Sort by returns, volatility, Sharpe ratio and drawdown, and compare up to four ETFs side by side.
- Screener candidates: mark an ETF as a candidate and its look-through profile and full price history are fetched. Candidates then appear in the new-money plan, and in the holdings optimiser when "Include screener candidates" is ticked, at 0 % today.

## [1.6.0] - 2026-10-12

The remaining roadmap items: fund data read from factsheets by the local AI, and the emergency package as an encrypted PDF.

### Added
- Fund factsheets and KIDs: upload one under Documents & statements and the local AI reads its ongoing charge, asset split, regions, countries, sectors, company sizes and bond duration and yield. You review the figures (anything whose number is not in the document text is marked "check"), pick the fund (pre-selected by ISIN) and save them as its fund profile, which the X-ray uses wherever Morningstar has no data. Names the app does not recognise are left out unless you assign them.
- Life → "If something happens to me": download the emergency package as a password-protected PDF (AES-256, printing allowed; the password is asked each time and never stored). Optionally a fresh encrypted copy is written on the 1st of each month to a folder you choose (default the imports folder under estate/), keeping the latest few; the passphrase is stored encrypted and never shown again. The package now also lists the people and splits debts from assets.

## [1.5.0] - 2026-10-11

Portfolio optimisation and factor analysis, and steadier prices for holdings that trade in another currency.

### Added
- Portfolio → Optimise: an efficient frontier and a suggested mix for the four asset classes (with return, risk and correlation assumptions you can edit) or for your holdings (returns and shrunk covariance estimated from their own monthly total returns). Goals: best return per risk, lowest risk, a target return or risk, or equal risk from each asset, with per-asset weight limits, the euro amount to buy or sell per asset and the turnover. Nothing is traded.
- Portfolio → Factors: a Fama-French regression of the portfolio and its 12 largest holdings on market, size, value, profitability, investment and momentum (Kenneth R. French Data Library, refreshed monthly), with loadings, t-statistics, alpha, R² and plain-language readings.

### Fixed
- In the asset-class optimisation, cash is the risk-free asset: Sharpe ratios are measured against its expected return, so the best-return-per-risk mix is no longer almost all cash.

### Changed
- An instrument that Yahoo quotes in another currency (Nordea in SEK, funds listed in USD) is priced from Morningstar's listing of the same ISIN in its own currency when one exists, so currency noise no longer distorts its monthly risk figures; Yahoo's dividends and splits are still used.

## [1.4.0] - 2026-10-11

Dividends in every return figure, a cleaner instrument list, and the first life modules beyond finance: goals and habits, a calendar and a health log.

### Added
- Risk and the historical simulator now use total return: dividends and fund distributions (from Yahoo, else derived from your own dividend bookings) are reinvested in each holding's and the benchmark's returns, and the report says where the dividend data came from and which distributing holdings have none.
- Income: the 12-month forecast is now filled from each holding's last 12 months of distributions per unit (Yahoo, else your own bookings), with the forecast yield on current value.
- Settings → Instruments: a "Possible duplicates" panel finds the same security entered twice (same name, same Morningstar id, or a clashing ISIN) and merges them into one, moving postings, prices, profiles and name mappings.
- Goals: set savings, net worth, investment portfolio, debt payoff or custom goals with a target and date. Net worth and portfolio goals read your accounts live; the page shows progress, the monthly pace you need against the pace you have, whether you are on track, and when you would get there at the current pace.
- Habits: daily or weekly habits with check-ins, current and best streak, the last 30 days and a warning when a streak breaks.
- Calendar: your own events (all day or timed, repeating weekly, monthly or yearly, with a reminder) together with the dates the app already knows: insurance renewals, katsastus and vehicle tax, home maintenance and warranties, subscription charges, loan rate resets and goal dates. Month grid, a 60-day agenda, filters, and a `.ics` download for your phone calendar.
- Health: per person, log weight, resting heart rate, blood pressure, sleep, steps or your own measurements; see the latest value, a trend chart with your target, and 30 and 90-day averages. Medications with refill dates that show up in the calendar. Health data is never sent to the AI, the weekly digest or push notifications.
- Goals, habits and calendar events appear in search, the timeline and the needs-attention list.

### Fixed
- Stocks that have no ISIN are looked up by name again on every daily price run, so a miss recorded earlier (such as Rapala VMC, which Morningstar lists as "Corp") no longer sticks until a manual full fetch.

## [1.3.0] - 2026-10-11

Plan contributions and withdrawals on your own portfolio, and longer risk history.

### Added
- Planning: a Monte Carlo for contributions and withdrawals on your own portfolio. Simulate monthly contributions and lump sums, then five withdrawal strategies (fixed real amount, 4 % rule, percent of portfolio, guardrails, variable) on the same random paths, with success probability, a fan chart, safe and perpetual withdrawal rates, an optional target, and either capital-market assumptions or a bootstrap of your portfolio's own price history.
- Risk: holdings with a short price history (such as a fund that replaced an older one) are extended with the monthly returns of a closely correlated holding or your benchmark, so 3Y, 5Y and 10Y cover longer periods; substitutions are listed and can be switched off.
- The historical model warns when only a few years of your portfolio's history are stretched over a long plan, and shows how much those years returned.

### Changed
- The simulator shows its results beside the inputs on wide screens, so every change is visible at once.
- The variable (VPW) strategy plans with the growth a portfolio actually compounds at, so it no longer spends faster than the typical path earns.

## [1.2.0] - 2026-10-11

Risk and correlations of the portfolio as it stands today, price feeds that no longer stop silently, and much faster portfolio pages.

### Added
- Portfolio risk: volatility, Sharpe and Sortino, maximum drawdown with dates, monthly VaR/CVaR, beta against your benchmark, drawdown and rolling-return charts, a correlation heatmap and each holding's share of portfolio risk, measured by applying today's weights to past monthly returns.

- Holdings whose price history is missing are listed next to the risk figures with the share of the portfolio they represent, instead of hiding the figures.

### Changed
- The portfolio pages load in well under a second: prices are kept in memory and re-read only when they change.

### Fixed
- ETFs that Yahoo lists without any prices (Amundi Core MSCI World, WisdomTree) and thinly traded listings are priced from Morningstar.
- The price coverage panel shows a holding whose feed delivers nothing as "no feed", even when a recent statement price exists.

## [1.1.0] - 2026-10-11

Look-through analytics: what the portfolio really holds once every ETF and fund is opened up, what it costs per year, and release notes in the app.

### Added
- Portfolio X-ray: true asset class, regions, countries, sectors, market-cap size, style and bond credit of the whole portfolio, looking through every ETF and fund (Morningstar data).
- Total fund costs (TER) in % and €/year and bond duration, yield and credit quality.
- Per-instrument profile with source and date; correct any figure by hand.
- Allocation targets and rebalancing count a balanced fund by its real stock/bond split.
- Weekly refresh of fund data from Morningstar.
- What's new page listing the changes in each release.

### Changed
- Fund running costs use the TER from Morningstar.

### Fixed
- Index funds that a statement recorded as shares are recognised as funds and looked through, instead of counting as one company in their home country.

## [1.0.0] - 2026-10-11

First versioned release. It covers the whole suite as built so far: ledger and portfolio, prices, net worth, cash flow, tax, planning, local-AI statement reading, crypto, property and life modules, running on a LAN-only home server.

### Added
- Double-entry ledger where positions, cost basis and net worth are derived and can be recomputed; corrections supersede earlier entries instead of deleting them.
- Nordnet CSV import (Finnish and English exports) with staging, review, instrument mapping, duplicate detection and reconciliation against Nordnet's own running quantity and cash balance.
- Nordnet corporate actions and fee types: splits, exchanges, redemptions, IPO placeholders, rights, ETF savings fee and loan interest.
- Holdings, FIFO cost basis, TWR and XIRR per account, person and household for YTD, 1Y, 3Y, 5Y and Max, plus dividends and income.
- A dashboard for each child with value, contributions, gain, return and projection.
- Allocation targets, drift view and tax-aware rebalancing.
- Daily prices from Yahoo and Morningstar (lt.morningstar.com) and ECB exchange rates, with history backfill and stale or missing prices flagged instead of shown as zero.
- Price feeds for every holding: funds are priced from Morningstar NAV, and holdings without an ISIN are found by name.
- Price coverage panel in Settings that shows each holding as ok, stale, no feed or missing, with the reason.
- Net worth for the household and per person with ownership shares, history and a breakdown of what changed.
- Home, housing-company loan, mortgage and other manual assets and liabilities, with valuations and loan terms; loan what-if scenarios.
- Housing value estimated from Statistics Finland price indices, optionally revalued every quarter.
- Cash flow from YNAB (incremental sync, balances in net worth, optional push back, spending analytics) and Actual Budget as an alternative backend.
- Housing costs taken from budget categories.
- Crypto balances from Kraken and Binance read-only API keys and public addresses.
- Crypto history imports (Kraken ledger CSV and API, Binance statement, Ledger Live, generic CSV) with swaps as disposals, rewards as income and transfer matching.
- Finnish tax engine with rule tables for 2017-2023, 2024-2025 and 2026: gains with the deemed cost, OST, small-disposal rule, gift and inheritance tax, year-to-date estimate and crypto tax views.
- Planning studio: pension (työeläke) estimate, deterministic and Monte Carlo retirement projection, life events, inheritances and scenario comparison, with live what-if changes.
- Stress tests, fee analysis and backtests.
- Tax-aware withdrawal order and a comparison of where new monthly savings should go.
- Local-AI statement reading: upload a PDF, scan, screenshot, CSV or Excel statement and a local model on the LAN reads it page by page.
- Checks on statement reading: holdings against the page text, quantity times price against value, and holdings plus cash against the statement total.
- Confirmed statements can be compared with the ledger, booked as quantities, or recorded as a total value, and booking can be undone.
- Nordnet lot exports are read exactly, and committing imported history replaces positions that were booked from statements.
- Extraction of values from Paperless-ngx documents with the quoted source text, and plain-language explanations of plans.
- Paperless-ngx links, tags and notes on any record.
- Alerts and a weekly digest through Gotify and email.
- Subscriptions detected from budget transactions with yearly cost, price increases and renewal warnings.
- Vehicles (inspection, insurance and tax dates, odometer, service log, cost per km, depreciation) and a home maintenance log.
- Insurance register with adequacy check and an estate package.
- Shared timeline, search and an attention list across modules; every figure opens a provenance tree back to its source row.
- Single-owner sign-in with optional TOTP; network mode signs in without a password from the LAN or VPN while still requiring sign-in from public addresses.
- Data export, nightly database backups with a tested restore, and a settings page showing jobs and every outbound connection.

### Changed
- Nordea OST statements are classified from the opening pages together, so bank overviews that show only a total on page 1 are read correctly.
- Statement reading repairs rows printed over several lines, and decimal-comma units and overlapping page tiles are handled.
- Statement reading limits itself to two requests in flight to the AI gateway and backs off when it is busy.

### Fixed
- Statement dates are never in the future; a footer such as 10/11/26 is read as month/day.
- Small crypto disposals no longer show a quantity of 0 in the tax views.
- A dash in an empty statement column is no longer read as a minus sign.
- Uploads with non-ASCII task names no longer fail.
