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
- `report.py`: XLSX parser, cross-checked against the report's Summary sheet
- `planner.py`: grouping, FIFO/holdings checks, tax model, selection
- `groww_auth.py`: credentials from `.env`; holdings fetch
- `get_holdings.py`: holdings viewer and snapshot
- `harvest_stcg.py`: deprecated wrapper for `harvest.py --asset stocks --term st`
- `get_holdings_node.js`: legacy, unmaintained (doesn't load `.env`)
