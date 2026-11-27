"""Lot-level harvesting planner built on Groww exports (exports.py).

Works out, for a sale of N units of a holding, exactly which lots FIFO consumes
and how much short-term / long-term P&L that books. Then plans two things:

1. Loss harvesting: book short-term losses that cancel this FY's taxable gains,
   selling the fewest units needed.
2. Gain harvesting: book long-term equity gains up to the unused Rs 1.25L 112A
   exemption tax-free, and buy back to reset the cost basis higher.

Simplifications (check with a CA): every gain/loss is taxed like listed equity
(111A/112A) except gold/silver ETFs are kept out of gain harvesting (no 112A
exemption) and debt (bonds/NCDs) is left out entirely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from exports import Exports, OpenLot
from planner import TaxParams, compute_tax

EQUITY = "Equity"
GOLD_SILVER = "Gold/Silver ETF"
DEBT = "Debt"


def asset_class(name: str, isin: str) -> str:
    n = name.upper()
    if "%" in n or re.search(r"\b(NCD|BOND|DEBENTURE)S?\b", n):
        return DEBT
    if isin.startswith("INF") and any(k in n for k in ("GOLD", "SLVR", "SILVER")):
        return GOLD_SILVER
    return EQUITY


def one_year_after(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year + 1, day=28)


def is_long_term(buy: date, sell: date) -> bool:
    """Listed shares / ETF units: long-term if held MORE than 12 months."""
    return sell > one_year_after(buy)


@dataclass
class Position:
    isin: str
    name: str
    symbol: Optional[str]
    asset: str
    price: float  # closing price used for the plan
    lots: List[OpenLot]  # FIFO order (oldest first)

    @property
    def quantity(self) -> float:
        return sum(l.quantity for l in self.lots)

    @property
    def label(self) -> str:
        return self.symbol or self.name

    def sale(self, qty: float, sell_date: date) -> Tuple[float, float]:
        """(short-term P&L, long-term P&L) booked by selling `qty` units FIFO."""
        st = lt = 0.0
        left = qty
        for lot in self.lots:
            if left <= 0:
                break
            take = min(left, lot.quantity)
            pnl = take * (self.price - lot.buy_price)
            if is_long_term(lot.buy_date, sell_date):
                lt += pnl
            else:
                st += pnl
            left -= take
        return st, lt

    def lt_prefix_units(self, sell_date: date) -> float:
        """Units that can be sold while touching only long-term lots."""
        n = 0.0
        for lot in self.lots:
            if not is_long_term(lot.buy_date, sell_date):
                break
            n += lot.quantity
        return n

    def unrealised(self, sell_date: date) -> Tuple[float, float]:
        return self.sale(self.quantity, sell_date)


def build_positions(ex: Exports) -> List[Position]:
    price = {h.isin: h.close_price for h in ex.holdings}
    by_isin: Dict[str, List[OpenLot]] = {}
    for l in ex.open_lots:
        by_isin.setdefault(l.isin, []).append(l)
    out = []
    for isin, lots in by_isin.items():
        lots = sorted(lots, key=lambda l: l.buy_date)  # stable: keeps Groww's order on ties
        name = lots[0].name
        sym = ex.symbols.get(isin)
        if sym and re.fullmatch(r"[\d.]+", sym):  # BSE scrip codes aren't useful labels
            sym = None
        out.append(Position(isin, name, sym, asset_class(name, isin),
                            price.get(isin) or lots[0].close_price, lots))
    return sorted(out, key=lambda p: p.name)


# ------------------------------------------------------------------ the plan


@dataclass
class Trade:
    pos: Position
    qty: float
    st_pnl: float
    lt_pnl: float
    kind: str  # "loss" or "gain"
    reason: str = ""

    @property
    def value(self) -> float:
        return self.qty * self.pos.price


@dataclass
class LotPlan:
    sell_date: date
    stcg_realised: float
    ltcg_realised: float  # 112A (listed equity)
    params: TaxParams
    cost_pct: float
    fixed_cost: float
    ltcg_other_realised: float = 0.0  # 112, no exemption (gold/silver ETFs)
    loss_trades: List[Trade] = field(default_factory=list)
    gain_trades: List[Trade] = field(default_factory=list)
    excluded: List[Tuple[Position, str]] = field(default_factory=list)

    def totals(self, include_gain: bool = True) -> Tuple[float, float, float]:
        """(ST P&L, 112A LT P&L, no-exemption LT P&L) booked by the plan."""
        acc = (0.0, 0.0, 0.0)
        for t in self.loss_trades + (self.gain_trades if include_gain else []):
            acc = _add(acc, buckets(t.pos, t.st_pnl, t.lt_pnl))
        return acc

    def tax(self, extra_st: float = 0.0, extra_lt: float = 0.0, extra_lt_other: float = 0.0):
        return compute_tax(self.stcg_realised + extra_st, self.ltcg_realised + extra_lt, 0, 0, self.params,
                           ltcg_other=self.ltcg_other_realised + extra_lt_other)

    def cost(self, value: float) -> float:
        """Approximate round-trip charges for selling and re-buying `value` rupees."""
        return value * self.cost_pct + self.fixed_cost


def buckets(pos: Position, st: float, lt: float) -> Tuple[float, float, float]:
    """Route a sale's LT P&L to 112A (equity) or the no-exemption bucket (gold/silver)."""
    return (st, lt, 0.0) if pos.asset == EQUITY else (st, 0.0, lt)


