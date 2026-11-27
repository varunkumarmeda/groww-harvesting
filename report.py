"""Parse Groww's "Tax Loss Harvesting Report" XLSX into typed lots.

The report (Reports -> Tax Loss Harvesting on Groww) has three sheets:
  Summary       - ST/LT unrealised loss totals per category
  Stocks        - Stock Name | ISIN | QTY | Unrealised Loss (in Rs) | Term (ST/LT)
  Mutual Funds  - Fund Name | Units Held | Unrealised Loss (in Rs) | Term (ST/LT)

Only lots that are currently in loss appear. The same ISIN can show up twice:
once for its short-term lots and once for its long-term lots.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

import openpyxl

ST = "ST"
LT = "LT"

# Header aliases (lower-cased, whitespace-collapsed) -> canonical field.
_HEADER_ALIASES = {
    "stock name": "name",
    "fund name": "name",
    "scheme name": "name",
    "isin": "isin",
    "qty": "quantity",
    "quantity": "quantity",
    "units held": "quantity",
    "units": "quantity",
    "unrealised loss (in rs)": "loss",
    "unrealized loss (in rs)": "loss",
    "unrealised loss (in ₹)": "loss",
    "unrealized loss (in ₹)": "loss",
    "term (st/lt)": "term",
    "term": "term",
}


@dataclass(frozen=True)
class Lot:
    """One row of the report: a group of units of one holding in one tax term."""

    asset: str  # "stock" or "mf"
    name: str
    isin: Optional[str]
    quantity: float
    loss: float  # negative rupees
    term: str  # ST or LT

    @property
    def key(self) -> str:
        """Identity used to group ST + LT rows of the same holding."""
        return self.isin or self.name.lower()


@dataclass
class Report:
    path: Path
    stocks: List[Lot]
    mutual_funds: List[Lot]
    summary: dict  # {"Stocks": {"ST": -x, "LT": -y}, ...} as printed by Groww


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def normalize_term(raw) -> Optional[str]:
    """'ST(ST<1 year)' -> 'ST', 'LT(LT>1 year)' -> 'LT', anything else -> None."""
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if text.startswith("ST") or "SHORT" in text:
        return ST
    if text.startswith("LT") or "LONG" in text:
        return LT
    return None


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[₹,\s]|Rs\.?", "", str(value))
    if cleaned.startswith("(") and cleaned.endswith(")"):  # accounting negative
        cleaned = "-" + cleaned[1:-1]
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_lot_sheet(ws, asset: str) -> List[Lot]:
    rows = list(ws.iter_rows(values_only=True))
    header_idx, colmap = None, {}
    for i, row in enumerate(rows[:20]):  # header is row 1 today; tolerate a preamble
        mapping = {}
        for j, cell in enumerate(row):
            field = _HEADER_ALIASES.get(_norm(cell)) if cell is not None else None
            if field and field not in mapping:
                mapping[field] = j
        if {"name", "quantity", "loss"} <= mapping.keys():
            header_idx, colmap = i, mapping
            break
    if header_idx is None:
        raise ValueError(
            f"Sheet '{ws.title}': could not find a header row with name, quantity "
            f"and unrealised-loss columns."
        )

    lots: List[Lot] = []
    for row in rows[header_idx + 1:]:
        def cell(field):
            j = colmap.get(field)
            return row[j] if j is not None and j < len(row) else None

        name = cell("name")
        qty = _to_float(cell("quantity"))
        loss = _to_float(cell("loss"))
        if not name or qty is None or loss is None or qty <= 0:
            continue
        if loss > 0:
            loss = -loss  # some exports print the loss magnitude without a sign
        if loss == 0:
            continue
        term = normalize_term(cell("term"))
        if term is None:
            raise ValueError(f"Sheet '{ws.title}': unrecognised term {cell('term')!r} for {name}")
        isin = cell("isin")
        lots.append(
            Lot(
                asset=asset,
                name=str(name).strip(),
                isin=str(isin).strip().upper() if isin else None,
                quantity=qty,
                loss=round(loss, 2),
                term=term,
            )
        )
    return lots


def _parse_summary(ws) -> dict:
    summary = {}
    for row in ws.iter_rows(values_only=True):
        if not row or row[0] is None or len(row) < 3:
            continue
        st, lt = _to_float(row[1]), _to_float(row[2])
        if st is not None and lt is not None:
            summary[str(row[0]).strip()] = {ST: st, LT: lt}
    return summary


def _find_sheet(wb, *candidates):
    by_norm = {_norm(name): name for name in wb.sheetnames}
    for cand in candidates:
        if _norm(cand) in by_norm:
            return wb[by_norm[_norm(cand)]]
    return None


def load_report(path) -> Report:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Report not found: {path}")
    with warnings.catch_warnings():
        # Groww's export has no default style; openpyxl warns about it harmlessly.
        warnings.simplefilter("ignore", UserWarning)
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        stocks_ws = _find_sheet(wb, "Stocks", "Equity")
        mf_ws = _find_sheet(wb, "Mutual Funds", "MF")
        summary_ws = _find_sheet(wb, "Summary")
        return Report(
            path=path,
            stocks=_parse_lot_sheet(stocks_ws, "stock") if stocks_ws else [],
            mutual_funds=_parse_lot_sheet(mf_ws, "mf") if mf_ws else [],
            summary=_parse_summary(summary_ws) if summary_ws else {},
        )
    finally:
        wb.close()


def reconcile_with_summary(report: Report) -> List[str]:
    """Return warnings when parsed rows don't add up to Groww's own Summary sheet."""
    issues = []
    checks = [("Stocks", report.stocks)]
    mf_key = next((k for k in report.summary if k.lower().startswith("mutual")), None)
    if mf_key:
        checks.append((mf_key, report.mutual_funds))
    for label, lots in checks:
        expected = report.summary.get(label)
        if not expected:
            continue
        for term in (ST, LT):
            got = round(sum(l.loss for l in lots if l.term == term), 2)
            if abs(got - expected[term]) > 1.0:
                issues.append(
                    f"{label} {term}: rows sum to {got:,.2f} but Summary says {expected[term]:,.2f}"
                )
    return issues


def report_date(path) -> date:
    """Date the report was generated: from Groww's filename (..._DD-MM-YYYY_...), else file mtime."""
    path = Path(path)
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", path.name)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime).date()
