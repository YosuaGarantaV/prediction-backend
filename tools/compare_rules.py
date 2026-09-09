"""Bandingkan ATURAN PENILAIAN lama vs baru di atas data yang SAMA (audit 2026-07-27).

Perbaikan akuntansi hanya berlaku untuk prediksi yang BELUM ditutup, jadi papan skor lama
tak bisa dibandingkan begitu saja dengan yang baru. Skrip ini memutar ulang SELURUH baris
`predictions` dengan dua aturan sekaligus, memakai close harian di tabel `prices`:

  LAMA : menang saat harga menyentuh target (jarak apa adanya, median 1,13%),
         kalah baru pada max(jarak, 2%) -> ambang menang lebih dekat; band FLAT +-1,0%;
         papan skor memakai SEMUA baris resolved (79% isinya forecast display 1-hari).
  BARU : satu jarak max(jarak, 2%) untuk kedua sisi; band FLAT +-1,75% (median |gerak|
         1 hari saham terpilih); papan skor hanya taruhan nyata.

Keterbatasan yang disengaja: replay pakai close HARIAN, sedangkan engine live menilai pada
quote intraday. Jadi angka absolut di sini bukan pembukuan resmi -- yang valid adalah SELISIH
antar aturan, karena keduanya diuji pada jalur harga yang persis sama.

Jalankan: python tools/compare_rules.py
"""
from __future__ import annotations

import collections
import datetime
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "prediction.db"

ADVERSE_FLOOR_PCT = 2.0
FLAT_BAND_OLD = 1.0
FLAT_BAND_NEW = 1.75


def load_prices(conn) -> dict[str, list[tuple[str, float]]]:
    px: dict[str, list[tuple[str, float]]] = collections.defaultdict(list)
    for t, ts, close in conn.execute("SELECT ticker, ts, close FROM prices ORDER BY ts"):
        if close:
            px[t].append((ts[:10], float(close)))
    return px


def path_after(px, ticker: str, day: str, n: int) -> list[float]:
    """Close n sesi pertama SESUDAH `day` (kosong bila data harga tak cukup)."""
    rows = px.get(ticker) or []
    return [c for d, c in rows if d > day][:n]


def replay(p: dict, closes: list[float], flat_band: float, symmetric: bool) -> str | None:
    """Vonis satu prediksi: bracket dulu (per sesi), sisanya dinilai di akhir horizon."""
    entry, target, direction = p["entry_price"], p["target_price"], p["direction"]
    if not closes:
        return None
    if direction in ("UP", "DOWN") and entry and target:
        raw = abs(target / entry - 1) * 100
        floor = max(raw, ADVERSE_FLOOR_PCT)
        win_at = floor if symmetric else raw       # <- inti perbedaannya
        for c in closes:
            favor = (c / entry - 1) * 100 * (1 if direction == "UP" else -1)
            if favor >= win_at:
                return "win"
            if favor <= -floor:
                return "loss"
    pct = (closes[-1] / entry - 1) * 100
    if direction == "UP":
        return "win" if pct > 0.5 else "loss"
    if direction == "DOWN":
        return "win" if pct < -0.5 else "loss"
    return "win" if abs(pct) <= flat_band else "loss"


def rate(c: collections.Counter) -> str:
    w, l = c["win"], c["loss"]
    return f"{w:4d}W/{l:4d}L = {w / max(w + l, 1) * 100:5.1f}%  (n={w + l})"


def main() -> int:
    if not DB.exists():
        print(f"DB tak ditemukan: {DB}")
        return 1
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    px = load_prices(conn)
    rows = [dict(r) for r in conn.execute("SELECT * FROM predictions")]

    box = collections.defaultdict(collections.Counter)
    nodata = 0
    for p in rows:
        if not p["entry_price"]:
            continue
        made = datetime.datetime.fromisoformat(p["ts"]) + datetime.timedelta(hours=7)
        closes = path_after(px, p["ticker"], made.date().isoformat(), p["horizon_days"] or 3)
        if not closes:
            nodata += 1
            continue
        display = (p["horizon_days"] == 1
                   and (p["reasoning"] or "").startswith("prediksi 1-hari"))
        old = replay(p, closes, FLAT_BAND_OLD, symmetric=False)
        new = replay(p, closes, FLAT_BAND_NEW, symmetric=True)
        box["LAMA aturan + LAMA populasi"][old] += 1        # semua baris ikut
        box["LAMA aturan + BARU populasi"][old if not display else "skip"] += 1
        box["BARU aturan + LAMA populasi"][new] += 1
        box["BARU aturan + BARU populasi"][new if not display else "skip"] += 1
        box[f"  arah {p['direction']} (baru)"][new if not display else "skip"] += 1
        # Subset yang SEBANDING dgn headline: hanya baris yang benar-benar dinilai engine
        # (superseded & open tak pernah masuk papan skor).
        if p["status"] == "resolved":
            box["   headline LAMA (resolved saja)"][old] += 1
            box["   headline BARU (resolved saja)"][new if not display else "skip"] += 1

    print(f"baris dievaluasi: {len(rows) - nodata} (tanpa data harga: {nodata})\n")
    for k in ("LAMA aturan + LAMA populasi", "LAMA aturan + BARU populasi",
              "BARU aturan + LAMA populasi", "BARU aturan + BARU populasi"):
        print(f"{k:32s} {rate(box[k])}")
    print()
    for k in ("   headline LAMA (resolved saja)", "   headline BARU (resolved saja)"):
        print(f"{k:32s} {rate(box[k])}")
    print()
    for k in sorted(k for k in box if k.startswith("  arah")):
        print(f"{k:32s} {rate(box[k])}")
    print("\nBaca: baris 1 = papan skor lama, baris 4 = papan skor baru. Beda baris 1 vs 3 = "
          "efek aturan bracket; beda baris 3 vs 4 = efek membuang forecast display 1-hari.")
    return 0


def demo() -> None:
    """Cek mandiri: aturan baru simetris, aturan lama tidak."""
    p = {"entry_price": 100.0, "target_price": 101.2, "direction": "UP"}   # target rapat 1,2%
    # +1,2% searah: aturan LAMA langsung menang, aturan BARU belum menutup apa pun
    assert replay(p, [101.2], FLAT_BAND_OLD, symmetric=False) == "win"
    assert replay(p, [101.2], FLAT_BAND_NEW, symmetric=True) == "win"      # <- lewat vonis akhir
    # jalur yang belum sampai akhir horizon: bracket saja yang boleh bicara
    assert replay(p, [101.2, 100.0], FLAT_BAND_NEW, symmetric=True) == "loss"   # akhir -0%<0.5
    assert replay(p, [101.2, 100.0], FLAT_BAND_OLD, symmetric=False) == "win"   # sudah dikunci
    # simetri: +2% dan -2% harus diperlakukan sama besar oleh aturan baru
    assert replay(p, [102.0], FLAT_BAND_NEW, symmetric=True) == "win"
    assert replay(p, [98.0], FLAT_BAND_NEW, symmetric=True) == "loss"
    # FLAT: gerak 1,5% dulu dianggap meleset, kini masih dalam band
    f = {"entry_price": 100.0, "target_price": 100.0, "direction": "FLAT"}
    assert replay(f, [101.5], FLAT_BAND_OLD, symmetric=False) == "loss"
    assert replay(f, [101.5], FLAT_BAND_NEW, symmetric=True) == "win"
    print("demo compare_rules OK")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        raise SystemExit(main())
