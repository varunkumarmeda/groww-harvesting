"""Fetch Groww demat holdings, print them, and save a JSON snapshot.

The snapshot can be fed to `harvest.py --holdings-file` for offline planning.
Credentials come from .env; see groww_auth.py.
"""

import sys

from groww_auth import AuthError, fetch_holdings_raw, get_client, ltp_map, save_snapshot

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    try:
        client = get_client()
    except AuthError as e:
        print(e)
        return 1
    holdings = fetch_holdings_raw(client)
    if not holdings:
        print("No holdings found.")
        return 0

    prices = ltp_map(client, [h.get("trading_symbol") for h in holdings if h.get("trading_symbol")])
    total_inv = total_cur = 0.0
    print(f"\n{'Symbol':15} {'Qty':>8} {'Avg':>10} {'LTP':>10} {'Invested':>12} {'P&L':>12} {'P&L%':>7}")
    for h in sorted(holdings, key=lambda h: h.get("trading_symbol") or ""):
        sym = h.get("trading_symbol") or h.get("isin")
        qty = float(h.get("quantity") or 0)
        avg = float(h.get("average_price") or 0)
        inv = qty * avg
        ltp = prices.get(sym)
        if ltp is None:
            print(f"{sym:15} {qty:>8g} {avg:>10,.2f} {'-':>10} {inv:>12,.2f} {'-':>12} {'-':>7}")
            continue
        pnl = qty * ltp - inv
        total_inv += inv
        total_cur += qty * ltp
        pct = pnl / inv * 100 if inv else 0
        print(f"{sym:15} {qty:>8g} {avg:>10,.2f} {ltp:>10,.2f} {inv:>12,.2f} {pnl:>12,.2f} {pct:>+6.1f}%")

    if total_inv:
        print(f"\nPriced holdings: invested ₹{total_inv:,.2f}, value ₹{total_cur:,.2f}, "
              f"P&L ₹{total_cur - total_inv:,.2f}")
    print(f"Snapshot saved: {save_snapshot(holdings)}")
    print("Note: P&L here uses average price. Tax uses per-lot FIFO cost, so use the "
          "Groww Tax Loss Harvesting report for harvesting decisions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
