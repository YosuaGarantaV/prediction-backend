"""Cek triple-barrier labeling (app/label.py) — kasus tangan + native==numpy bila terkompilasi.
Jalankan: python test_triple_barrier.py
"""
import numpy as np

from app import label


def test_known_cases():
    up, dn, h = 0.03, 0.03, 3
    # bar0: +4% di h1 -> target dulu -> 1
    # bar1..: dibentuk supaya deterministik
    closes = np.array([100, 104, 101, 100,   100, 96, 100, 100,   100, 100, 100, 100], float)
    out = label._triple_barrier_numpy(closes, up, dn, h)
    assert out[0] == 1.0, out[0]                 # 100->104 (+4%) sentuh target di h1
    assert out[4] == 0.0, out[4]                 # 100->96 (-4%) sentuh stop di h1
    # bar8: 100,100,100 dalam window -> waktu habis, akhir==entry -> 0 (tidak > entry)
    assert out[8] == 0.0, out[8]
    # ekor (i >= n-h) = nan
    assert np.isnan(out[-1]) and np.isnan(out[-2]) and np.isnan(out[-3])


def test_time_barrier_up():
    # naik tipis <up sepanjang window, tak pernah sentuh barrier -> waktu habis, akhir>entry -> 1
    closes = np.array([100, 100.5, 101, 101.5, 102], float)
    out = label._triple_barrier_numpy(closes, 0.03, 0.03, 4)
    assert out[0] == 1.0, out[0]


def test_native_matches_numpy():
    if not label.HAS_NATIVE:
        print("  native belum dikompilasi -> SKIP (numpy dipakai). "
              "Kompilasi: python native/build_tb.py")
        return
    rng = np.random.default_rng(3)
    closes = 1000 * np.cumprod(1 + rng.normal(0, 0.02, 5000))
    a = label._triple_barrier_numpy(closes, 0.03, 0.03, 5)
    b = label._triple_barrier_native(np.ascontiguousarray(closes), 0.03, 0.03, 5)
    both = ~(np.isnan(a) | np.isnan(b))
    assert np.array_equal(np.isnan(a), np.isnan(b)), "pola nan beda"
    assert np.array_equal(a[both], b[both]), "label native != numpy"
    print("  native == numpy pada 5000 bar: OK.")


if __name__ == "__main__":
    test_known_cases()
    test_time_barrier_up()
    test_native_matches_numpy()
    print(f"OK test_triple_barrier (HAS_NATIVE={label.HAS_NATIVE}).")
