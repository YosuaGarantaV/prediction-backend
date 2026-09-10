"""Penjaga gerbang gabungan arus asing + buku order (factors.extra_signals).

Yang dijaga, dan kenapa: arsip 394 hari menunjukkan fp dan buku order LEMAH sendiri-sendiri
(+2,5 / +2,3 pp di h5) tapi kuat berbarengan (+6,2 pp, lulus di 2025 maupun 2026). Kalau
seseorang mengembalikan dua faktor terpisah, bukti yang sama dihitung dua kali dan kasus
gabungan kehilangan nama faktornya di papan skor — kemunduran yang TAK terlihat dari luar.

Jalankan:  python test_combo_gate.py
"""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from app.agents import factors
from app.data import idxflow


def _signals(fp, imb, queue_vol=1e6, price=1000.0):
    """Jalankan extra_signals dgn arus asing & buku order dipalsukan."""
    orig = (idxflow.foreign_pressure, idxflow.book_imbalance, idxflow.foreign_for)
    idxflow.foreign_pressure = lambda t: fp
    idxflow.book_imbalance = lambda t: imb
    idxflow.foreign_for = lambda t: {"bidv": queue_vol, "offerv": queue_vol}
    try:
        sigs = factors.extra_signals("TSTX", {}, price)
    finally:
        (idxflow.foreign_pressure, idxflow.book_imbalance, idxflow.foreign_for) = orig
    return {s["name"]: s for s in sigs}


def test_combined_replaces_the_two_separate_factors():
    s = _signals(fp=0.55, imb=0.7)
    assert "asing_beli_buku_tebal" in s, "gerbang gabungan tak menyala padahal syarat terpenuhi"
    assert "asing_beli" not in s and "book_bid_heavy" not in s, \
        "bukti yang sama dihitung dua kali (faktor lama ikut menyala)"
    assert s["asing_beli_buku_tebal"]["dir"] == 1
    assert s["asing_beli_buku_tebal"]["weight"] > 0.6, "bobot gabungan harus di atas fp sendirian"


def test_single_side_keeps_old_behaviour():
    only_fp = _signals(fp=0.55, imb=0.1)
    assert "asing_beli" in only_fp and "asing_beli_buku_tebal" not in only_fp
    only_book = _signals(fp=0.0, imb=0.7)
    assert "book_bid_heavy" in only_book and "asing_beli_buku_tebal" not in only_book
    sell = _signals(fp=-0.55, imb=-0.7)
    assert "asing_jual" in sell and "book_offer_heavy" in sell, \
        "sisi DOWN belum terbukti digabung — dua faktor lama harus tetap apa adanya"


def test_thin_book_does_not_open_the_gate():
    """Antrean < Rp200jt = derau; ambang ini ikut diuji, jangan dilonggarkan diam-diam."""
    s = _signals(fp=0.55, imb=0.7, queue_vol=1.0, price=1.0)
    assert "asing_beli_buku_tebal" not in s and "book_bid_heavy" not in s
    assert "asing_beli" in s, "arus asing tetap dinilai walau buku tipis"


if __name__ == "__main__":
    from app import db
    db.init_db()
    test_combined_replaces_the_two_separate_factors()
    test_single_side_keeps_old_behaviour()
    test_thin_book_does_not_open_the_gate()
    print("OK: gerbang gabungan menyala, faktor lama tak dobel, buku tipis tetap tertutup")
