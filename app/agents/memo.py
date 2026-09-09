"""Memoization tesis analyst — hemat token TANPA membekukan otak.

Tesis analis (panggilan LLM termahal, ~6.400 tok) dipakai ULANG hanya selama input yang
menentukan tesis TIDAK berubah. Begitu ada perubahan nyata → analisis fresh. Jadi mikirnya
tetap DINAMIS (event-driven), cuma berhenti menghitung ulang kesimpulan yang identik.

Pemicu mikir-ulang (cache di-invalidate) — salah satu saja cukup:
  1. Berita KHUSUS emiten baru (repo.ticker_news_key berubah)
  2. Harga gerak > PRICE_MOVE_PCT
  3. Rezim makro flip (bucket lean risk-on/off berubah)
  4. Arus asing berubah materiil (bucket foreign_pressure)
  5. Hari bursa baru
  6. TTL lewat (jaring pengaman: walau sepi, paksa segar tiap TTL_HOURS)

ponytail: cache in-memory + lock; hilang saat restart (lalu rebuild sendiri — aman, cuma
1 siklus pertama analisis penuh). Persistensi ke disk tak perlu untuk engine 24/7.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from app import repo

_WIB = repo._WIB  # satu sumber definisi WIB (jangan duplikat)
_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}   # ticker -> {view, price, sig, ts}

PRICE_MOVE_PCT = 1.2   # harga gerak lebih dari ini → tesis dianalisis ulang
TTL_HOURS = 3.0        # walau tak ada perubahan, paksa segar tiap 3 jam

_hits = 0              # statistik: berapa kali tesis dipakai ulang (hemat)
_misses = 0


def _signature(ticker: str, quote: dict) -> tuple:
    """Bagian yang HARUS sama persis agar tesis boleh dipakai ulang (selain harga & TTL)."""
    day = datetime.now(_WIB).date().isoformat()
    news_key = repo.ticker_news_key(ticker)
    try:
        from app.data import premarket
        lean = round(premarket.global_brief().get("score", 0) or 0)
    except Exception:  # noqa: BLE001
        lean = 0
    try:
        from app.data import idxflow
        fp = idxflow.foreign_pressure(ticker)
        fp_bucket = round(fp * 5) if fp is not None else None   # bucket ~0.2
    except Exception:  # noqa: BLE001
        fp_bucket = None
    return (day, news_key, lean, fp_bucket)


def get_thesis(ticker: str, quote: dict) -> str | None:
    """Tesis lama kalau MASIH valid (input tak berubah), else None (harus analisis ulang)."""
    global _hits
    price = quote.get("price")
    with _LOCK:
        c = _CACHE.get(ticker)
    if not c:
        return None
    if c["sig"] != _signature(ticker, quote):
        return None
    if price and c.get("price") and abs(price / c["price"] - 1) * 100 > PRICE_MOVE_PCT:
        return None
    if (datetime.now(timezone.utc) - c["ts"]).total_seconds() / 3600 > TTL_HOURS:
        return None
    with _LOCK:
        _hits += 1
    return c["view"]


def put_thesis(ticker: str, quote: dict, view: str) -> None:
    global _misses
    entry = {"view": view, "price": quote.get("price"),
             "sig": _signature(ticker, quote), "ts": datetime.now(timezone.utc)}
    with _LOCK:
        _CACHE[ticker] = entry
        _misses += 1


def stats() -> dict:
    with _LOCK:
        total = _hits + _misses
        return {"reused": _hits, "fresh": _misses, "cached_tickers": len(_CACHE),
                "reuse_rate": round(_hits / total * 100, 1) if total else 0.0}


def clear() -> None:
    global _hits, _misses
    with _LOCK:
        _CACHE.clear()
        _hits = _misses = 0
