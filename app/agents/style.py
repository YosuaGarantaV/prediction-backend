"""Klasifikasi GAYA prediksi per saham — OTOMATIS, deterministik, 0 token.

Filosofi (permintaan user 2026-07-13): pasar Indonesia relatif TAK TERDUGA → default pendek
(scalp/swing). Hanya saham yang TERBUKTI stabil & berkualitas yang naik ke 'invest' dengan
horizon lebih panjang (maks 10 hari bursa). Semua otomatis — tak perlu set manual di .env.

Sinyal keterdugaan (dari fitur teknikal + fundamental + rekam jejak):
  - volatilitas 20-hari RENDAH  → lebih terduga (bisa horizon panjang)
  - tren NAIK yang mulus (di atas SMA20 + momentum positif) → arah stabil
  - fundamental berkualitas (ROE tinggi / dividen / valuasi wajar) → layak hold
  - rekam jejak prediksi di saham ini bagus (win-rate historis)
"""
from __future__ import annotations

import config

# Ambang (tunable). vol = stdev return harian 20-hari dalam %.
VOL_SCALP = 4.5   # >= ini = sangat fluktuatif → scalping harian (1 hari)
VOL_INVEST = 2.5  # <= ini + syarat lain = cukup tenang untuk horizon panjang


def classify_style(feats: dict, fund: dict | None = None, track: dict | None = None) -> str:
    """Kembalikan 'scalp' | 'swing' | 'invest' dari data saham. Aman saat data minim."""
    vol = feats.get("volatility_20d")
    if vol is None or vol == 0.0:
        return "scalp"                 # data volatilitas kurang → pendek & hati-hati
    above = bool(feats.get("above_sma20"))
    mom = feats.get("momentum_10d") or 0.0
    if vol >= VOL_SCALP:
        return "scalp"                 # sangat fluktuatif = tak terduga → 1 hari

    fund = fund or {}
    roe, per, div = fund.get("roe"), fund.get("per"), fund.get("div_yield")
    quality = bool((roe and roe >= 10) or (div and div >= 3) or (per and 0 < per < 15))

    track_ok = False
    if track:
        by = track.get("by_direction", {}) or {}
        tot = sum(v.get("n", 0) for v in by.values())
        win = sum(v.get("win", 0) for v in by.values())
        track_ok = tot >= 8 and (win / tot) >= 0.55

    steady = above and mom > 0 and vol <= VOL_INVEST
    if steady and (quality or track_ok):
        return "invest"                # tenang + tren naik + berkualitas/terbukti → boleh panjang
    return "swing"                     # default menengah


def style_and_horizon(feats: dict, fund: dict | None = None, track: dict | None = None) -> tuple[str, int]:
    """(gaya, horizon hari-bursa). Horizon dari config.FOCUS_STYLES (scalp 1 / swing 5 / invest 10)."""
    s = classify_style(feats, fund, track)
    return s, config.FOCUS_STYLES.get(s, 5)


if __name__ == "__main__":
    # fluktuatif → scalp; tenang+tren+quality → invest; sisanya swing
    assert classify_style({"volatility_20d": 5.0, "above_sma20": True, "momentum_10d": 3}) == "scalp"
    assert classify_style({"volatility_20d": 2.0, "above_sma20": True, "momentum_10d": 3},
                          {"roe": 18}) == "invest"
    assert classify_style({"volatility_20d": 3.2, "above_sma20": True, "momentum_10d": 1}) == "swing"
    assert classify_style({"volatility_20d": 0.0}) == "scalp"
    print("style classifier ok")