def _add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def _best_qty(pos: Position, sell_date: date, score, max_qty: Optional[float] = None):
    """Scan whole-unit sale sizes; return (score, qty, st, lt) with the best score, smallest qty on ties."""
    best = None
    total = int(max_qty if max_qty is not None else pos.quantity)
    for q in range(1, total + 1):
        st, lt = pos.sale(q, sell_date)
        s = score(q, st, lt)
        if s is None:
            continue
        if best is None or s > best[0] + 1e-9:
            best = (s, q, st, lt)
    return best


def plan_from_positions(
    positions: List[Position],
    sell_date: date,
    stcg_realised: float,
    ltcg_realised: float,
    params: TaxParams,
    cost_pct: float = 0.0025,
    fixed_cost: float = 40.0,
    min_benefit: float = 100.0,
    harvest_gains: bool = True,
    ltcg_other_realised: float = 0.0,
) -> LotPlan:
    plan = LotPlan(sell_date, stcg_realised, ltcg_realised, params, cost_pct, fixed_cost, ltcg_other_realised)
    pool = []
    for p in positions:
        if p.asset == DEBT:
            plan.excluded.append((p, "Bond/NCD: taxed differently (often at slab rate) and illiquid; decide with your CA"))
        elif p.quantity != int(p.quantity):
            plan.excluded.append((p, "Fractional units"))
        else:
            pool.append(p)

    # 1. Loss harvesting: greedily add the sale with the best net benefit.
    acc = (0.0, 0.0, 0.0)
    current = plan.tax().tax
    chosen = set()
    while True:
        best = None
        for p in pool:
            if p.isin in chosen:
                continue

            def score(q, st, lt, p=p):
                if st >= 0:
                    return None
                saving = current - plan.tax(*_add(acc, buckets(p, st, lt))).tax
                net = saving - plan.cost(q * p.price)
                return net if saving > 0.5 else None

            r = _best_qty(p, sell_date, score)
            if r and r[0] > 0 and (best is None or r[0] > best[0][0] + 1e-9):
                best = (r, p)
        if best is None:
            break
        (net, q, st, lt), p = best
        chosen.add(p.isin)
        acc = _add(acc, buckets(p, st, lt))
        current = plan.tax(*acc).tax
        plan.loss_trades.append(Trade(p, q, st, lt, "loss", f"cuts this year's tax by ~₹{net + plan.cost(q * p.price):,.0f}"))

    # 2. Gain harvesting: fill the unused 112A exemption with long-term equity gains.
    if harvest_gains:
        tax_now = plan.tax(*acc).tax
        while True:
            best = None
            for p in pool:
                if p.isin in chosen or p.asset != EQUITY:
                    continue
                cap = p.lt_prefix_units(sell_date)
                if cap < 1:
                    continue

                def score(q, st, lt, p=p):
                    if lt <= 0 or st != 0:
                        return None
                    if plan.tax(*_add(acc, buckets(p, st, lt))).tax > tax_now + 0.5:
                        return None  # would create tax
                    # Value = future 112A tax avoided on this gain, net of charges.
                    return params.ltcg_rate * lt - plan.cost(q * p.price)

                r = _best_qty(p, sell_date, score, max_qty=cap)
                if r and r[0] >= min_benefit and (best is None or r[0] > best[0][0] + 1e-9):
                    best = (r, p)
            if best is None:
                break
            (_, q, st, lt), p = best
            chosen.add(p.isin)
            acc = _add(acc, buckets(p, st, lt))
            plan.gain_trades.append(Trade(p, q, st, lt, "gain",
                                          f"tax-free now; avoids up to ~₹{params.ltcg_rate * lt:,.0f} of future LTCG tax"))
    return plan


# ------------------------------------------------------------------- insights


@dataclass
class TermWatch:
    pos: Position
    lot: OpenLot
    turns_lt: date
    pnl: float
    advice: str


def upcoming_term_changes(positions: List[Position], today: date, days: int = 60) -> List[TermWatch]:
    """Lots that become long-term soon: sell losers before, hold winners until after."""
    out = []
    for p in positions:
        if p.asset == DEBT:
            continue
        for lot in p.lots:
            lt_day = one_year_after(lot.buy_date) + timedelta(days=1)
            if today < lt_day <= today + timedelta(days=days):
                pnl = lot.quantity * (p.price - lot.buy_price)
                if pnl < 0:
                    advice = f"Short-term loss until {lt_day - timedelta(days=1):%d %b}; after that it can only offset LTCG"
                else:
                    advice = f"Wait until {lt_day:%d %b} to sell: the gain becomes long-term (12.5%, ₹1.25L exemption) instead of 20%"
                out.append(TermWatch(p, lot, lt_day, pnl, advice))
    return sorted(out, key=lambda w: w.turns_lt)
