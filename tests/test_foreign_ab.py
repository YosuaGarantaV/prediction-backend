"""Cek evaluator A/B arus-asing (item-3 NEXT_UPDATES) AMAN saat data belum cukup:
data sekarang belum punya prediksi resolved ber-foreign_pressure (collector baru), jadi harus
lapor 'insufficient' dan TIDAK menulis artefak (gate tetap HOLD). Bukti, bukan tebakan.

Jalankan:  python test_foreign_ab.py
"""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from app.backtest import AB_FILE, backtest_foreign_ab


def test_insufficient_does_not_write_artifact():
    existed = AB_FILE.exists()
    # Bar mustahil (collector kini sudah > 40 sampel nyata): paksa cabang "insufficient" secara
    # deterministik untuk menguji guard "sampel kurang → JANGAN tulis artefak" (gate item-3 HOLD).
    r = backtest_foreign_ab(min_samples=10_000)
    assert r["status"] == "insufficient", r
    assert r["n"] < r["need"], r
    # artefak TAK boleh muncul/berubah saat data kurang → gate item-3 tetap HOLD
    assert AB_FILE.exists() == existed, "artefak ditulis padahal sampel kurang"


if __name__ == "__main__":
    from app import db
    db.init_db()
    test_insufficient_does_not_write_artifact()
    print("OK: evaluator aman saat data kurang (insufficient, tanpa artefak)")
