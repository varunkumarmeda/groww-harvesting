"""Portfolio + tax-loss harvesting report (self-contained HTML).

Answers: which holdings to SELL today to book short-term capital losses, what to
BUY BACK on the next trading day, what it's worth in tax, and what needs a manual
check. With live holdings (--holdings) it also adds a full portfolio overview.

  python portfolio_report.py                              # from the newest report
  python portfolio_report.py --stcg 60000 --ltcg 180000   # select only what saves tax
  python portfolio_report.py --stcg 60000 --ltcg 180000 --holdings --open
"""

from __future__ import annotations

import argparse
import html
import os
import sys
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from harvest import IST, PlanError, add_plan_args, build_plan
from planner import OK, REVIEW, SKIP, Candidate, compute_tax, group_lots, harvested_losses
from report import LT, ST, report_date

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:  # lets GROWW_EXPORTS_DIR live in .env
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass


# ----------------------------------------------------------------- small helpers


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def rs(x: Optional[float], dp: int = 0) -> str:
    if x is None:
        return "—"
    sign = "−" if x < 0 else ""
    return f"{sign}₹{abs(x):,.{dp}f}"


def qty(x: float) -> str:
    return f"{x:g}" if x == int(x) else f"{x:,.3f}"


def next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def default_sell_date(now: datetime) -> date:
    """Today if the NSE session hasn't closed yet on a weekday, else the next weekday."""
    today = now.date()
    if today.weekday() < 5 and (now.hour, now.minute) < (15, 30):
        return today
    return next_weekday(today)


def table(headers: List[str], rows: List[List[str]], num_cols=(), cls="") -> str:
    """rows are pre-escaped HTML cell strings."""
    if not rows:
        return '<p class="muted">Nothing here.</p>'
    th = "".join(f'<th class="{"num" if i in num_cols else ""}">{h}</th>' for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f'<td class="{"num" if i in num_cols else ""}">{c}</td>' for i, c in enumerate(r)) + "</tr>"
        for r in rows
    )
    return f'<div class="tw"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def badge(status: str, text: Optional[str] = None) -> str:
    cls = {OK: "ok", REVIEW: "warn", SKIP: "bad"}.get(status, "info")
    return f'<span class="badge {cls}">{esc(text or status)}</span>'


def notes_html(c: Candidate) -> str:
    keep = [n for n in c.notes if not n.startswith("Not selected")]
    return "<br>".join(esc(n) for n in keep)


# ------------------------------------------------------------------- the report


CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--muted:#6b6b66;--line:#e4e4df;--accent:#1f6f5c;
--ok:#1f7a4d;--okbg:#e3f3ea;--warn:#8a5a00;--warnbg:#fbf0d6;--bad:#9b2c2c;--badbg:#f8e1e1;--info:#2c5a9b;--infobg:#e2ebf8;}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--fg:#ececea;--muted:#a0a09a;--line:#33332f;
--accent:#5fc4a6;--ok:#7fd4a6;--okbg:#1c3428;--warn:#f0c674;--warnbg:#3a2f16;--bad:#f19a9a;--badbg:#3d1f1f;--info:#9cc0f5;--infobg:#1d2a3d;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:36px 0 10px;padding-top:8px;border-top:1px solid var(--line)}
h3{font-size:15px;margin:22px 0 8px}.muted{color:var(--muted)}small{color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:18px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi .l{font-size:12px;color:var(--muted)}.kpi .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.kpi .s{font-size:12px;color:var(--muted)}
.tw{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;background:var(--card);position:sticky;top:0}
tr:last-child td{border-bottom:0}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.notes,td:last-child{font-size:12px}
.neg{color:var(--bad)}.pos{color:var(--ok)}
.badge{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;font-weight:600;white-space:nowrap}
.badge.ok{background:var(--okbg);color:var(--ok)}.badge.warn{background:var(--warnbg);color:var(--warn)}
.badge.bad{background:var(--badbg);color:var(--bad)}.badge.info{background:var(--infobg);color:var(--info)}
.callout{border-left:4px solid var(--warn);background:var(--warnbg);padding:10px 14px;border-radius:6px;margin:12px 0}
.callout.info{border-color:var(--info);background:var(--infobg)}.callout.bad{border-color:var(--bad);background:var(--badbg)}
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.step{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.step h3{margin-top:0}.step .d{font-size:12px;color:var(--accent);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
ol,ul{padding-left:20px;margin:6px 0}li{margin:3px 0}
footer{margin-top:40px;font-size:12px;color:var(--muted)}
@media print{body{background:#fff}.tw,.kpi,.step{border-color:#ccc}th{position:static}}
"""


def build_html(plan, args, now: datetime) -> str:
    cands = plan.cands
    report = plan.report
    p = plan.params
    have_gains = args.stcg is not None
    rdate = report_date(plan.report_path)
    age = (now.date() - rdate).days
    sell_day = args.sell_date or default_sell_date(now)
    rebuy_day = next_weekday(sell_day)

    raw = plan.raw_holdings or []
    by_isin: Dict[str, dict] = {(h.get("isin") or "").upper(): h for h in raw}
    prices: Dict[str, float] = {}
    if plan.client is not None and raw and not args.no_ltp:
        from groww_auth import ltp_map

        prices = ltp_map(plan.client, [h["trading_symbol"] for h in raw if h.get("trading_symbol")])

    def ltp_for(c: Candidate) -> Optional[float]:
        return prices.get(c.trading_symbol) if c.trading_symbol else None

    sell = [c for c in cands if c.selected and c.status != SKIP]
    stocks_sell = [c for c in sell if c.asset == "stock"]
    mf_sell = [c for c in sell if c.asset == "mf"]
    unselected = [c for c in cands if not c.selected and c.status != SKIP]
    skipped = [c for c in cands if c.status == SKIP]
    st_booked, lt_booked = harvested_losses(cands)

    if have_gains:
        before = compute_tax(args.stcg, args.ltcg, 0, 0, p)
        after = compute_tax(args.stcg, args.ltcg, st_booked, lt_booked, p)
        saved = before.tax - after.tax
    else:
        before = after = None
        saved = st_booked * p.stcg_rate + lt_booked * p.ltcg_rate  # ceiling: needs enough gains

    total_value = sum((ltp_for(c) or 0) * c.sell_qty for c in stocks_sell)
    cost_pct = args.cost_pct / 100

    out: List[str] = []
    a = out.append
    a(f"<title>Tax-Loss Harvesting Report · {esc(now.strftime('%d %b %Y'))}</title><style>{CSS}</style>")
    a('<div class="wrap">')
    a("<h1>Portfolio &amp; Tax-Loss Harvesting Report</h1>")
    src = esc(plan.holdings_source) if plan.holdings_source else "not connected (report only)"
    a(f'<p class="muted">Generated {esc(now.strftime("%d %b %Y, %H:%M"))} IST · Groww report dated '
      f'{esc(rdate.strftime("%d %b %Y"))} ({esc(plan.report_path.name)}) · Live holdings: {src}</p>')

    # --- warnings up top
    if age > 3:
        a(f'<div class="callout bad"><b>The Groww report is {age} days old.</b> Prices, quantities and the '
          f"ST/LT split have changed since then, and lots that were short-term may now be long-term. "
          f"Download a fresh report (Groww → Reports → Tax Loss Harvesting) before acting on this.</div>")
    for issue in plan.issues:
        a(f'<div class="callout">Report check: {esc(issue)}</div>')
    if plan.raw_holdings is None:
        a('<div class="callout info">Live holdings weren\'t checked, so every row below is marked REVIEW. '
          "Run with <code>--holdings</code> to confirm trading symbols, quantities, pledges and FIFO safety, "
          "and to add a full portfolio overview with current prices.</div>")

    # --- KPIs
    sm = report.summary
    tot_st = sum(v[ST] for v in sm.values() if v) if sm else None
    stk = sm.get("Stocks") if sm else None
    grand = sm.get("Grand Total") if sm else None
    a('<div class="kpis">')
    if grand:
        a(f'<div class="kpi"><div class="l">Unrealised ST loss available</div><div class="v neg">{rs(grand[ST])}</div>'
          f'<div class="s">LT: {rs(grand[LT])} · per Groww summary</div></div>')
    elif tot_st is not None:
        a(f'<div class="kpi"><div class="l">Unrealised ST loss available</div><div class="v neg">{rs(tot_st)}</div></div>')
    a(f'<div class="kpi"><div class="l">Loss booked by this plan</div><div class="v">{rs(st_booked + lt_booked)}</div>'
      f'<div class="s">ST {rs(st_booked)} · LT {rs(lt_booked)}</div></div>')
    if have_gains:
        a(f'<div class="kpi"><div class="l">Estimated tax saved</div><div class="v pos">{rs(saved)}</div>'
          f'<div class="s">{rs(before.tax)} → {rs(after.tax)}</div></div>')
    else:
        a(f'<div class="kpi"><div class="l">Tax value, if fully used</div><div class="v">up to {rs(saved)}</div>'
          f'<div class="s">Add --stcg/--ltcg for the real figure</div></div>')
    a(f'<div class="kpi"><div class="l">Trades</div><div class="v">{len(stocks_sell)} sell'
      f'{" + " + str(len(mf_sell)) + " MF" if mf_sell else ""}</div>'
      f'<div class="s">Sell {esc(sell_day.strftime("%a %d %b"))} · rebuy {esc(rebuy_day.strftime("%a %d %b"))}</div></div>')
    if total_value:
        a(f'<div class="kpi"><div class="l">Capital cycled (approx.)</div><div class="v">{rs(total_value)}</div>'
          f'<div class="s">~{rs(total_value * cost_pct)} round-trip costs at {args.cost_pct:g}%</div></div>')
    a("</div>")

    # --- action plan
    a("<h2>Action plan</h2>")
    if not sell:
        a('<p>No holding is selected. Either nothing is in short-term loss, or (with --stcg/--ltcg) booking '
          "more loss wouldn't reduce this year's tax.</p>")
    else:
        ok_n = sum(1 for c in stocks_sell if c.status == OK)
        a('<div class="steps">')
        a(f'<div class="step"><div class="d">Day 1 · {esc(sell_day.strftime("%A %d %b %Y"))}</div><h3>Sell</h3><ol>'
          f"<li>Download a fresh Tax Loss Harvesting report and re-run this tool.</li>"
          f"<li>Resolve every <b>REVIEW</b> item (see the notes column).</li>"
          f"<li>Sell the {len(stocks_sell)} stock/ETF position(s) below as <b>delivery (CNC)</b> during market hours. "
          f"{ok_n} are verified for <code>harvest.py --execute</code>.</li>"
          + (f"<li>Redeem the {len(mf_sell)} mutual fund(s) in the Groww app.</li>" if mf_sell else "")
          + "<li>Check the contract note shows a <i>delivery</i> sale.</li></ol></div>")
        a(f'<div class="step"><div class="d">Day 2 · {esc(rebuy_day.strftime("%A %d %b %Y"))}</div><h3>Buy back</h3><ol>'
          "<li>Wait for the next trading day. Buying back the same day nets off as an intraday trade and the loss may not count.</li>"
          "<li>Buy the same quantities back (table below), or a similar fund/ETF if you'd rather not hold the stock again.</li>"
          "<li>The new units start a fresh 12-month holding period at the new (lower) cost.</li></ol>"
          "<small>Market holidays aren't checked. If Day 2 is an NSE holiday, use the next trading day.</small></div>")
        a("</div>")

        rows = []
        for c in sorted(stocks_sell, key=lambda c: c.total_loss):
            ltp = ltp_for(c)
            val = ltp * c.sell_qty if ltp else None
            cost = val * cost_pct if val else None
            worth = ""
            maxval = -c.st_loss * p.stcg_rate - c.lt_loss * p.ltcg_rate
            if cost is not None and cost >= maxval:
                worth = '<br><span class="badge bad">costs ≥ tax value</span>'
            rows.append([
                f"<b>{esc(c.trading_symbol or '—')}</b><br><small>{esc(c.name)}</small>",
                esc(c.terms), qty(c.sell_qty),
                f'<span class="neg">{rs(c.st_loss)}</span>', f'<span class="neg">{rs(c.lt_loss)}</span>' if c.lt_loss else "—",
                rs(ltp, 2) if ltp else "—", rs(val), rs(maxval),
                badge(c.status) + worth, notes_html(c),
            ])
        a("<h3>Day 1: sell these</h3>")
        a(table(["Stock / ETF", "Lots", "Qty", "ST loss", "LT loss", "LTP", "Value", "Max tax value", "Status", "Notes"],
                rows, num_cols=(2, 3, 4, 5, 6, 7)))
        a('<p class="muted">"Max tax value" assumes the loss offsets gains at the full rate '
          f"(ST {p.stcg_rate*100:g}%, LT {p.ltcg_rate*100:g}%). The real saving depends on your gains; see the tax section.</p>")

        if mf_sell:
            rows = [[f"<b>{esc(c.name)}</b>", esc(c.terms), qty(c.sell_qty), f'<span class="neg">{rs(c.st_loss)}</span>',
                     f'<span class="neg">{rs(c.lt_loss)}</span>' if c.lt_loss else "—", badge("INFO", "Redeem in app")]
                    for c in sorted(mf_sell, key=lambda c: c.total_loss)]
            a("<h3>Day 1: redeem these mutual funds (manually)</h3>")
            a(table(["Fund", "Lots", "Units", "ST loss", "LT loss", ""], rows, num_cols=(2, 3, 4)))
            a('<p class="muted">MF redemptions settle at that day\'s NAV (cut-off 3 pm). Check for exit loads before redeeming.</p>')

        rows = []
        for c in sorted(sell, key=lambda c: (c.asset != "stock", c.total_loss)):
            ltp = ltp_for(c)
            rows.append([
                f"<b>{esc(c.trading_symbol or c.name)}</b>" + (f"<br><small>{esc(c.name)}</small>" if c.trading_symbol else ""),
                "Mutual fund" if c.asset == "mf" else "Stock/ETF",
                qty(c.sell_qty), rs(ltp * c.sell_qty) if ltp else "—",
                "Buy units worth the redeemed amount" if c.asset == "mf" else f"BUY {qty(c.sell_qty)} CNC",
            ])
        a(f"<h3>Day 2 ({esc(rebuy_day.strftime('%a %d %b'))}): buy back</h3>")
        a(table(["Holding", "Type", "Qty", "Approx. cost", "Order"], rows, num_cols=(2, 3)))

    # --- tax picture
    a("<h2>Tax picture</h2>")
    if have_gains:
        rows = [
            ["Realised STCG so far (111A)", rs(args.stcg)],
            ["Realised LTCG so far (112A)", rs(args.ltcg)],
            ["ST loss booked by plan", rs(-st_booked)],
            ["LT loss booked by plan", rs(-lt_booked)],
            ["Taxable STCG after set-off", rs(after.taxable_stcg)],
            [f"Taxable LTCG after set-off and ₹{p.ltcg_exemption:,.0f} exemption", rs(after.taxable_ltcg)],
            ["<b>Tax before</b>", f"<b>{rs(before.tax)}</b>"],
            ["<b>Tax after</b>", f"<b>{rs(after.tax)}</b>"],
            ['<b>Saving</b>', f'<b class="pos">{rs(saved)}</b>'],
        ]
        if after.carry_forward_st or after.carry_forward_lt:
            rows.append(["Loss carried forward (file ITR on time)",
                         f"ST {rs(after.carry_forward_st)} · LT {rs(after.carry_forward_lt)}"])
        if after.wasted_loss > 0.5:
            rows.append(['<span class="neg">Loss wasted against already-exempt LTCG</span>', rs(after.wasted_loss)])
        a(table(["", "Amount"], rows, num_cols=(1,)))
        a('<p class="muted">Excludes surcharge, 4% cess and any basic-exemption-limit adjustment.</p>')
    else:
        a("<p>You didn't enter your realised gains for the year, so the tool can't tell how much loss is "
          "actually useful. It has selected every short-term loss holding. To get the exact saving and "
          "drop holdings that wouldn't help, re-run with the figures from Groww → Reports → Capital Gains:</p>"
          "<p><code>python portfolio_report.py --stcg &lt;STCG&gt; --ltcg &lt;LTCG&gt;</code></p>"
          "<ul><li>Short-term loss offsets STCG (20%) first, then LTCG.</li>"
          "<li>Long-term loss offsets only LTCG, and the first ₹1.25L of LTCG is already tax-free. "
          "Booking LT loss when your LTCG is below that wastes it.</li>"
          "<li>Unused loss carries forward 8 years if you file your ITR by the due date.</li></ul>")

    # --- review / not selected / skipped
    others = []
    for c in unselected:
        others.append([esc(c.trading_symbol or c.name), esc(c.terms), qty(c.sell_qty), rs(c.st_loss), rs(c.lt_loss),
                       badge("INFO", "No extra tax saving")])
    for c in skipped:
        others.append([esc(c.trading_symbol or c.name), esc(c.terms), qty(c.sell_qty), rs(c.st_loss), rs(c.lt_loss),
                       badge(SKIP) + "<br><small>" + notes_html(c) + "</small>"])
    if others:
        a("<h2>Considered but not in the plan</h2>")
        a(table(["Holding", "Lots", "Qty", "ST loss", "LT loss", "Why"], others, num_cols=(2, 3, 4)))

    # --- portfolio overview
    a("<h2>Portfolio overview</h2>")
    if raw:
        a(portfolio_section(raw, prices, {c.isin for c in sell if c.isin}))
    else:
        a('<p class="muted">Connect live holdings (<code>--holdings</code>) for current value, P&amp;L, weights and '
          "concentration. Until then, here is every loss position in the Groww report:</p>")
    if sm:
        rows = [[esc(k), rs(v[ST], 2), rs(v[LT], 2), rs(v[ST] + v[LT], 2)] for k, v in sm.items()]
        a("<h3>Unrealised losses by category (Groww summary)</h3>")
        a(table(["Category", "Short-term", "Long-term", "Total"], rows, num_cols=(1, 2, 3)))
    all_loss = group_lots(report.stocks + report.mutual_funds)
    rows = [[esc(c.name), "MF" if c.asset == "mf" else "Stock/ETF", esc(c.terms), qty(c.sell_qty),
             rs(c.st_loss, 2) if c.st_qty else "—", rs(c.lt_loss, 2) if c.lt_qty else "—", rs(c.total_loss, 2)]
            for c in all_loss]
    a("<h3>All loss positions in the report</h3>")
    a(table(["Holding", "Type", "Lots", "Qty in loss", "ST loss", "LT loss", "Total"], rows, num_cols=(3, 4, 5, 6)))

    # --- method
    a("<h2>How this was worked out</h2><ul>"
      "<li><b>FIFO:</b> a sale is matched against your oldest units first. If a stock has older long-term units, "
      "you can't sell just the short-term ones, so the plan sells all loss units of that holding. Holdings where "
      "you own more units than are in loss are flagged, because FIFO could sell profitable lots.</li>"
      "<li><b>Selection:</b> with --stcg/--ltcg, holdings are added one at a time by largest extra tax saved, "
      "stopping when nothing more helps.</li>"
      f"<li><b>Costs:</b> estimated at {args.cost_pct:g}% of value round-trip (STT, exchange, stamp, DP, GST). "
      "Check Groww's pricing for exact charges.</li>"
      "<li><b>Rebuy:</b> next weekday after the sale. Exchange holidays aren't modelled.</li></ul>")
    a("<footer>Generated by portfolio_report.py from your Groww Tax Loss Harvesting report. This is an estimate, "
      "not tax advice: confirm with a Chartered Accountant before trading. Prices are last traded prices at "
      "generation time and will move.</footer></div>")
    return "\n".join(out)


def portfolio_section(raw: list, prices: Dict[str, float], planned_isins: set) -> str:
    rows, tot_inv, tot_val, priced = [], 0.0, 0.0, []
    for h in raw:
        sym = h.get("trading_symbol") or h.get("isin")
        q = float(h.get("quantity") or 0)
        avg = float(h.get("average_price") or 0)
        inv = q * avg
        ltp = prices.get(sym)
        val = q * ltp if ltp else None
        tot_inv += inv
        if val is not None:
            tot_val += val
            priced.append((sym, inv, val))
        rows.append((sym, q, avg, ltp, inv, val, (h.get("isin") or "").upper() in planned_isins))

    parts = []
    if priced:
        pinv = sum(i for _, i, _ in priced)
        pnl = tot_val - pinv
        parts.append('<div class="kpis">'
                     f'<div class="kpi"><div class="l">Current value</div><div class="v">{rs(tot_val)}</div>'
                     f'<div class="s">{len(priced)} of {len(raw)} holdings priced</div></div>'
                     f'<div class="kpi"><div class="l">Invested</div><div class="v">{rs(pinv)}</div></div>'
                     f'<div class="kpi"><div class="l">Unrealised P&amp;L</div><div class="v {"pos" if pnl >= 0 else "neg"}">{rs(pnl)}</div>'
                     f'<div class="s">{pnl / pinv * 100:+.1f}% on average cost</div></div>')
        top = sorted(priced, key=lambda t: -t[2])[:5]
        top_w = sum(v for _, _, v in top) / tot_val * 100 if tot_val else 0
        parts.append(f'<div class="kpi"><div class="l">Top-5 concentration</div><div class="v">{top_w:.0f}%</div>'
                     f'<div class="s">{esc(", ".join(s for s, _, _ in top))}</div></div></div>')

    rows.sort(key=lambda r: -(r[5] if r[5] is not None else r[4]))
    body = []
    for sym, q, avg, ltp, inv, val, planned in rows:
        pnl = (val - inv) if val is not None else None
        pct = (pnl / inv * 100) if pnl is not None and inv else None
        w = (val / tot_val * 100) if val and tot_val else None
        cls = "pos" if (pnl or 0) >= 0 else "neg"
        body.append([
            f"<b>{esc(sym)}</b>" + (' <span class="badge warn">in plan</span>' if planned else ""),
            qty(q), rs(avg, 2), rs(ltp, 2) if ltp else "—", rs(inv), rs(val),
            f'<span class="{cls}">{rs(pnl)}</span>' if pnl is not None else "—",
            f'<span class="{cls}">{pct:+.1f}%</span>' if pct is not None else "—",
            f"{w:.1f}%" if w is not None else "—",
        ])
    parts.append(table(["Symbol", "Qty", "Avg cost", "LTP", "Invested", "Value", "P&amp;L", "P&amp;L %", "Weight"],
                       body, num_cols=(1, 2, 3, 4, 5, 6, 7, 8)))
    parts.append('<p class="muted">P&amp;L here uses average cost. Tax is worked out per lot (FIFO), which is why a '
                 "holding can show an overall gain and still have short-term lots in loss.</p>")
    return "\n".join(parts)


# --------------------------------------------------------------------------- CLI


def _write(doc: str, out: Path, open_it: bool) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                   "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                   + doc.replace("</style>", "</style></head><body>", 1) + "</body></html>", encoding="utf-8")
    print(f"Report written: {out.resolve()}")
    if open_it:
        webbrowser.open(out.resolve().as_uri())


def live_check(ex) -> None:
    """Compare exported holdings with live Groww holdings (read-only API call)."""
    from exports import compare_with_live
    from groww_auth import _env

    if not (_env("GROWW_ACCESS_TOKEN", "GROWW_AUTH_TOKEN") or _env("GROWW_API_KEY")):
        ex.live_status = "skipped (no Groww API credentials in .env)"
        return
    try:
        from groww_auth import fetch_holdings_raw, get_client

        live = fetch_holdings_raw(get_client())
    except Exception as e:  # never block the report on the API
        ex.live_status = f"failed: {e}"
        print(f"Live holdings check failed ({e}); continuing with exports only.")
        return
    ex.live_diffs = compare_with_live(ex, live)
    ex.live_status = f"checked {datetime.now(IST):%d %b %H:%M} IST against {len(live)} live holdings"
    if ex.live_diffs:
        print(f"⚠ Holdings changed since the export ({len(ex.live_diffs)}):")
        for d in ex.live_diffs:
            print(f"   - {d}")
        print("   Re-export Holdings + P&L from Groww before acting on this plan.")
    else:
        print("✓ Live holdings match the export.")


def run_exports(args, now: datetime) -> int:
    from exports import load_exports
    from exports_report import build_exports_html
    from lot_planner import build_positions, plan_from_positions
    from planner import TaxParams

    ex = load_exports(args.exports, today=now.date())
    print(f"Exports: {ex.folder}  ({len(ex.holdings)} holdings, {len(ex.open_lots)} open lots, prices {ex.as_of})")
    if not args.no_live_check:
        live_check(ex)
    if not ex.open_lots:
        print("No open lots found: the folder needs a Stocks_PnL_Report_*.xlsx covering today.")
        return 2
    stcg = args.stcg if args.stcg is not None else ex.realised_st
    ltcg = args.ltcg if args.ltcg is not None else ex.realised_lt
    if stcg is None or ltcg is None:
        print("No capital gains report for this FY in the folder; pass --stcg and --ltcg.")
        return 2
    for w in ex.warnings:
        print(f"⚠ {w}")
    positions = build_positions(ex)
    sell_day = args.sell_date or default_sell_date(now)
    # Split realised LTCG: gold/silver ETFs get no 112A exemption.
    from lot_planner import EQUITY, asset_class
    ltcg_other = 0.0
    if args.ltcg is None and ex.realised_trades:
        ltcg_other = sum(t.pnl for t in ex.realised_trades if t.term == "LT" and asset_class(t.name, t.isin) != EQUITY)
        ltcg -= ltcg_other
    params = TaxParams(args.stcg_rate / 100, args.ltcg_rate / 100, args.ltcg_exemption)
    plan = plan_from_positions(positions, sell_day, stcg, ltcg, params, cost_pct=args.cost_pct / 100,
                               fixed_cost=args.fixed_cost, min_benefit=args.min_benefit,
                               harvest_gains=not args.no_gain_harvest, ltcg_other_realised=ltcg_other)
    before, after = plan.tax(), plan.tax(*plan.totals())
    print(f"Plan: {len(plan.loss_trades)} loss + {len(plan.gain_trades)} gain sale(s) on {sell_day:%d %b}; "
          f"tax {before.tax:,.0f} -> {after.tax:,.0f}")
    _write(build_exports_html(ex, positions, plan, args, now),
           args.out or Path("output") / f"portfolio_report_{now:%Y%m%d_%H%M%S}.html", args.open)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="HTML portfolio + tax-loss harvesting report from Groww data.")
    ap.add_argument("--exports", type=Path, default=os.getenv("GROWW_EXPORTS_DIR") or None,
                    help="Folder of Groww exports (Holdings, P&L, Capital Gains, Order History). "
                         "Lot-level mode; recommended. Default: $GROWW_EXPORTS_DIR")
    add_plan_args(ap, default_term="st")
    ap.add_argument("--sell-date", type=date.fromisoformat, help="Day you'll sell, YYYY-MM-DD (default: today if the market is still open, else next weekday)")
    ap.add_argument("--cost-pct", type=float, default=0.25, help="Approx. round-trip trading cost %% of value (default 0.25)")
    ap.add_argument("--fixed-cost", type=float, default=40.0, help="Exports mode: flat charges per round trip, brokerage + DP (default 40)")
    ap.add_argument("--min-benefit", type=float, default=100.0, help="Exports mode: min net benefit for a gain-harvest sale (default 100)")
    ap.add_argument("--no-gain-harvest", action="store_true", help="Exports mode: skip the LTCG-exemption section")
    ap.add_argument("--no-live-check", action="store_true",
                    help="Exports mode: don't compare the export with live Groww holdings via the API")
    ap.add_argument("--no-ltp", action="store_true", help="Don't fetch live prices")
    ap.add_argument("--out", type=Path, help="Output HTML path (default: output/portfolio_report_<timestamp>.html)")
    ap.add_argument("--open", action="store_true", help="Open the report in your browser")
    args = ap.parse_args(argv)
    now = datetime.now(IST).replace(tzinfo=None)

    if args.exports:
        try:
            return run_exports(args, now)
        except FileNotFoundError as e:
            print(e)
            return 2

    try:
        plan = build_plan(args)
    except PlanError as e:
        print(e)
        return 2
    _write(build_html(plan, args, now),
           args.out or Path("output") / f"portfolio_report_{now:%Y%m%d_%H%M%S}.html", args.open)
    return 0


if __name__ == "__main__":
    sys.exit(main())
