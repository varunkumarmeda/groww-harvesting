# Groww Tax-Loss Harvesting

Reads Groww's **Tax Loss Harvesting Report** (XLSX) and builds a sell plan. Optionally it
checks the plan against live holdings and places the SELL orders through the official Groww
Trade API (`growwapi`). Dry-run is the default.

> Not tax advice. The tax rules below are simplified; confirm with a CA before acting.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Put ONE credential option in `.env` (never commit or share it):

```env
# Option 1: access token from the Groww Trade API page (expires daily)
GROWW_ACCESS_TOKEN=...
# Option 2: API key + TOTP secret (no daily approval)
GROWW_API_KEY=...
GROWW_TOTP_SECRET=...
# Option 3: API key + secret (approve once a day in Groww)
GROWW_API_KEY=...
GROWW_API_SECRET=...
```

Download the report from Groww (Reports → Tax Loss Harvesting) into this folder.

## Usage

```bash
# 1. See every loss-making holding (uses the newest report in the folder)
python harvest.py

# 2. Enter gains you've already realised this FY (Groww → Reports → Capital Gains).
#    The tool then selects only the holdings that cut this year's tax.
python harvest.py --stcg 60000 --ltcg 180000

# 3. Check the plan against live holdings (trading symbols, quantities, FIFO safety)
python harvest.py --stcg 60000 --ltcg 180000 --holdings

# 4. Place the orders (asks you to type SELL first)
python harvest.py --stcg 60000 --ltcg 180000 --holdings --execute
```

Other flags: `--asset stocks|mf|all`, `--term st|lt|all`, `--min-loss 100` (skip losses
smaller than charges), `--harvest-all` (also book losses to carry forward),
`--holdings-file holdings/holdings_*.json` (plan offline from a saved snapshot),
`--csv path`. Every run saves the plan to `output/plan_*.csv`. Executed orders are
logged to `output/orders_*.json`.

## Portfolio report from Groww exports (recommended)

Export these from Groww (Reports, Stocks) into one folder:

| Export | Used for |
|---|---|
| Holdings Statement | quantities, closing prices, portfolio overview |
| P&L Report (FY start → today) | every open **lot** with buy date and price ("Unrealised trades") |
| Capital Gains Report (current FY) | STCG/LTCG already realised this year |
| Order History | trading symbols |

```bash
python portfolio_report.py --exports "C:\path\to\groww_exports" --open
# or set GROWW_EXPORTS_DIR in .env and just run: python portfolio_report.py --open
```

No API token needed. If Groww API credentials are in `.env` (a TOTP key is best), it also
compares the export with your **live holdings**. If anything changed since you exported
(a buy, sell or IPO allotment), it warns you to re-export (`--no-live-check` to skip). Live
prices aren't used: market data needs a Groww API plan this key doesn't have.

Because it works lot by lot, it knows exactly what a FIFO sale of N units
books, and plans:

1. **Loss harvesting:** the fewest units to sell so short-term losses cancel this year's
   taxable gains, net of charges.
2. **Gain harvesting (optional):** long-term equity gains to book tax-free inside the ₹1.25L
   exemption, then buy back to raise your cost basis.
3. **Day 2 buy-back list,** a tax before/after table, and a **watch-list** of lots turning
   long-term in the next 60 days.
4. **Portfolio overview:** allocation, concentration, and lot-level ST/LT unrealised P&L per holding.

It handles bonus (zero-cost) lots. Gold/silver ETFs are a separate tax bucket (no 112A
exemption) and bonds are left out. Flags: `--stcg/--ltcg` (override the CG report),
`--sell-date`, `--cost-pct 0.25`, `--fixed-cost 40`, `--min-benefit 100`, `--no-gain-harvest`.

## Portfolio report from the Tax Loss Harvesting report

```bash
python portfolio_report.py --stcg 60000 --ltcg 180000 --holdings --open
```

Writes `output/portfolio_report_<timestamp>.html`, a single page you can open in any browser or print. It covers:

- **Action plan:** what to sell on Day 1 (with FIFO, pledge and cost warnings) and what to buy back on Day 2, the next trading day.
- **Tax:** tax before and after, carry-forward, and any wasted loss.
- **Not in the plan:** holdings that were considered and why they were left out.
- **Portfolio overview (with `--holdings`):** value, P&L, weights and top-5 concentration.
- **Warnings:** a stale report, or totals that don't match the report's Summary sheet.

It defaults to short-term losses (`--term st`). Other flags: `--sell-date YYYY-MM-DD`,
`--cost-pct 0.25` (approximate round-trip charges), `--no-ltp`, `--out`, plus every
planning flag from `harvest.py`.

`python get_holdings.py` prints holdings with live P&L and saves a snapshot to `holdings/`.

## How the plan is built

| Rule | What the tool does |
|---|---|
| **FIFO.** A sale is matched against your oldest units first, so you can't sell only the short-term lot. | Sells every loss lot of a holding together (LT + ST). Marks a holding **REVIEW** if you own more units than the report shows in loss, because FIFO could sell profitable lots. |
| **Set-off.** STCL offsets STCG, then LTCG. LTCL offsets LTCG only. | Models both when you pass `--stcg/--ltcg`. |
| **₹1.25L LTCG exemption.** A loss that only offsets LTCG that was already exempt is wasted. | Selects holdings greedily by marginal tax saved and warns about wasted loss. |
| **Carry-forward.** Unused losses carry forward 8 years if you file the ITR on time. | Shown in the estimate; `--harvest-all` to book them anyway. |
| **Rebuy timing.** A same-day sell and buy-back of the same stock is netted as intraday. | Reminds you to rebuy on the next trading day. |

Rates default to 20% STCG / 12.5% LTCG / ₹1.25L exemption (rates from 23 Jul 2024). Change
them with `--stcg-rate`, `--ltcg-rate` and `--ltcg-exemption`. Surcharge and cess are not
included.

Only holdings with status **OK** (verified against live holdings, whole quantity, nothing
pledged) are ever auto-sold. Mutual funds always need manual redemption in the app, because
the Trade API covers equity only.

## Tests

```bash
python -m unittest discover tests
```

## Files

- `harvest.py`: CLI (plan, verify, execute)
- `portfolio_report.py`: HTML portfolio and harvesting report (`--exports` for lot-level mode)
- `exports.py`: parser for Groww Holdings / P&L / Capital Gains / Order History exports
- `lot_planner.py`: FIFO lot engine, loss + gain harvesting, term watch-list
- `exports_report.py`: HTML for the exports mode
- `report.py`: XLSX parser, cross-checked against the report's Summary sheet
- `planner.py`: grouping, FIFO/holdings checks, tax model, selection
- `groww_auth.py`: credentials from `.env`; holdings fetch
- `get_holdings.py`: holdings viewer and snapshot
- `harvest_stcg.py`: deprecated wrapper for `harvest.py --asset stocks --term st`
- `get_holdings_node.js`: legacy, unmaintained (doesn't load `.env`)
