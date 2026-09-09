"""Triple-barrier labeling (López de Prado; konsep diambil dari HKUDS/Vibe-Trading).

Label lama model.py = tanda return di t+HORIZON — BUTA JALUR: trade yang menyentuh +3%
lalu balik turun dihitung 'kalah' padahal trader nyata sudah keluar di target. Triple-barrier
meniru EXIT nyata: mana yang tersentuh LEBIH DULU dalam max_h bar — target (+up), stop (-dn),
atau waktu habis. Ini biasanya sumber perbaikan edge terbesar (bukan menambah fitur/sumber data).

Hot path = loop (bar x horizon) lintas ratusan ribu sampel → jalur C++ OPSIONAL di
native/tb_label.cpp (dikompilasi via native/build_tb.py). Kalau modul C++ tak terkompilasi
(mis. tanpa MSVC Build Tools), otomatis JATUH ke numpy — hasil IDENTIK, hanya lebih lambat.
Tak ada dependency baru; C++ murni akselerasi opsional.
"""
from __future__ import annotations

import numpy as np

try:                                             # jalur cepat C++ (opsional)
    from native import _tb_native                # noqa: F401
    _LIB, _FFI = _tb_native.lib, _tb_native.ffi
    HAS_NATIVE = True
except Exception:                                # noqa: BLE001 — belum dikompilasi → numpy
    _LIB = _FFI = None
    HAS_NATIVE = False


def _triple_barrier_numpy(closes: np.ndarray, up: float, dn: float, max_h: int) -> np.ndarray:
    """Referensi numpy (selalu benar). Label per bar: 1=target dulu, 0=stop/waktu-turun,
    nan=tak terdefinisi (ekor < max_h bar, atau harga entry <= 0)."""
    n = len(closes)
    out = np.full(n, np.nan)
    for i in range(n - max_h):
        entry = closes[i]
        if entry <= 0:
            continue
        lab = None
        for h in range(1, max_h + 1):
            r = closes[i + h] / entry - 1.0
            if r >= up:
                lab = 1.0
                break
            if r <= -dn:
                lab = 0.0
                break
        if lab is None:                          # waktu habis → tanda return akhir (barrier waktu)
            lab = 1.0 if closes[i + max_h] > entry else 0.0
        out[i] = lab
    return out


def _triple_barrier_native(closes: np.ndarray, up: float, dn: float, max_h: int) -> np.ndarray:
    n = len(closes)
    out = np.empty(n, dtype=np.float64)
    cin = _FFI.cast("double*", closes.ctypes.data)
    cout = _FFI.cast("double*", out.ctypes.data)
    _LIB.triple_barrier_c(cin, n, float(up), float(dn), int(max_h), cout)
    out[out <= -1.5] = np.nan                    # sentinel -2 (C++) → nan (samakan dgn numpy)
    return out


def triple_barrier(closes, up: float = 0.03, dn: float = 0.03, max_h: int = 5) -> np.ndarray:
    """Label triple-barrier. Pakai C++ bila terkompilasi, else numpy (hasil identik)."""
    closes = np.ascontiguousarray(np.asarray(closes, dtype=np.float64))
    if HAS_NATIVE:
        return _triple_barrier_native(closes, up, dn, max_h)
    return _triple_barrier_numpy(closes, up, dn, max_h)
