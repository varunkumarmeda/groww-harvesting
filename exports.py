"""Parse the standard Groww stock exports (Reports section of the Groww app/web).

Point it at a folder holding any of:
  Stocks_Holdings_Statement_<UCC>_<DD-MM-YYYY>.xlsx
  Stocks_PnL_Report_<UCC>_<from>_<to>.xlsx            ("Trade Level" sheet has open lots)
  Stocks_Capital_Gains_Report_<UCC>_<from>_<to>.xlsx  (realised ST/LT for the FY)
  Stocks_Order_History_<UCC>_<from>_<to>.xlsx         (used for trading symbols)

The P&L report's "Unrealised trades" section is the key input: one row per open
lot with buy date and buy price, already FIFO-matched by Groww.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import openpyxl

_DATE_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")


def parse_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    m = _DATE_RE.search(str(value))
    if not m:
        return None
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("₹", ""))
    except ValueError:
        return None


def _rows(path: Path) -> Dict[str, List[tuple]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        return {
            ws.title: [r for r in ws.iter_rows(values_only=True) if any(c not in (None, "") for c in r)]
            for ws in wb.worksheets
        }
    finally:
        wb.close()


def _label(row) -> str:
    return str(row[0]).strip().lower() if row and row[0] is not None else ""


def _section(rows: List[tuple], title: str, stop_titles: Tuple[str, ...]) -> List[tuple]:
    """Data rows after a section title (and its header row) until the next section."""
    out, inside, seen_header = [], False, False
    for r in rows:
        lab = _label(r)
        if lab == title.lower():
            inside, seen_header = True, False
            continue
        if not inside:
            continue
        if lab in stop_titles or lab.startswith("disclaimer"):
            break
        if lab == "stock name":
            seen_header = True
            continue
        if seen_header:
            out.append(r)
    return out


# ------------------------------------------------------------------- data types


@dataclass(frozen=True)
class OpenLot:
    isin: str
    name: str
    quantity: float
    buy_date: date
    buy_price: float
    close_price: float
    remark: str = ""

    @property
    def cost(self) -> float:
        return self.quantity * self.buy_price


@dataclass(frozen=True)
class HoldingRow:
    isin: str
    name: str
    quantity: float
    avg_price: float
    close_price: float
    buy_value: Optional[float] = None  # as printed by Groww (avoids avg-price rounding)
    close_value: Optional[float] = None

    @property
    def invested(self) -> float:
        return self.buy_value if self.buy_value is not None else self.quantity * self.avg_price

    @property
    def value(self) -> float:
        return self.close_value if self.close_value is not None else self.quantity * self.close_price


@dataclass(frozen=True)
class RealisedTrade:
    isin: str
    name: str
    quantity: float
    buy_date: Optional[date]
    sell_date: Optional[date]
    pnl: float
    term: str  # "ST" or "LT"


@dataclass
class Exports:
    folder: Path
    as_of: Optional[date] = None  # holdings / prices date
    holdings: List[HoldingRow] = field(default_factory=list)
    open_lots: List[OpenLot] = field(default_factory=list)
    fy_label: Optional[str] = None
    realised_st: Optional[float] = None
    realised_lt: Optional[float] = None
    realised_trades: List[RealisedTrade] = field(default_factory=list)
    charges_fy: Optional[float] = None
    dividends_fy: Optional[float] = None
    symbols: Dict[str, str] = field(default_factory=dict)  # isin -> trading symbol
    sources: Dict[str, str] = field(default_factory=dict)  # role -> filename
    warnings: List[str] = field(default_factory=list)
    live_status: Optional[str] = None  # result of the live holdings check, if run
    live_diffs: List[str] = field(default_factory=list)


# --------------------------------------------------------------------- parsers


def _dates_in_name(p: Path) -> List[date]:
    return [date(int(y), int(m), int(d)) for d, m, y in _DATE_RE.findall(p.name)]


def _latest(folder: Path, prefix: str) -> Optional[Path]:
    files = [p for p in folder.glob(f"{prefix}*.xlsx") if not p.name.startswith("~$")]
    if not files:
        return None
    return max(files, key=lambda p: (max(_dates_in_name(p), default=date.min), p.stat().st_mtime))


def parse_holdings(path: Path) -> Tuple[Optional[date], List[HoldingRow]]:
    rows = next(iter(_rows(path).values()))
    as_of = None
    for r in rows[:6]:
        if r[0] and "as on" in str(r[0]).lower():
            as_of = parse_date(r[0])
    out, header = [], False
    for r in rows:
        if _label(r) == "stock name":
            header = True
            continue
        if header and r[1] and _f(r[2]):
            out.append(HoldingRow(str(r[1]).strip().upper(), str(r[0]).strip(), _f(r[2]), _f(r[3]) or 0.0,
                                  _f(r[5]) or 0.0, _f(r[4]) if len(r) > 4 else None, _f(r[6]) if len(r) > 6 else None))
    return as_of, out


def parse_pnl_open_lots(path: Path) -> List[OpenLot]:
    sheets = _rows(path)
    rows = sheets.get("Trade Level") or next(iter(sheets.values()))
    lots = []
    for r in _section(rows, "Unrealised trades", ("realised trades",)):
        q, bd = _f(r[2]), parse_date(r[3])
        if not r[1] or not q or bd is None:
            continue
        lots.append(OpenLot(
            isin=str(r[1]).strip().upper(), name=str(r[0]).strip(), quantity=q, buy_date=bd,
            buy_price=_f(r[4]) or 0.0, close_price=_f(r[7]) or 0.0,
            remark=str(r[10]).strip() if len(r) > 10 and r[10] else "",
        ))
    return lots


def parse_capital_gains(path: Path) -> dict:
    rows = next(iter(_rows(path).values()))
    summary = {}
    for r in rows:
        lab = _label(r)
        if lab in ("short term p&l", "long term p&l", "total", "dividends", "intraday p&l") and _f(r[1]) is not None:
            summary.setdefault(lab, _f(r[1]))  # first "total" is the charges total
    trades = []
    stop = ("long term trades", "short term trades", "buyback trades", "intraday trades")
    for title, term in (("Short Term trades", "ST"), ("Long Term trades", "LT")):
        for r in _section(rows, title, stop):
            if r[1] and _f(r[9]) is not None:
                trades.append(RealisedTrade(str(r[1]).strip().upper(), str(r[0]).strip(), _f(r[2]) or 0.0,
                                            parse_date(r[3]), parse_date(r[6]), _f(r[9]), term))
    title = next((str(r[0]) for r in rows[:5] if r[0] and "capital gains statement" in str(r[0]).lower()), "")
    return {"summary": summary, "trades": trades, "title": title}


def parse_order_symbols(path: Path) -> Dict[str, str]:
    rows = next(iter(_rows(path).values()))
    out, header = {}, False
    for r in rows:
        if _label(r) == "stock name":
            header = True
            continue
        if header and len(r) > 2 and r[1] and r[2]:
            out[str(r[2]).strip().upper()] = str(r[1]).strip()
    return out


def _fy_of(d: date) -> Tuple[date, date]:
    start = date(d.year if d.month >= 4 else d.year - 1, 4, 1)
    return start, date(start.year + 1, 3, 31)


def load_exports(folder, today: Optional[date] = None) -> Exports:
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"Exports folder not found: {folder}")
    ex = Exports(folder=folder)

    hp = _latest(folder, "Stocks_Holdings_Statement")
    if hp:
        ex.as_of, ex.holdings = parse_holdings(hp)
        ex.sources["holdings"] = hp.name

    pp = _latest(folder, "Stocks_PnL_Report")
    if pp:
        ex.open_lots = parse_pnl_open_lots(pp)
        ex.sources["lots"] = pp.name
        pnl_end = max(_dates_in_name(pp), default=None)
        if ex.as_of and pnl_end and pnl_end != ex.as_of:
            ex.warnings.append(f"P&L report runs to {pnl_end:%d %b %Y} but holdings are as on {ex.as_of:%d %b %Y}; "
                               "export both on the same day for consistent lots.")

    ref = ex.as_of or today or date.today()
    fy_start, fy_end = _fy_of(ref)
    ex.fy_label = f"FY {fy_start.year}-{str(fy_end.year)[2:]}"
    cg = [p for p in folder.glob("Stocks_Capital_Gains_Report*.xlsx") if not p.name.startswith("~$")]
    cg_cur = [p for p in cg if _dates_in_name(p) and _dates_in_name(p)[0] == fy_start]
    if cg_cur:
        data = parse_capital_gains(cg_cur[0])
        s = data["summary"]
        ex.realised_st, ex.realised_lt = s.get("short term p&l"), s.get("long term p&l")
        ex.charges_fy, ex.dividends_fy = s.get("total"), s.get("dividends")
        ex.realised_trades = data["trades"]
        ex.sources["capital_gains"] = cg_cur[0].name
    elif cg:
        ex.warnings.append(f"No capital gains report for {ex.fy_label} found; pass --stcg/--ltcg instead.")

    op = _latest(folder, "Stocks_Order_History")
    if op:
        ex.symbols = parse_order_symbols(op)
        ex.sources["orders"] = op.name

    # Cross-check: lots per ISIN should add up to the holdings quantity.
    lot_qty: Dict[str, float] = {}
    for l in ex.open_lots:
        lot_qty[l.isin] = lot_qty.get(l.isin, 0) + l.quantity
    for h in ex.holdings:
        got = lot_qty.get(h.isin, 0)
        if abs(got - h.quantity) > 1e-6:
            ex.warnings.append(f"{h.name}: holdings show {h.quantity:g} but P&L lots add up to {got:g}")
    return ex


def compare_with_live(ex: Exports, live: list) -> List[str]:
    """Differences between exported holdings and live Groww holdings (raw API rows)."""
    live_qty = {(h.get("isin") or "").upper(): float(h.get("quantity") or 0) for h in live if h.get("isin")}
    live_sym = {(h.get("isin") or "").upper(): h.get("trading_symbol") for h in live}
    exp = {h.isin: h for h in ex.holdings}
    diffs = []
    for isin, h in exp.items():
        q = live_qty.get(isin, 0.0)
        if abs(q - h.quantity) > 1e-6:
            what = "no longer held" if q == 0 else f"now {q:g}"
            diffs.append(f"{h.name}: export shows {h.quantity:g}, {what}")
    for isin, q in live_qty.items():
        if isin not in exp and q > 0:
            diffs.append(f"{live_sym.get(isin) or isin}: {q:g} held now but not in the export (new buy or IPO allotment)")
    return diffs
