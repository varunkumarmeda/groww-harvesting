"""Tests for Groww-export parsing and the lot-level planner (synthetic data only)."""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exports import OpenLot, load_exports  # noqa: E402
from lot_planner import (  # noqa: E402
    DEBT, EQUITY, GOLD_SILVER, Position, asset_class, build_positions, is_long_term, plan_from_positions,
    upcoming_term_changes,
)
from planner import TaxParams, compute_tax  # noqa: E402

P = TaxParams()
HDR = ["Stock name", "ISIN", "Quantity", "Buy date", "Buy price", "Buy value", "Sell date", "Sell price",
       "Sell value", "Realised P&L", "Remark"]


def _wb(path, rows, title="Sheet1"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    for r in rows:
        ws.append(r)
    wb.save(path)


def make_exports(folder: Path):
    _wb(folder / "Stocks_Holdings_Statement_1_26-11-2026.xlsx", [
        ["Name", "X"], ["Holdings statement for stocks as on 26-11-2026"], ["Summary"],
        ["Stock Name", "ISIN", "Quantity", "Average buy price", "Buy value", "Closing price", "Closing value", "Unrealised P&L"],
        ["ALPHA LTD", "INE000A00001", 10, 100, 1000, 80, 800, -200],
        ["GROWWAMC - GROWWGOLD", "INF000G00001", 100, 10, 1000, 12, 1200, 200],
    ])
    _wb(folder / "Stocks_PnL_Report_1_01-04-2026_26-11-2026.xlsx", [
        ["Name", "X"], ["Realised trades"], HDR,
        ["Unrealised trades"],
        ["Stock name", "ISIN", "Quantity", "Buy date", "Buy price", "Buy value", "Closing date", "Closing price",
         "Closing value", "Unrealised P&L", "Remark"],
        ["ALPHA LTD", "INE000A00001", 4, "01-01-2024", 50, 200, "26-11-2026", 80, 320, 120, ""],
        ["ALPHA LTD", "INE000A00001", 6, "01-06-2026", 133.33, 800, "26-11-2026", 80, 480, -320, ""],
        ["GROWWAMC - GROWWGOLD", "INF000G00001", 100, "01-01-2024", 10, 1000, "26-11-2026", 12, 1200, 200, ""],
        ["Disclaimer: ", None],
    ], title="Trade Level")
    _wb(folder / "Stocks_Capital_Gains_Report_1_01-04-2026_31-03-2027.xlsx", [
        ["Name", "X"], ["Capital Gains Statement for stocks from 01-04-2026 To 31-03-2027"], ["Summary"],
        ["Charges"], ["Total", 50], ["Realised P&L"], ["Short Term P&L", 1000], ["Long Term P&L", 0],
        ["Dividends", 10], ["Short Term trades"], HDR,
        ["BETA LTD", "INE000B00001", 1, "01-05-2026", 100, 100, "01-06-2026", 1100, 1100, 1000, ""],
        ["Long Term trades"], HDR, ["Disclaimer: "],
    ])
    _wb(folder / "Stocks_Order_History_1_01-04-2025_26-11-2026.xlsx", [
        ["Name", "X"],
        ["Stock name", "Symbol", "ISIN", "Type", "Quantity", "Value", "Exchange", "Id", "When", "Status"],
        ["ALPHA LTD", "ALPHA", "INE000A00001", "BUY", 6, 800, "NSE", "1", "01-06-2026 10:00 AM", "Executed"],
    ])


class ExportsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_exports(Path(self.tmp.name))
        self.ex = load_exports(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse(self):
        ex = self.ex
        self.assertEqual(ex.as_of, date(2026, 11, 26))
        self.assertEqual(ex.fy_label, "FY 2026-27")
        self.assertEqual((ex.realised_st, ex.realised_lt), (1000, 0))
        self.assertEqual(ex.charges_fy, 50)
        self.assertEqual(len(ex.open_lots), 3)
        self.assertEqual(len(ex.realised_trades), 1)
        self.assertEqual(ex.symbols["INE000A00001"], "ALPHA")
        self.assertEqual(ex.warnings, [])  # lots add up to holdings
        self.assertEqual(ex.holdings[0].invested, 1000)

    def test_live_compare(self):
        from exports import compare_with_live

        same = [{"isin": "INE000A00001", "quantity": 10}, {"isin": "INF000G00001", "quantity": 100}]
        self.assertEqual(compare_with_live(self.ex, same), [])
        changed = [{"isin": "INE000A00001", "quantity": 4},
                   {"isin": "INE999Z00001", "trading_symbol": "NEWCO", "quantity": 3}]
        diffs = compare_with_live(self.ex, changed)
        self.assertEqual(len(diffs), 3)  # ALPHA changed, GOLD gone, NEWCO new
        self.assertTrue(any("NEWCO" in d for d in diffs))

    def test_positions_and_fifo(self):
        pos = {p.isin: p for p in build_positions(self.ex)}
        alpha = pos["INE000A00001"]
        self.assertEqual(alpha.label, "ALPHA")
        sd = date(2026, 11, 30)
        st, lt = alpha.sale(5, sd)  # 4 old LT units, then 1 new ST unit
        self.assertAlmostEqual(lt, 4 * 30)
        self.assertAlmostEqual(st, 80 - 133.33, places=2)
        self.assertEqual(alpha.lt_prefix_units(sd), 4)

    def test_plan_offsets_stcg_and_taxes_gold_lt(self):
        positions = build_positions(self.ex)
        plan = plan_from_positions(positions, date(2026, 11, 30), 1000, 0, P, fixed_cost=0, cost_pct=0)
        self.assertEqual([t.pos.label for t in plan.loss_trades], ["ALPHA"])
        self.assertEqual(plan.loss_trades[0].qty, 10)  # must sell through the LT lot to reach ST loss
        # Gold is never used for gain harvesting (no 112A exemption).
        self.assertNotIn("GROWWAMC - GROWWGOLD", [t.pos.label for t in plan.gain_trades])


class TermTests(unittest.TestCase):
    def test_more_than_12_months(self):
        self.assertFalse(is_long_term(date(2025, 11, 30), date(2026, 11, 30)))
        self.assertTrue(is_long_term(date(2025, 11, 29), date(2026, 11, 30)))
        self.assertTrue(is_long_term(date(2024, 2, 29), date(2025, 3, 1)))

    def test_asset_class(self):
        self.assertEqual(asset_class("ACAPL-12.50%-28-6-28-PVT", "INE08XP07548"), DEBT)
        self.assertEqual(asset_class("GROWWAMC - GROWWSLVR", "INF666M01OF4"), GOLD_SILVER)
        self.assertEqual(asset_class("NIP IND ETF NIFTY BEES", "INF204KB14I2"), EQUITY)

    def test_watchlist(self):
        lot = OpenLot("I", "X", 10, date(2025, 12, 10), 100, 90)
        p = Position("I", "X", "X", EQUITY, 90, [lot])
        w = upcoming_term_changes([p], date(2026, 11, 27))
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0].turns_lt, date(2026, 12, 11))


class OtherBucketTaxTests(unittest.TestCase):
    def test_gold_ltcg_has_no_exemption(self):
        self.assertAlmostEqual(compute_tax(0, 0, 0, 0, P, ltcg_other=10_000).tax, 1_250)

    def test_stcl_offsets_no_exemption_bucket_first(self):
        out = compute_tax(0, 50_000, 10_000, 0, P, ltcg_other=10_000)
        self.assertEqual(out.tax, 0)
        self.assertEqual(out.wasted_loss, 0)

    def test_ltcl_prefers_no_exemption_bucket(self):
        out = compute_tax(0, 200_000, 0, 10_000, P, ltcg_other=10_000)
        self.assertAlmostEqual(out.tax, 0.125 * 75_000)


if __name__ == "__main__":
    unittest.main()
