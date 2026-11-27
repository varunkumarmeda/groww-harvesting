"""Turn report lots into a tax-loss harvesting plan.

Key rules modelled here (Indian listed equity / equity MFs, sections 111A / 112A).
These are simplifications. Check them with a CA before acting:

* FIFO. For demat shares and MF units, a sale is matched against the OLDEST units
  first. You cannot "sell only the short-term lot": if a stock has both LT and ST
  lots, any sale eats the (older) LT lots first. So the plan always sells every
  loss lot of a holding together, and flags holdings with units that are not in
  loss, because a FIFO sale could hit profitable lots instead.
* Set-off. STCL can offset STCG or LTCG; LTCL can offset only LTCG. Current-year
  losses must be set off against current-year gains before being carried forward.
* Exemption. The first Rs 1.25 lakh of 112A LTCG is tax-free. A loss absorbed by
  LTCG that was already under the exemption gives no benefit: it is wasted. That is
  why harvesting "everything" can be worse than harvesting selectively.
* Unused losses carry forward up to 8 years, but only if the ITR is filed on time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from report import LT, ST, Lot

DEFAULT_STCG_RATE = 0.20  # 111A, from 23-Jul-2024
DEFAULT_LTCG_RATE = 0.125  # 112A, from 23-Jul-2024
DEFAULT_LTCG_EXEMPTION = 125_000.0


# --------------------------------------------------------------------------- tax


@dataclass(frozen=True)
class TaxParams:
    stcg_rate: float = DEFAULT_STCG_RATE
    ltcg_rate: float = DEFAULT_LTCG_RATE
    ltcg_exemption: float = DEFAULT_LTCG_EXEMPTION


@dataclass(frozen=True)
class TaxOutcome:
    tax: float
    taxable_stcg: float
    taxable_ltcg: float  # after exemption
    carry_forward_st: float  # unused STCL (positive rupees)
    carry_forward_lt: float  # unused LTCL (positive rupees)
    wasted_loss: float  # loss used up against LTCG that was exempt anyway


def compute_tax(
    stcg: float, ltcg: float, st_loss: float, lt_loss: float, p: TaxParams, ltcg_other: float = 0.0
) -> TaxOutcome:
    """Capital-gains tax after setting off harvested losses.

    stcg: net short-term gains (111A; may be negative).
    ltcg: net long-term gains under 112A (listed equity, gets the exemption; may be negative).
    ltcg_other: net long-term gains under 112 with NO exemption (e.g. gold/silver ETFs), same rate.
    st_loss/lt_loss: harvested losses as POSITIVE amounts.
    Losses are set off in the order that minimises tax: LTCL against no-exemption LTCG first;
    STCL against STCG, then no-exemption LTCG, then 112A LTCG.
    Excludes surcharge, 4% cess and the basic-exemption-limit adjustment.
    """
    # Existing net losses in a bucket behave like extra harvested loss.
    if stcg < 0:
        st_loss, stcg = st_loss - stcg, 0.0
    if ltcg < 0:
        lt_loss, ltcg = lt_loss - ltcg, 0.0
    if ltcg_other < 0:
        lt_loss, ltcg_other = lt_loss - ltcg_other, 0.0

    # LTCL only against LTCG: the no-exemption bucket first.
    used = min(lt_loss, ltcg_other)
    other_left = ltcg_other - used
    lt_rem = lt_loss - used
    lt_used = min(lt_rem, ltcg)
    ltcg_left = ltcg - lt_used
    cf_lt = lt_rem - lt_used

    # STCL against STCG first (taxed at the higher rate), then LTCG.
    st_vs_st = min(st_loss, stcg)
    stcg_left = stcg - st_vs_st
    st_rem = st_loss - st_vs_st
    used = min(st_rem, other_left)
    other_left -= used
    st_rem -= used
    st_vs_lt = min(st_rem, ltcg_left)
    ltcg_left -= st_vs_lt
    cf_st = st_rem - st_vs_lt

    taxable_ltcg = max(ltcg_left - p.ltcg_exemption, 0.0) + other_left
    # Loss that only pushed LTCG further below the exemption bought nothing.
    ltcg_reduction = ltcg - ltcg_left
    taxable_reduction = max(ltcg - p.ltcg_exemption, 0.0) - max(ltcg_left - p.ltcg_exemption, 0.0)
    wasted = ltcg_reduction - taxable_reduction

    tax = p.stcg_rate * stcg_left + p.ltcg_rate * taxable_ltcg
    return TaxOutcome(
        tax=round(tax, 2),
        taxable_stcg=round(stcg_left, 2),
        taxable_ltcg=round(taxable_ltcg, 2),
        carry_forward_st=round(cf_st, 2),
        carry_forward_lt=round(cf_lt, 2),
        wasted_loss=round(wasted, 2),
    )


# --------------------------------------------------------------------- candidates

OK = "OK"
REVIEW = "REVIEW"
SKIP = "SKIP"


@dataclass
class Holding:
    trading_symbol: str
    quantity: float
    sellable_quantity: float


@dataclass
class Candidate:
    asset: str
    name: str
    isin: Optional[str]
    st_qty: float = 0.0
    st_loss: float = 0.0  # negative
    lt_qty: float = 0.0
    lt_loss: float = 0.0  # negative
    trading_symbol: Optional[str] = None
    held_qty: Optional[float] = None
    status: str = OK
    notes: List[str] = field(default_factory=list)
    selected: bool = True

    @property
    def sell_qty(self) -> float:
        return round(self.st_qty + self.lt_qty, 6)

    @property
    def total_loss(self) -> float:
        return round(self.st_loss + self.lt_loss, 2)

    @property
    def terms(self) -> str:
        return "+".join(t for t, q in ((LT, self.lt_qty), (ST, self.st_qty)) if q)

    def flag(self, status: str, note: str) -> None:
        # REVIEW/SKIP outrank OK; SKIP outranks REVIEW.
        rank = {OK: 0, REVIEW: 1, SKIP: 2}
        if rank[status] > rank[self.status]:
            self.status = status
        self.notes.append(note)


def group_lots(lots: Iterable[Lot]) -> List[Candidate]:
    by_key: Dict[str, Candidate] = {}
    for lot in lots:
        c = by_key.get(lot.key)
        if c is None:
            c = by_key[lot.key] = Candidate(asset=lot.asset, name=lot.name, isin=lot.isin)
        if lot.term == ST:
            c.st_qty += lot.quantity
            c.st_loss += lot.loss
        else:
            c.lt_qty += lot.quantity
            c.lt_loss += lot.loss
    for c in by_key.values():
        c.st_loss, c.lt_loss = round(c.st_loss, 2), round(c.lt_loss, 2)
    return sorted(by_key.values(), key=lambda c: c.total_loss)


def apply_term_filter(cands: List[Candidate], term: str) -> List[Candidate]:
    """Keep holdings that have a loss lot in the requested term.

    Because of FIFO, a holding chosen for its ST loss still sells its LT lots too.
    """
    if term == "all":
        return cands
    want_st = term == "st"
    kept = []
    for c in cands:
        if (want_st and c.st_qty) or (not want_st and c.lt_qty):
            if want_st and c.lt_qty:
                c.notes.append(
                    f"FIFO: the {c.lt_qty:g} older LT units are sold before the ST units, "
                    f"so the plan sells all {c.sell_qty:g} (also books LT loss {c.lt_loss:,.2f})"
                )
            kept.append(c)
    return kept


def apply_holdings(cands: List[Candidate], holdings: Optional[Dict[str, Holding]]) -> None:
    """Attach live holdings and flag anything that is unsafe to auto-sell."""
    for c in cands:
        if c.asset == "mf":
            c.flag(REVIEW, "Mutual fund: redeem manually in the Groww app (Trade API is equity-only)")
            continue
        if c.sell_qty != int(c.sell_qty):
            c.flag(REVIEW, f"Fractional quantity {c.sell_qty} from report")
        if holdings is None:
            c.flag(REVIEW, "Unverified: run with --holdings to check live quantity and FIFO safety")
            continue
        h = holdings.get(c.isin or "")
        if h is None:
            c.flag(SKIP, "Not in current holdings (report is T-1; already sold?)")
            continue
        c.trading_symbol = h.trading_symbol
        c.held_qty = h.quantity
        if h.quantity > c.sell_qty + 1e-9:
            c.flag(
                REVIEW,
                f"You hold {h.quantity:g} but only {c.sell_qty:g} are in loss. FIFO could match "
                f"the sale against profitable older lots. Check your lot dates before selling.",
            )
        elif h.quantity < c.sell_qty - 1e-9:
            c.flag(REVIEW, f"Holding ({h.quantity:g}) is smaller than the report ({c.sell_qty:g}); re-download the report")
        if h.sellable_quantity < c.sell_qty - 1e-9:
            c.flag(REVIEW, f"Only {h.sellable_quantity:g} sellable (pledged/locked units)")


def select_for_tax(
    cands: List[Candidate], stcg: float, ltcg: float, p: TaxParams
) -> TaxOutcome:
    """Greedily pick holdings that reduce this year's tax; deselect the rest.

    Each step adds the candidate with the largest marginal tax saving (tie-break:
    smallest loss, so you reset fewer cost bases). Stops when nothing saves more.
    Returns the tax outcome for the selected set.
    """
    pool = [c for c in cands if c.status != SKIP]
    for c in cands:
        c.selected = False
    st = lt = 0.0
    current = compute_tax(stcg, ltcg, 0, 0, p)
    while True:
        best, best_out, best_key = None, None, None
        for c in pool:
            if c.selected:
                continue
            out = compute_tax(stcg, ltcg, st - c.st_loss, lt - c.lt_loss, p)
            saving = current.tax - out.tax
            if saving <= 0.005:
                continue
            key = (saving, c.total_loss)  # total_loss is negative: larger = smaller loss
            if best_key is None or key > best_key:
                best, best_out, best_key = c, out, key
        if best is None:
            break
        best.selected = True
        st -= best.st_loss
        lt -= best.lt_loss
        current = best_out
    for c in pool:
        if not c.selected:
            c.notes.append("Not selected: no further tax saving this FY")
    return current


def harvested_losses(cands: Iterable[Candidate]) -> tuple:
    sel = [c for c in cands if c.selected and c.status != SKIP]
    return -sum(c.st_loss for c in sel), -sum(c.lt_loss for c in sel)
