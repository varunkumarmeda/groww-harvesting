"""Tax-loss harvesting planner (and optional executor) for Groww.

Dry-run is the default. Typical flow:

  python harvest.py                                   # plan from newest report
  python harvest.py --stcg 60000 --ltcg 180000        # pick only what saves tax
  python harvest.py --stcg 60000 --ltcg 180000 --holdings   # verify vs live holdings
  python harvest.py ... --holdings --execute          # place SELL orders (asks first)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from planner import (
    OK,
    SKIP,
    Candidate,
    TaxParams,
    apply_holdings,
    apply_term_filter,
    compute_tax,
    group_lots,
    harvested_losses,
    select_for_tax,
)
from report import load_report, reconcile_with_summary

IST = timezone(timedelta(hours=5, minutes=30))

if hasattr(sys.stdout, "reconfigure"):  # Windows consoles choke on the rupee sign otherwise
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def rs(x: float) -> str:
    return f"₹{x:,.2f}"


def newest_report() -> Optional[Path]:
    files = sorted(Path(".").glob("Tax_Loss_Harvesting_Report*.xlsx"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def market_open(now: Optional[datetime] = None) -> bool:
    """NSE equity cash session, Mon-Fri 09:15-15:30 IST. Exchange holidays aren't checked."""
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= t < 15 * 60 + 30


# ------------------------------------------------------------------------ output


def print_plan(cands: List[Candidate]) -> None:
    if not cands:
        print("No loss-making holdings match the filters.")
        return
    head = f"{'':2} {'Holding':32} {'Sym':12} {'Term':5} {'Sell':>8} {'ST loss':>11} {'LT loss':>11}  Status"
    print(head)
    print("-" * len(head))
    for c in cands:
        mark = "✔" if c.selected and c.status != SKIP else " "
        print(
            f"{mark:2} {c.name[:32]:32} {(c.trading_symbol or '-')[:12]:12} {c.terms:5} "
            f"{c.sell_qty:>8g} {c.st_loss:>11,.2f} {c.lt_loss:>11,.2f}  {c.status}"
        )
        for n in c.notes:
            print(f"{'':6}↳ {n}")


def print_tax(stcg: float, ltcg: float, cands: List[Candidate], p: TaxParams) -> None:
    st, lt = harvested_losses(cands)
    before = compute_tax(stcg, ltcg, 0, 0, p)
    after = compute_tax(stcg, ltcg, st, lt, p)
    print("\nTax estimate (111A/112A only; excludes surcharge and 4% cess)")
    print(f"  Realised so far:   STCG {rs(stcg)}   LTCG {rs(ltcg)}")
    print(f"  Harvested losses:  ST   {rs(st)}   LT   {rs(lt)}")
    print(f"  Tax before:        {rs(before.tax)}")
    print(f"  Tax after:         {rs(after.tax)}   (saves {rs(before.tax - after.tax)})")
    if after.carry_forward_st or after.carry_forward_lt:
        print(
            f"  Carried forward:   ST {rs(after.carry_forward_st)}  LT {rs(after.carry_forward_lt)}"
            "  (file ITR by the due date to keep these)"
        )
    if after.wasted_loss > 0.5:
        print(f"  ⚠ {rs(after.wasted_loss)} of loss only offsets LTCG that was already exempt; it is wasted.")


