"""Cek kalibrasi isotonic model.py (PAV) — sintetik, tanpa DB. Jalankan: python test_isotonic.py

Gagal kalau: (a) isotonic tak menurunkan Brier pada P overconfident, (b) kurva tak monoton,
(c) fallback model-lama (tanpa calib_x) tak identik dengan P mentah.
"""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
import numpy as np

from app import model


def test_pav_monotonic():
    y = np.array([0, 1, 0, 0, 1, 1, 1, 0, 1, 1], float)
    out = model._pav(y)
    assert np.all(np.diff(out) >= -1e-9), out  # hasil PAV wajib non-turun


def test_isotonic_lowers_brier():
    rng = np.random.default_rng(0)
    n = 4000
    true_p = rng.uniform(0, 1, n)
    y = (rng.uniform(0, 1, n) < true_p).astype(float)
    # skor MENTAH overconfident: dorong ke ekstrem (0.5 +- lebih lebar) → miskalibrasi khas sigmoid.
    raw = np.clip(0.5 + (true_p - 0.5) * 1.8, 0, 1)
    cx, cy = model._fit_isotonic(raw, y)
    cal = np.interp(raw, cx, cy)
    b_raw = float(((raw - y) ** 2).mean())
    b_cal = float(((cal - y) ** 2).mean())
    assert b_cal <= b_raw + 1e-9, (b_raw, b_cal)          # kalibrasi tak boleh memperburuk
    assert b_cal < b_raw, (b_raw, b_cal)                  # pada data overconfident, harus MEMBAIK
    assert np.all(np.diff(cy) >= -1e-9), cy               # breakpoint monoton


def test_backward_compat():
    # model.json lama tanpa calib_x → _apply_calib mengembalikan raw apa adanya.
    assert model._apply_calib(0.73, {}) == 0.73
    assert model._apply_calib(0.73, {"calib_x": [], "calib_y": []}) == 0.73


def test_model_without_oos_edge_is_silenced():
    """Model tanpa daya pisah OOS TIDAK boleh menyetir keputusan. Audit 2026-08-13: model.json
    live ber-AUC 0,456 + kalibrasi isotonic yang runtuh (60 dari 64 breakpoint bernilai sama)
    memberi P identik 0,49298 untuk 673 dari 703 saham → arah model 'DOWN' untuk semuanya →
    semua sinyal UP dipaksa FLAT dan keyakinan DOWN terpaku 50,0."""
    n = len(model.FEATURE_NAMES)
    buruk = {"oos_auc": 0.456, "mu": [0.0] * n, "sd": [1.0] * n, "w": [0.01] * n, "b": 0.0}
    bagus = {**buruk, "oos_auc": 0.62}
    assert not model.is_usable(buruk)
    assert model.is_usable(bagus)
    assert model.is_usable({})            # model lama tanpa metrik → perilaku lama, jangan dibungkam

    model._cache.clear()
    model._cache["m"] = buruk
    model.repo.log = lambda *a, **k: None
    assert model.predict_proba({"rsi14": 20}) is None, "model tanpa skill harus diam"
    model._cache.clear()
    model._cache["m"] = bagus
    p = model.predict_proba({"rsi14": 20})
    assert p is not None and 0.0 <= p <= 1.0, p
    model._cache.clear()


if __name__ == "__main__":
    test_pav_monotonic()
    test_isotonic_lowers_brier()
    test_backward_compat()
    test_model_without_oos_edge_is_silenced()
    print("OK test_isotonic: PAV monoton, isotonic menurunkan Brier, fallback aman, "
          "model tanpa edge OOS dibungkam.")
