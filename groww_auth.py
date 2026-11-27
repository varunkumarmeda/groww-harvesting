"""Groww Trade API authentication from environment / .env. Never hardcode tokens.

Supported (first match wins):
  GROWW_ACCESS_TOKEN (or GROWW_AUTH_TOKEN)   daily access token from the Groww API page
  GROWW_API_KEY + GROWW_TOTP_SECRET          TOTP flow, no daily approval
  GROWW_API_KEY + GROWW_API_SECRET           key + secret, needs daily approval
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:  # python-dotenv optional; plain env vars still work
    pass


class AuthError(RuntimeError):
    pass


def _env(*names: str) -> Optional[str]:
    for n in names:
        v = os.getenv(n)
        if v and v.strip():
            return v.strip()
    return None


def get_client():
    """Return an authenticated growwapi.GrowwAPI client."""
    from growwapi import GrowwAPI  # slow import; only when actually needed

    token = _env("GROWW_ACCESS_TOKEN", "GROWW_AUTH_TOKEN")
    api_key = _env("GROWW_API_KEY")
    totp_secret = _env("GROWW_TOTP_SECRET")
    api_secret = _env("GROWW_API_SECRET")

    try:
        if token:
            return GrowwAPI(token)
        if api_key and totp_secret:
            import pyotp

            code = pyotp.TOTP(totp_secret.replace(" ", "").upper()).now()
            return GrowwAPI(GrowwAPI.get_access_token(api_key=api_key, totp=code))
        if api_key and api_secret:
            return GrowwAPI(GrowwAPI.get_access_token(api_key=api_key, secret=api_secret))
    except Exception as e:
        raise AuthError(
            f"Groww authentication failed: {e}\n"
            "  - Access token: tokens expire daily; generate a fresh one.\n"
            "  - TOTP: secret must be base32 (A-Z, 2-7).\n"
            "  - Key + secret: approve today's request on the Groww API page first."
        ) from e
    raise AuthError(
        "No Groww credentials found. Set GROWW_ACCESS_TOKEN, or GROWW_API_KEY with "
        "GROWW_TOTP_SECRET / GROWW_API_SECRET, in .env (see .env.example)."
    )


def fetch_holdings_raw(client, timeout: int = 15) -> list:
    return client.get_holdings_for_user(timeout=timeout).get("holdings", []) or []


def save_snapshot(holdings: list, folder: str = "holdings") -> Path:
    Path(folder).mkdir(exist_ok=True)
    out = Path(folder) / f"holdings_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({"holdings": holdings}, indent=2), encoding="utf-8")
    return out


def holdings_by_isin(raw: list) -> Dict[str, "Holding"]:
    from planner import Holding

    result = {}
    for h in raw:
        isin = (h.get("isin") or "").upper()
        if not isin:
            continue
        qty = float(h.get("quantity") or 0)
        locked = sum(
            float(h.get(k) or 0)
            for k in ("pledge_quantity", "demat_locked_quantity", "groww_locked_quantity")
        )
        result[isin] = Holding(
            trading_symbol=h.get("trading_symbol") or "",
            quantity=qty,
            sellable_quantity=max(qty - locked, 0.0),
        )
    return result