def write_csv(cands: List[Candidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["selected", "status", "asset", "name", "isin", "trading_symbol", "terms",
                    "sell_qty", "held_qty", "st_loss", "lt_loss", "total_loss", "notes"])
        for c in cands:
            w.writerow([c.selected, c.status, c.asset, c.name, c.isin or "", c.trading_symbol or "",
                        c.terms, c.sell_qty, "" if c.held_qty is None else c.held_qty,
                        c.st_loss, c.lt_loss, c.total_loss, " | ".join(c.notes)])


# --------------------------------------------------------------------- execution


def execute(client, orders: List[Candidate], assume_yes: bool) -> None:
    from growwapi import GrowwAPI

    print(f"\nAbout to place {len(orders)} MARKET SELL order(s), CNC, NSE:")
    for c in orders:
        print(f"  SELL {int(c.sell_qty):>6} {c.trading_symbol:15} (books {rs(c.total_loss)})")
    if not market_open():
        print("⚠ NSE cash market looks closed right now (09:15-15:30 IST, Mon-Fri). Orders may be rejected.")
    if not assume_yes:
        if input('\nType "SELL" to place these orders: ').strip() != "SELL":
            print("Aborted. Nothing was placed.")
            return

    log = []
    day = datetime.now(IST).strftime("%Y%m%d")
    for i, c in enumerate(orders, 1):
        ref = f"tlh-{day}-{i:02d}"
        entry = {"symbol": c.trading_symbol, "isin": c.isin, "quantity": int(c.sell_qty), "reference_id": ref}
        try:
            resp = client.place_order(
                trading_symbol=c.trading_symbol,
                quantity=int(c.sell_qty),
                price=0,
                validity=GrowwAPI.VALIDITY_DAY,
                exchange=GrowwAPI.EXCHANGE_NSE,
                segment=GrowwAPI.SEGMENT_CASH,
                product=GrowwAPI.PRODUCT_CNC,
                order_type=GrowwAPI.ORDER_TYPE_MARKET,
                transaction_type=GrowwAPI.TRANSACTION_TYPE_SELL,
                order_reference_id=ref,
                timeout=15,
            )
            entry["response"] = resp
            print(f"  ✔ {c.trading_symbol}: {resp.get('order_status')} (order {resp.get('groww_order_id')})")
        except Exception as e:  # keep going; one rejection shouldn't strand the rest
            entry["error"] = str(e)
            print(f"  ✘ {c.trading_symbol}: {e}")
        log.append(entry)

    time.sleep(3)
    print("\nOrder status:")
    for entry in log:
        oid = (entry.get("response") or {}).get("groww_order_id")
        if not oid:
            continue
        try:
            st = client.get_order_status(segment=GrowwAPI.SEGMENT_CASH, groww_order_id=oid, timeout=15)
            entry["status"] = st
            print(f"  {entry['symbol']:15} {st.get('order_status')}  filled {st.get('filled_quantity')}/{entry['quantity']}")
        except Exception as e:
            print(f"  {entry['symbol']:15} status check failed: {e}")

    out = Path("output") / f"orders_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")
    print(f"\nOrder log: {out}")
    print("If you want to keep the exposure, buy back on the NEXT trading day. A same-day "
          "sell+buy of the same stock is netted as intraday and may not count as a delivery sale.")


