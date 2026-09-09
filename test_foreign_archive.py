"""Penjaga arsip arus asing + evaluatornya (item-3 NEXT_UPDATES).

Dua hal yang kalau rusak membuat seluruh vonis item-3 tak sah, dan keduanya rusak DIAM-DIAM:
  1. arsip per-tanggal terbaca oleh kode produksi (`idxflow.foreign_on`), skema sama;
  2. evaluator memakai entry OPEN t+1. Kembali ke close t = look-ahead (IDX menerbitkan
     Trading Summary sesudah closing) dan melebihkan win-rate UP h1 sekitar 4 pp.

Jalankan:  python test_foreign_archive.py
"""
import pytest

from app.data import idxflow
from tools.eval_foreign_archive import build_rows, load_panel

# Arsip 394 hari bursa dibangun `tools/import_idx_archive.py` dan TIDAK ikut repo (91 MB data
# pasar). Klon baru belum punya isinya, dan test yang gagal di klon bersih terbaca seperti kode
# rusak. Prasyarat yang absen = LEWATI dengan alasan, bukan MERAH.
pytestmark = pytest.mark.skipif(
    len(idxflow.archived_dates()) < 300,
    reason="arsip arus asing < 300 hari — isi dulu: python tools/import_idx_archive.py")


def test_archive_readable_by_production_code():
    dates = idxflow.archived_dates()
    assert len(dates) >= 300, f"arsip terlalu tipis untuk backtest: {len(dates)} hari"
    snap = idxflow.foreign_on(dates[len(dates) // 2])
    assert snap, "foreign_on() tak bisa membaca arsip"
    row = next(iter(snap.values()))
    for k in ("net_val", "net_vol", "fbuy", "fsell", "close", "date", "bidv", "offerv", "value"):
        assert k in row, f"skema arsip menyimpang, kunci '{k}' hilang"


def test_entry_is_next_open_not_today_close():
    """Return sampel harus dihitung dari OPEN t+1, bukan CLOSE t."""
    panel = load_panel()
    ticker = "BBCA" if "BBCA" in panel else next(iter(panel))
    rows = build_rows({ticker: panel[ticker]})
    assert rows, "tak ada sampel terbentuk"
    # layout build_rows: (tanggal, fp, buku, akumulasi20, nilai_antrean, ret20, {h: return})
    assert isinstance(rows[0][6], dict), "layout tuple build_rows berubah — penjaga ini basi"
    ser = panel[ticker]
    td = [d for d, _ in ser]
    by = dict(ser)
    d = rows[0][0]
    i = td.index(d)
    entry = by[td[i + 1]]["open"]
    expected = by[td[i + 1]]["close"] / entry - 1          # h=1: open t+1 -> close t+1
    look_ahead = by[td[i + 1]]["close"] / by[d]["close"] - 1
    assert abs(rows[0][6][1] - expected) < 1e-9, "entry bukan open t+1"
    assert abs(rows[0][6][1] - look_ahead) > 1e-12 or entry == by[d]["close"], \
        "return identik dgn versi look-ahead (entry close t)"


if __name__ == "__main__":
    test_archive_readable_by_production_code()
    test_entry_is_next_open_not_today_close()
    print("OK: arsip terbaca kode produksi + evaluator entry di OPEN t+1 (bukan look-ahead)")
