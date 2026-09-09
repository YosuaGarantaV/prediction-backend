"""Fundamental emiten dari yfinance (GRATIS & akurat) — di-cache di DB.

Hanya di-fetch untuk saham yang BENAR-BENAR dianalisis LLM (kandidat top), dan
di-cache beberapa hari (fundamental berubah lambat) → efisien, tak boros request.
Memberi agen: PER/PBV/ROE/dividen/margin/growth/utang + konsensus & target analis.
"""
from __future__ import annotations

from datetime import datetime, timezone

import yfinance as yf

from app import repo
from app.data import msci

CACHE_DAYS = 3


def _pct(v):
    return round(v * 100, 1) if isinstance(v, (int, float)) else None


def fetch_fundamentals(ticker: str) -> dict | None:
    try:
        info = yf.Ticker(f"{ticker}.JK").info
    except Exception:  # noqa: BLE001
        return None
    if not info or not info.get("sector") and not info.get("trailingPE"):
        return None
    f = {
        "ticker": ticker,
        "per": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "pbv": info.get("priceToBook"),
        "roe": _pct(info.get("returnOnEquity")),
        "div_yield": info.get("dividendYield"),
        "profit_margin": _pct(info.get("profitMargins")),
        "earnings_growth": _pct(info.get("earningsGrowth")),
        "revenue_growth": _pct(info.get("revenueGrowth")),
        "debt_equity": info.get("debtToEquity"),
        "beta": info.get("beta"),
        "rec_key": info.get("recommendationKey"),
        "target_price": info.get("targetMeanPrice"),
        "mcap": info.get("marketCap"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
    }
    # Guard nilai ngaco dari Yahoo (mis. PBV ADRO pernah 13058).
    if f["pbv"] and not (0 < f["pbv"] < 100):
        f["pbv"] = None
    for k in ("per", "forward_pe"):
        if f[k] and not (-1000 < f[k] < 1000):
            f[k] = None
    repo.save_fundamentals(f)
    return f


def get_fundamentals(ticker: str, max_age_days: int = CACHE_DAYS) -> dict | None:
    """Pakai cache kalau masih segar, kalau tidak fetch baru."""
    row = repo.get_fundamentals_row(ticker)
    if row and row.get("ts"):
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["ts"])).days
            if age <= max_age_days:
                return row
        except Exception:  # noqa: BLE001
            pass
    return fetch_fundamentals(ticker) or row


def fundamentals_brief(ticker: str) -> str:
    f = get_fundamentals(ticker)
    msci_line = msci.msci_note(ticker)
    if not f:
        return f"(fundamental belum tersedia)\n{msci_line}"
    parts = []
    if f.get("per"):
        parts.append(f"PER {f['per']:.1f}")
    if f.get("forward_pe"):
        parts.append(f"fwdPE {f['forward_pe']:.1f}")
    if f.get("pbv"):
        parts.append(f"PBV {f['pbv']:.2f}")
    if f.get("roe") is not None:
        parts.append(f"ROE {f['roe']}%")
    if f.get("div_yield"):
        parts.append(f"div {f['div_yield']}%")
    if f.get("profit_margin") is not None:
        parts.append(f"margin {f['profit_margin']}%")
    if f.get("earnings_growth") is not None:
        parts.append(f"EPS growth {f['earnings_growth']}%")
    if f.get("debt_equity") is not None:
        parts.append(f"DER {f['debt_equity']}")
    analyst = ""
    if f.get("rec_key") or f.get("target_price"):
        analyst = (f" | Konsensus analis: {f.get('rec_key','-')}, "
                   f"target {f.get('target_price')}")
    sector = f" | {f.get('sector','')}/{f.get('industry','')}" if f.get("sector") else ""
    return f"{', '.join(parts)}{analyst}{sector}\n{msci_line}"