# --------------------------------------------------------------------------- CLI


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Plan (and optionally execute) tax-loss harvesting from a Groww report.")
    ap.add_argument("--report", type=Path, help="Groww Tax Loss Harvesting XLSX (default: newest in this folder)")
    ap.add_argument("--asset", choices=["stocks", "mf", "all"], default="all")
    ap.add_argument("--term", choices=["st", "lt", "all"], default="all",
                    help="Holdings to consider, by the term of their loss lots (FIFO still applies)")
    ap.add_argument("--min-loss", type=float, default=0,
                    help="Ignore holdings whose total loss is smaller than this many rupees (charges can exceed tiny losses)")
    ap.add_argument("--stcg", type=float, help="Net STCG (111A) already realised this FY, in rupees")
    ap.add_argument("--ltcg", type=float, help="Net LTCG (112A) already realised this FY, in rupees")
    ap.add_argument("--harvest-all", action="store_true",
                    help="With --stcg/--ltcg, still select every candidate (to carry losses forward)")
    ap.add_argument("--stcg-rate", type=float, default=20.0, help="STCG rate %% (default 20)")
    ap.add_argument("--ltcg-rate", type=float, default=12.5, help="LTCG rate %% (default 12.5)")
    ap.add_argument("--ltcg-exemption", type=float, default=125000, help="LTCG exemption (default 125000)")
    ap.add_argument("--holdings", action="store_true", help="Fetch live holdings via Groww API to verify the plan")
    ap.add_argument("--holdings-file", type=Path, help="Use a saved holdings JSON (from get_holdings.py) instead")
    ap.add_argument("--csv", type=Path, help="Write the plan to this CSV (default: output/plan_<timestamp>.csv)")
    ap.add_argument("--execute", action="store_true", help="Place SELL orders for OK + selected stocks (needs --holdings)")
    ap.add_argument("--yes", action="store_true", help="Skip the typed confirmation (not recommended)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if (args.stcg is None) != (args.ltcg is None):
        print("Pass both --stcg and --ltcg (use 0 for none).")
        return 2
    if args.execute and not args.holdings:
        print("--execute needs --holdings: orders are only placed after checking live quantities.")
        return 2

    report_path = args.report or newest_report()
    if report_path is None:
        print("No report given and none named Tax_Loss_Harvesting_Report*.xlsx found here.")
        return 2
    report = load_report(report_path)
    print(f"Report: {report_path}  ({len(report.stocks)} stock lots, {len(report.mutual_funds)} MF lots)")
    for issue in reconcile_with_summary(report):
        print(f"⚠ {issue}")

    lots = (report.stocks if args.asset in ("stocks", "all") else []) + (
        report.mutual_funds if args.asset in ("mf", "all") else []
    )
    cands = apply_term_filter(group_lots(lots), args.term)
    cands = [c for c in cands if -c.total_loss >= args.min_loss]

    client = None
    holdings = None
    if args.holdings:
        from groww_auth import fetch_holdings_raw, get_client, holdings_by_isin, save_snapshot

        client = get_client()
        raw = fetch_holdings_raw(client)
        print(f"Live holdings: {len(raw)} (snapshot {save_snapshot(raw)})")
        holdings = holdings_by_isin(raw)
    elif args.holdings_file:
        from groww_auth import holdings_by_isin

        data = json.loads(args.holdings_file.read_text(encoding="utf-8"))
        holdings = holdings_by_isin(data.get("holdings", data) if isinstance(data, dict) else data)
        print(f"Holdings file: {args.holdings_file} ({len(holdings)} ISINs)")
    apply_holdings(cands, holdings)

    params = TaxParams(args.stcg_rate / 100, args.ltcg_rate / 100, args.ltcg_exemption)
    have_gains = args.stcg is not None
    if have_gains and not args.harvest_all:
        select_for_tax(cands, args.stcg, args.ltcg, params)

    print()
    print_plan(cands)
    if have_gains:
        print_tax(args.stcg, args.ltcg, cands, params)
    else:
        st, lt = harvested_losses(cands)
        print(f"\nTotal if all sold: ST loss {rs(st)}, LT loss {rs(lt)}.")
        print("Tip: pass --stcg/--ltcg with your realised gains (Groww > Reports > Capital Gains) to select only "
              "what saves tax. LT losses offset only LTCG, and losses against LTCG under ₹1.25L are wasted.")

    csv_path = args.csv or Path("output") / f"plan_{datetime.now():%Y%m%d_%H%M%S}.csv"
    write_csv(cands, csv_path)
    print(f"\nPlan saved: {csv_path}")

    orders = [c for c in cands if c.selected and c.status == OK and c.asset == "stock" and c.trading_symbol]
    blocked = [c for c in cands if c.selected and c.status != OK and c.status != SKIP]
    if blocked:
        print(f"{len(blocked)} selected holding(s) need manual review and won't be auto-sold.")
    if not args.execute:
        print(f"Dry run: {len(orders)} order(s) would be placed. Nothing was sent.")
        return 0
    if not orders:
        print("Nothing is safe to auto-sell.")
        return 0
    execute(client, orders, args.yes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
