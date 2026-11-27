"""Run with:  python -m unittest discover tests"""

import sys
import tempfile
import unittest
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from planner import (  # noqa: E402
    OK, REVIEW, SKIP, Holding, TaxParams, apply_holdings, apply_term_filter,
    compute_tax, group_lots, select_for_tax,
)
from report import LT, ST, load_report, normalize_term, reconcile_with_summary  # noqa: E402

P = TaxParams()


def make_report(path):
    wb = openpyxl.Workbook()
    s = wb.active
    s.title = "Summary"
    s.append(["Name", "Test"])
    s.append([])
    s.append(["Category", "Short-Term Loss (in ₹)", "Long-Term Loss (in ₹)", "Total"])
    s.append(["Stocks", -1300, -500, -1800])
    s.append(["Mutual Funds (Equity)", -100, 0, -100])
    st = wb.create_sheet("Stocks")
    st.append(["Stock Name", "ISIN", "QTY", "Unrealised Loss (in Rs)", "Term (ST/LT)"])
    st.append(["Alpha Ltd", "INE000A00001", 3, -1000, "ST(ST<1 year)"])
    st.append(["Beta Ltd", "INE000B00001", 13, -300, "ST(ST<1 year)"])
    st.append(["Beta Ltd", "INE000B00001", 12, -500, "LT(LT>1 year)"])
    mf = wb.create_sheet("Mutual Funds")
    mf.append(["Fund Name", "Units Held", "Unrealised Loss (in Rs)", "Term (ST/LT)"])
    mf.append(["Some Fund Direct Growth", 10.5, -100, "ST(ST<1 year)"])
    wb.save(path)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "r.xlsx"
        make_report(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse(self):
        r = load_report(self.path)
        self.assertEqual(len(r.stocks), 3)
        self.assertEqual(len(r.mutual_funds), 1)
        self.assertEqual(r.stocks[2].term, LT)
        self.assertEqual(r.mutual_funds[0].isin, None)
        self.assertEqual(reconcile_with_summary(r), [])

    def test_terms(self):
        self.assertEqual(normalize_term("ST(ST<1 year)"), ST)
        self.assertEqual(normalize_term("LT(LT>1 year)"), LT)
        self.assertIsNone(normalize_term("??"))

    def test_fifo_grouping_sells_lt_with_st(self):
        cands = apply_term_filter(group_lots(load_report(self.path).stocks), "st")
        beta = next(c for c in cands if c.name == "Beta Ltd")
        self.assertEqual(beta.sell_qty, 25)  # 13 ST + 12 older LT units
        self.assertTrue(any("FIFO" in n for n in beta.notes))


class HoldingsTests(unittest.TestCase):
    def cands(self):
        from report import Lot
        return group_lots([Lot("stock", "Alpha", "I1", 3, -1000, ST), Lot("stock", "Beta", "I2", 5, -200, ST)])

    def test_exact_match_ok(self):
        cs = self.cands()
        apply_holdings(cs, {"I1": Holding("ALPHA", 3, 3), "I2": Holding("BETA", 5, 5)})
        self.assertEqual([c.status for c in cs], [OK, OK])

    def test_extra_units_need_review(self):
        cs = self.cands()
        apply_holdings(cs, {"I1": Holding("ALPHA", 10, 10), "I2": Holding("BETA", 5, 5)})
        self.assertEqual(cs[0].status, REVIEW)

    def test_missing_is_skipped(self):
        cs = self.cands()
        apply_holdings(cs, {"I1": Holding("ALPHA", 3, 3)})
        self.assertEqual(cs[1].status, SKIP)

    def test_pledged_needs_review(self):
        cs = self.cands()
        apply_holdings(cs, {"I1": Holding("ALPHA", 3, 1), "I2": Holding("BETA", 5, 5)})
        self.assertEqual(cs[0].status, REVIEW)


class TaxTests(unittest.TestCase):
    def test_no_losses(self):
        out = compute_tax(10_000, 200_000, 0, 0, P)
        self.assertAlmostEqual(out.tax, 0.2 * 10_000 + 0.125 * 75_000)

    def test_lt_loss_cannot_offset_stcg(self):
        out = compute_tax(10_000, 0, 0, 5_000, P)
        self.assertAlmostEqual(out.tax, 2_000)
        self.assertAlmostEqual(out.carry_forward_lt, 5_000)

    def test_st_loss_spills_to_ltcg(self):
        out = compute_tax(10_000, 200_000, 30_000, 0, P)
        self.assertAlmostEqual(out.tax, 0.125 * 55_000)

    def test_wasted_when_ltcg_under_exemption(self):
        out = compute_tax(0, 100_000, 0, 20_000, P)
        self.assertEqual(out.tax, 0)
        self.assertAlmostEqual(out.wasted_loss, 20_000)

    def test_partially_wasted(self):
        out = compute_tax(0, 135_000, 0, 20_000, P)  # 10k useful, 10k wasted
        self.assertAlmostEqual(out.wasted_loss, 10_000)

    def test_selection_skips_useless_lt_loss(self):
        from report import Lot
        cs = group_lots([Lot("stock", "S", "I1", 1, -5_000, ST), Lot("stock", "L", "I2", 1, -8_000, LT)])
        select_for_tax(cs, stcg=5_000, ltcg=50_000, p=P)  # LTCG is exempt already
        picked = {c.name for c in cs if c.selected}
        self.assertEqual(picked, {"S"})


if __name__ == "__main__":
    unittest.main()
