"""Audit look-ahead / leakage pipeline prediksi (dipilih user 2026-07-21).
Jalankan: python test_leakage.py

Dua pemeriksaan yang benar-benar bisa gagal kalau leakage muncul:
  1. INVARIAN LOOK-AHEAD: fitur di bar-k WAJIB tak berubah saat bar masa depan ditambahkan.
     compute_features hanya boleh membaca data <= bar-k (tail/iloc negatif). Kalau ada fitur
     yang bergeser setelah data masa depan disambung → look-ahead → dibuang.
  2. KONSISTENSI POPULASI train vs backtest: model dilatih di repo.liquid_tickers (audit
     2026-07-20) tapi backtest berjalan di repo.quote_tickers. Kalau irisannya kecil, metrik
     training (oos_acc 67%) TAK berlaku untuk populasi backtest (win 43%) — sumber jurang nyata
     antara klaim model & realisasi. Cek ini melaporkan overlap; <60% = WARNING (bukan error).
"""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd

from app.data import stocks


def _synth(n: int, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 1000 * np.cumprod(1 + rng.normal(0, 0.02, n))
    vol = rng.integers(1_000, 5_000_000, n).astype(float)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": vol})


def test_no_lookahead():
    df = _synth(300)
    for k in (30, 60, 120, 250):
        now = stocks.compute_features(df.iloc[:k])
        # sambung 40 bar masa depan, lalu hitung ULANG fitur di bar-k yang sama (iloc[:k]).
        # Kalau ada fitur yang membaca bar >k, nilainya akan bergeser -> ketahuan.
        full = stocks.compute_features(pd.concat([df.iloc[:k], df.iloc[k:k + 40]],
                                                 ignore_index=True).iloc[:k])
        for key in now:
            a, b = now[key], full[key]
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                assert abs(a - b) < 1e-6, f"look-ahead di '{key}' @k={k}: {a} != {b}"
            else:
                assert a == b, f"look-ahead di '{key}' @k={k}: {a} != {b}"
    print("  [1] look-ahead: AMAN (fitur bar-k invarian thd data masa depan).")


def test_train_backtest_population():
    """Best-effort: butuh DB. Kosong/absen → SKIP (bukan gagal)."""
    try:
        from app import repo
        train_u = set(repo.liquid_tickers())
        bt_u = set(repo.quote_tickers())
    except Exception as e:  # noqa: BLE001
        print(f"  [2] populasi: SKIP (DB tak tersedia: {str(e)[:50]})")
        return
    if not train_u or not bt_u:
        print(f"  [2] populasi: SKIP (train={len(train_u)}, backtest={len(bt_u)} — DB kosong).")
        return
    overlap = len(train_u & bt_u) / len(bt_u) * 100
    msg = (f"  [2] populasi: train(liquid)={len(train_u)} backtest(quote)={len(bt_u)} "
           f"overlap={overlap:.0f}%")
    if overlap < 60:
        print(msg + "  <-- WARNING: metrik training tak transfer ke backtest (mismatch populasi).")
    else:
        print(msg + "  OK.")


if __name__ == "__main__":
    test_no_lookahead()
    test_train_backtest_population()
    print("OK test_leakage.")
