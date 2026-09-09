"""Fraksi harga IDX: harga saham hanya ada di kelipatan tick, bukan di sembarang desimal.

Target seperti 102,73 tidak pernah bisa tersentuh: di harga 102 satu tick = Rp1, jadi harga
berikutnya adalah 103. Target yang jatuh di antara tick membuat bracket penutupan menilai
sesuatu yang mustahil terjadi, dan `expected_pct` yang di bawah satu tick adalah klaim yang
lebih kecil dari gerak terkecil yang bisa dilakukan pasar.

Grid di bawah diturunkan dari data harga milik engine sendiri (17.555 penutupan Agustus-
September 2026): FPB harga per pita persis 1 / 2 / 5 / 10 / 25.
"""
from __future__ import annotations

import math

# (batas atas eksklusif, tick). Harga >= batas terakhir memakai tick terakhir.
_GRID = ((200, 1), (500, 2), (2000, 5), (5000, 10))
_TICK_TERATAS = 25


def tick_size(price: float) -> int:
    """Fraksi harga yang berlaku di level harga ini."""
    for batas, t in _GRID:
        if price < batas:
            return t
    return _TICK_TERATAS


def snap(price: float, direction: str = "nearest") -> float:
    """Tarik harga ke grid tick. 'up' membulatkan naik, 'down' membulatkan turun."""
    if price <= 0:
        return 0.0
    t = tick_size(price)
    if direction == "up":
        return float(math.ceil(price / t) * t)
    if direction == "down":
        return float(math.floor(price / t) * t)
    return float(round(price / t) * t)


def target_for(entry: float, expected_pct: float, direction: str) -> tuple[float | None, float]:
    """Target yang BISA tersentuh + expected_pct yang sesuai dengannya.

    Dibulatkan MENJAUH dari entry supaya target tak pernah lebih dekat dari yang diklaim.
    Kembalikan (None, 0.0) kalau gerak yang diklaim lebih kecil dari satu tick: di situ
    tak ada ruang untuk klaim itu, dan memaksakan angka desimal cuma menyamarkan hal itu.
    """
    if not entry or entry <= 0 or direction not in ("UP", "DOWN"):
        return None, 0.0
    kasar = entry * (1 + (expected_pct or 0) / 100.0)
    target = snap(kasar, "up" if direction == "UP" else "down")
    if (direction == "UP" and target <= entry) or (direction == "DOWN" and target >= entry):
        return None, 0.0
    return target, round((target / entry - 1) * 100, 2)


def min_move_pct(price: float) -> float:
    """Gerak terkecil yang mungkin di harga ini, dalam persen. Klaim di bawah ini mustahil."""
    return (tick_size(price) / price * 100) if price and price > 0 else 0.0


def _selftest() -> None:
    assert tick_size(102) == 1 and tick_size(199) == 1
    assert tick_size(200) == 2 and tick_size(499) == 2
    assert tick_size(500) == 5 and tick_size(1995) == 5
    assert tick_size(2000) == 10 and tick_size(4990) == 10
    assert tick_size(5000) == 25 and tick_size(11725) == 25
    assert snap(102.73, "up") == 103 and snap(102.73, "down") == 102
    assert snap(4703.0, "up") == 4710 and snap(4703.0, "down") == 4700

    # Kasus yang memicu perbaikan ini: entry 102, klaim +0,7% -> target lama 102,73 (mustahil).
    t, pct = target_for(102, 0.7, "UP")
    assert t == 103 and pct == 0.98, (t, pct)
    # DOWN memakai expected_pct BERTANDA NEGATIF, sama dengan yang disimpan engine.
    t, pct = target_for(11725, -0.5, "DOWN")
    assert t == 11650 and pct == -0.64, (t, pct)
    # Klaim yang tandanya berlawanan dengan arah = tak ada ruang, jangan dipaksakan.
    assert target_for(102, -0.7, "UP") == (None, 0.0)
    assert target_for(102, 0.7, "DOWN") == (None, 0.0)
    assert target_for(0, 1.0, "UP") == (None, 0.0)
    assert target_for(100, 1.0, "FLAT") == (None, 0.0)

    assert round(min_move_pct(102), 2) == 0.98
    assert round(min_move_pct(5000), 2) == 0.5
    print("tick selftest OK")


if __name__ == "__main__":
    _selftest()
