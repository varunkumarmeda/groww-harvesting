"""HTML report built from Groww exports (lot-level). Called by portfolio_report.py --exports."""

from __future__ import annotations

from datetime import date, datetime
from typing import List

from exports import Exports
from lot_planner import DEBT, EQUITY, LotPlan, Position, Trade, upcoming_term_changes
from portfolio_report import CSS, esc, next_weekday, qty, rs, table

EXTRA_CSS = """
details{margin-top:4px}summary{cursor:pointer;color:var(--accent);font-size:12px}
details table{font-size:12px;margin-top:6px}
.bar{height:8px;border-radius:4px;background:var(--accent);min-width:2px}
.alloc td:nth-child(4){width:40%}
"""


def pnl_span(x: float, dp: int = 0) -> str:
    if abs(x) < 0.005:
        return "—"
    return f'<span class="{"pos" if x > 0 else "neg"}">{"+" if x > 0 else ""}{rs(x, dp)}</span>'


def lot_details(pos: Position, q: float, sell_date: date) -> str:
    from lot_planner import is_long_term

    rows, left = [], q
    for lot in pos.lots:
        if left <= 0:
            break
        take = min(left, lot.quantity)
        lt = is_long_term(lot.buy_date, sell_date)
        rows.append([lot.buy_date.strftime("%d %b %Y"), qty(take), rs(lot.buy_price, 2),
                     "LT" if lt else "ST", pnl_span(take * (pos.price - lot.buy_price))])
        left -= take
    first, last = pos.lots[0].buy_date, rows[-1][0]
    inner = table(["Bought", "Units", "Cost", "Term", "P&amp;L"], rows, num_cols=(1, 2, 4))
    return (f"<details><summary>{len(rows)} lot(s), FIFO from {first:%d %b %Y} to {esc(last)}</summary>"
            f"{inner}</details>")


def trade_rows(trades: List[Trade], plan: LotPlan) -> List[List[str]]:
    rows = []
    for t in trades:
        p = t.pos
        part = "all" if t.qty >= p.quantity else f"of {qty(p.quantity)}"
        rows.append([
            f"<b>{esc(p.label)}</b><br><small>{esc(p.name)} · {esc(p.asset)}</small>",
            f"{qty(t.qty)} <small>{part}</small>",
            rs(p.price, 2), rs(t.value),
            pnl_span(t.st_pnl), pnl_span(t.lt_pnl),
            rs(plan.cost(t.value)),
            esc(t.reason) + lot_details(p, t.qty, plan.sell_date),
        ])
    return rows


def build_exports_html(ex: Exports, positions: List[Position], plan: LotPlan, args, now: datetime) -> str:
    p = plan.params
    sell_day = plan.sell_date
    rebuy_day = next_weekday(sell_day)
    st_all, lt_all, lt_oth = plan.totals()
    st_loss_only, lt_loss_only, lt_oth_loss = plan.totals(include_gain=False)
    before = plan.tax()
    after = plan.tax(st_all, lt_all, lt_oth)
    saved = before.tax - after.tax

    tot_val = sum(h.value for h in ex.holdings)
    tot_inv = sum(h.invested for h in ex.holdings)
    out: List[str] = []
    a = out.append
    a(f"<title>Portfolio Report · {esc(now.strftime('%d %b %Y'))}</title><style>{CSS}{EXTRA_CSS}</style>")
    a('<div class="wrap">')
    a("<h1>Portfolio &amp; Tax Harvesting Report</h1>")
    srcs = " · ".join(f"{esc(k)}: {esc(v)}" for k, v in ex.sources.items())
    a(f'<p class="muted">Generated {esc(now.strftime("%d %b %Y, %H:%M"))} IST · prices as of '
      f'{esc(ex.as_of.strftime("%d %b %Y") if ex.as_of else "?")} close · {esc(ex.fy_label)}<br><small>{srcs}</small></p>')

    age = (now.date() - ex.as_of).days if ex.as_of else None
    if age is not None and age > 3:
        a(f'<div class="callout bad"><b>These exports are {age} days old.</b> Export fresh Holdings, P&amp;L and '
          "Capital Gains reports from Groww before trading.</div>")
    if ex.live_diffs:
        items = "".join(f"<li>{esc(d)}</li>" for d in ex.live_diffs)
        a(f'<div class="callout bad"><b>Your holdings changed since these exports</b> ({esc(ex.live_status)}):'
          f"<ul>{items}</ul>Re-export Holdings and P&amp;L from Groww and re-run before acting on this plan.</div>")
    elif ex.live_status and ex.live_status.startswith("checked"):
        a(f'<div class="callout info">✓ Live holdings match the export ({esc(ex.live_status)}).</div>')
    elif ex.live_status:
        a(f'<div class="callout">Live holdings check {esc(ex.live_status)}.</div>')
    for w in ex.warnings:
        a(f'<div class="callout">{esc(w)}</div>')
    if getattr(args, "stcg", None) is not None:
        a('<div class="callout info">Realised gains were overridden on the command line (--stcg/--ltcg).</div>')

    # ---- KPIs
    a('<div class="kpis">')
    a(f'<div class="kpi"><div class="l">Portfolio value</div><div class="v">{rs(tot_val)}</div>'
      f'<div class="s">{len(ex.holdings)} holdings · invested {rs(tot_inv)}</div></div>')
    a(f'<div class="kpi"><div class="l">Unrealised P&amp;L</div><div class="v {"pos" if tot_val >= tot_inv else "neg"}">'
      f'{rs(tot_val - tot_inv)}</div><div class="s">{(tot_val / tot_inv - 1) * 100 if tot_inv else 0:+.1f}% on cost</div></div>')
    a(f'<div class="kpi"><div class="l">Realised this FY</div><div class="v">{rs(plan.stcg_realised)}</div>'
      f'<div class="s">STCG · LTCG {rs(plan.ltcg_realised)}</div></div>')
    a(f'<div class="kpi"><div class="l">Capital-gains tax this FY</div><div class="v">{rs(before.tax)} → '
      f'<span class="pos">{rs(after.tax)}</span></div><div class="s">saves {rs(saved)} with this plan</div></div>')
    n_l, n_g = len(plan.loss_trades), len(plan.gain_trades)
    a(f'<div class="kpi"><div class="l">Trades</div><div class="v">{n_l + n_g} sell</div>'
      f'<div class="s">{n_l} loss · {n_g} gain · rebuy {esc(rebuy_day.strftime("%a %d %b"))}</div></div>')
    a("</div>")

    # ---- action plan
    a("<h2>Action plan</h2>")
    a('<div class="steps">')
    a(f'<div class="step"><div class="d">Day 1 · {esc(sell_day.strftime("%A %d %b %Y"))}</div><h3>Sell</h3><ol>'
      "<li>Re-export Holdings and P&amp;L from Groww the same morning and re-run this report if prices have moved a lot.</li>"
      f"<li>Sell the {n_l} <b>loss-harvesting</b> position(s): these cancel this year's short-term gains.</li>"
      + (f"<li>Optionally sell the {n_g} <b>gain-harvesting</b> position(s): long-term gains booked tax-free.</li>" if n_g else "")
      + "<li>Use delivery (CNC) sell orders. FIFO decides which lots go, as shown in each row's lot list.</li></ol></div>")
    a(f'<div class="step"><div class="d">Day 2 · {esc(rebuy_day.strftime("%A %d %b %Y"))}</div><h3>Buy back</h3><ol>'
      "<li>Buy the same quantities back on the next trading day, not the same day (a same-day sell and buy nets off as intraday).</li>"
      "<li>The new units have a fresh cost and holding period, so they're short-term again for 12 months.</li>"
      "<li>Keep the contract notes. Losses carry forward only if your ITR is filed by the due date.</li></ol>"
      "<small>NSE holidays aren't checked. If Day 2 is a holiday, use the next trading day.</small></div>")
    a("</div>")

    heads = ["Holding", "Sell qty", "Price", "Value", "ST P&amp;L", "LT P&amp;L", "Est. charges", "Why / lots"]
    a("<h3>1 · Loss harvesting: cancel this year's short-term gains</h3>")
    if plan.loss_trades:
        a(table(heads, trade_rows(plan.loss_trades, plan), num_cols=(1, 2, 3, 4, 5, 6)))
        a(f'<p class="muted">Books {rs(st_loss_only)} short-term P&amp;L against {rs(plan.stcg_realised)} of realised '
          f"STCG. FIFO also pulls in older long-term lots: {rs(lt_loss_only)} of equity LT P&amp;L (inside the ₹1.25L "
          f"exemption) and {rs(lt_oth_loss)} of gold/silver LT P&amp;L (no exemption; the tax figures account for it).</p>")
    else:
        a('<p class="muted">Nothing to do: no realised short-term gain to offset, or no short-term loss that pays for its charges.</p>')

    a("<h3>2 · Gain harvesting: use the ₹1.25L long-term exemption (optional)</h3>")
    if plan.gain_trades:
        used = plan.ltcg_realised + lt_all
        a(table(heads, trade_rows(plan.gain_trades, plan), num_cols=(1, 2, 3, 4, 5, 6)))
        a(f'<p class="muted">Net LTCG after the whole plan: {rs(used)} of the {rs(p.ltcg_exemption)} exemption, so no tax. '
          "Buying back raises your cost basis, so these gains are never taxed later. That only matters in a future "
          "year when your LTCG goes above ₹1.25L. Equity only: gold/silver ETFs and bonds don't get this exemption.</p>")
    else:
        a('<p class="muted">No long-term equity gain is worth booking after charges.</p>')

    rebuy = [[f"<b>{esc(t.pos.label)}</b><br><small>{esc(t.pos.name)}</small>",
              "Loss" if t.kind == "loss" else "Gain", f"BUY {qty(t.qty)} CNC", rs(t.value)]
             for t in plan.loss_trades + plan.gain_trades]
    if rebuy:
        a(f"<h3>Day 2 ({esc(rebuy_day.strftime('%a %d %b'))}): buy back</h3>")
        a(table(["Holding", "Harvest", "Order", "Approx. cost"], rebuy, num_cols=(3,)))

    # ---- tax picture
    a("<h2>Tax picture</h2>")
    rows = [
        [f"Realised STCG ({esc(ex.fy_label)}, from capital gains report)", rs(plan.stcg_realised, 2)],
        ["Realised LTCG", rs(plan.ltcg_realised, 2)],
        ["Short-term P&amp;L booked by plan", pnl_span(st_all, 2)],
        ["Long-term equity P&amp;L booked (112A, exemption applies)", pnl_span(lt_all, 2)],
        ["Long-term gold/silver P&amp;L booked (no exemption)", pnl_span(lt_oth, 2)],
        ["Taxable STCG after set-off", rs(after.taxable_stcg, 2)],
        [f"Taxable LTCG after set-off and ₹{p.ltcg_exemption:,.0f} exemption", rs(after.taxable_ltcg, 2)],
        ["<b>Tax before</b>", f"<b>{rs(before.tax, 2)}</b>"],
        ["<b>Tax after</b>", f"<b>{rs(after.tax, 2)}</b>"],
        ["<b>Saving</b>", f'<b class="pos">{rs(saved, 2)}</b>'],
    ]
    if after.carry_forward_st or after.carry_forward_lt:
        rows.append(["Carried forward", f"ST {rs(after.carry_forward_st)} · LT {rs(after.carry_forward_lt)}"])
    if ex.charges_fy is not None:
        rows.append(["Trading charges paid this FY (Groww)", rs(ex.charges_fy, 2)])
    if ex.dividends_fy is not None:
        rows.append(["Dividends this FY (taxed at your slab, separately)", rs(ex.dividends_fy, 2)])
    a(table(["", "Amount"], rows, num_cols=(1,)))
    a(f'<p class="muted">Rates: STCG {p.stcg_rate*100:g}%, LTCG {p.ltcg_rate*100:g}% above ₹{p.ltcg_exemption:,.0f}. '
      "Excludes surcharge and 4% cess. Gold/silver ETF short-term gains are taxed at your slab rate; this model treats "
      "everything as listed equity, which only affects the gold/silver rows.</p>")

    # ---- timing watch-list
    sold_lots = set()
    for t in plan.loss_trades + plan.gain_trades:
        left = t.qty
        for lot in t.pos.lots:
            if left <= 0:
                break
            sold_lots.add(id(lot))
            left -= lot.quantity
    watch = [w for w in upcoming_term_changes(positions, now.date(), days=60) if id(w.lot) not in sold_lots]
    a("<h2>Watch-list: lots turning long-term in the next 60 days</h2>")
    if watch:
        rows = [[f"<b>{esc(w.pos.label)}</b>", qty(w.lot.quantity), w.lot.buy_date.strftime("%d %b %Y"),
                 w.turns_lt.strftime("%d %b %Y"), pnl_span(w.pnl), esc(w.advice)] for w in watch]
        a(table(["Holding", "Units", "Bought", "Long-term from", "P&amp;L", "What it means"], rows, num_cols=(1, 4)))
    else:
        a('<p class="muted">None.</p>')

    # ---- spare short-term losses
    planned = {t.pos.isin for t in plan.loss_trades + plan.gain_trades}
    spare = []
    for pos in positions:
        if pos.isin in planned or pos.asset == DEBT:
            continue
        st, lt = pos.unrealised(sell_day)
        if st < -1:
            spare.append([f"<b>{esc(pos.label)}</b>", esc(pos.asset), qty(pos.quantity), pnl_span(st), pnl_span(lt)])
    if spare:
        a("<h2>Other short-term losses (not needed this year)</h2>")
        a('<p class="muted">Selling these would only carry losses forward (8 years, ITR on time). Worth it if you expect '
          "short-term gains next year, and an easy win if you were planning to exit anyway.</p>")
        a(table(["Holding", "Class", "Units", "ST P&amp;L (all units)", "LT P&amp;L"], spare, num_cols=(2, 3, 4)))

    # ---- portfolio overview
    a("<h2>Portfolio overview</h2>")
    by_cls = {}
    for pos in positions:
        by_cls[pos.asset] = by_cls.get(pos.asset, 0) + pos.quantity * pos.price
    rows = []
    for cls_, v in sorted(by_cls.items(), key=lambda kv: -kv[1]):
        w = v / tot_val * 100 if tot_val else 0
        rows.append([esc(cls_), rs(v), f"{w:.1f}%", f'<div class="bar" style="width:{w:.1f}%"></div>'])
    a("<h3>Allocation</h3>")
    a(table(["Class", "Value", "Weight", ""], rows, num_cols=(1, 2), cls="alloc"))

    pos_by_isin = {pos.isin: pos for pos in positions}
    hold = sorted(ex.holdings, key=lambda h: -h.value)
    top5 = sum(h.value for h in hold[:5]) / tot_val * 100 if tot_val else 0
    a(f'<p class="muted">Top 5 holdings are {top5:.0f}% of the portfolio: '
      f'{esc(", ".join((pos_by_isin[h.isin].label if h.isin in pos_by_isin else h.name) for h in hold[:5]))}.</p>')
    rows = []
    for h in hold:
        pos = pos_by_isin.get(h.isin)
        st, lt = pos.unrealised(sell_day) if pos else (0.0, 0.0)
        pnl = h.value - h.invested
        flag = ' <span class="badge warn">in plan</span>' if h.isin in planned else ""
        rows.append([
            f"<b>{esc(pos.label if pos else h.name)}</b>{flag}<br><small>{esc(h.name)}</small>",
            esc(pos.asset if pos else ""), qty(h.quantity), rs(h.avg_price, 2), rs(h.close_price, 2),
            rs(h.invested), rs(h.value), pnl_span(pnl),
            f"{(pnl / h.invested * 100) if h.invested else 0:+.1f}%",
            f"{h.value / tot_val * 100 if tot_val else 0:.1f}%",
            pnl_span(st), pnl_span(lt),
        ])
    a("<h3>Holdings</h3>")
    a(table(["Holding", "Class", "Qty", "Avg", "Close", "Invested", "Value", "P&amp;L", "%", "Weight",
             "ST unrealised", "LT unrealised"], rows, num_cols=tuple(range(2, 12))))
    a('<p class="muted">ST/LT unrealised splits each holding\'s P&amp;L by lot age if sold on Day 1. Average-cost '
      "P&amp;L and lot-level tax P&amp;L can differ in sign; tax always uses the lots.</p>")
    zero = [pos.label for pos in positions if any(l.buy_price == 0 for l in pos.lots)]
    if zero:
        a(f'<div class="callout info">Zero-cost lots (bonus shares or corporate actions) in: {esc(", ".join(zero))}. '
          "Their whole sale value is a gain, short-term if sold within a year of allotment. For splits or demergers, "
          "check that the cost Groww shows is right before relying on it.</div>")

    if plan.excluded:
        a("<h2>Left out of the plan</h2>")
        rows = [[f"<b>{esc(pos.label)}</b><br><small>{esc(pos.name)}</small>", qty(pos.quantity),
                 rs(pos.quantity * pos.price), pnl_span(sum(pos.unrealised(sell_day))), esc(why)]
                for pos, why in plan.excluded]
        a(table(["Holding", "Units", "Value", "Unrealised", "Why"], rows, num_cols=(1, 2, 3)))

    a("<h2>How this was worked out</h2><ul>"
      "<li><b>Lots:</b> open lots come from the Groww P&amp;L report (\"Unrealised trades\"), already FIFO-matched, "
      "and are cross-checked against the holdings statement.</li>"
      "<li><b>FIFO sales:</b> selling N units consumes the oldest lots first. A lot is long-term if held more than "
      "12 months on the sell date.</li>"
      "<li><b>Loss harvesting:</b> picks sales one at a time by largest tax cut net of charges, using the smallest "
      "number of units that achieves it, until this year's tax reaches zero.</li>"
      "<li><b>Gain harvesting:</b> only long-term equity lots, only while total LTCG stays within the exemption, "
      f"and only if the future tax avoided ({p.ltcg_rate*100:g}%) beats charges by ₹{args.min_benefit:g}+.</li>"
      f"<li><b>Charges:</b> approximated as {args.cost_pct:g}% of value plus ₹{args.fixed_cost:g} per round trip.</li>"
      "<li><b>Prices:</b> previous close from the holdings statement. Live prices will differ.</li></ul>")
    a("<footer>Generated by portfolio_report.py from your Groww exports. Estimate only, not tax advice: confirm with "
      "a Chartered Accountant before trading.</footer></div>")
    return "\n".join(out)
