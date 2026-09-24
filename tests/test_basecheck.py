"""basecheck.py adalah berkas penghitung Tabel 4.3 (Lampiran 2 dan 5 naskah).

Dua janji naskah dijaga di sini: pemeriksaan mandirinya meloloskan sinyal sempurna dan
menggagalkan sinyal acak, dan perintah yang dicetak di Lampiran 5 menghasilkan tabel
bertata letak sama dengan yang dicetak di sana.
"""
import subprocess
import sys
from pathlib import Path

import basecheck

ROOT = Path(__file__).resolve().parent.parent


def test_self_check_sempurna_lulus_acak_gagal():
    basecheck._self_check()


def test_report_sama_dengan_lampiran_5():
    """Angka baris 30 Agustus Lampiran 5 dimasukkan apa adanya; tata letaknya harus identik."""
    def row(n, h, wg, dg, wt, dt, d, ci):
        return {"n": n, "hari": h, "win": wg, "dasar_cocok": dg, "menang_hari": wt,
                "dasar_cocok_hari": dt, "selisih_cocok": d, "ci95_cocok": ci}
    res = {"n_bets": 204, "n_used": 193, "n_liquid": 240,
           "SEMUA": row(193, 27, 39.4, 36.9, 34.1, 37.5, -3.3, (-16.4, 8.5)),
           "UP": row(73, 18, 46.6, 42.4, 38.5, 46.1, -7.6, (-31.6, 12.5)),
           "DOWN": row(120, 18, 35.0, 33.6, 30.2, 35.3, -5.1, (-20.5, 9.3))}
    assert basecheck.report(res, "2026-08-30") == (
        "batas 30 Agustus: sinyal tuntas 204, terpakai 193, universe likuid 240 kode\n"
        "arah      n hari |  win_g  dsr_g |  win_t  dsr_t |    d_t  CI95 d_t\n"
        "SEMUA   193   27 |   39.4   36.9 |   34.1   37.5 |   -3.3  (-16.4; 8.5)\n"
        "UP       73   18 |   46.6   42.4 |   38.5   46.1 |   -7.6  (-31.6; 12.5)\n"
        "DOWN    120   18 |   35.0   33.6 |   30.2   35.3 |   -5.1  (-20.5; 9.3)")


def test_cli_seperti_perintah_lampiran_5(tmp_path):
    db = tmp_path / "prediction_v4.db"
    basecheck._synthetic_db(str(db), [("T000", f"2026-01-{i:02d}", "win")
                                      for i in range(1, 21)])
    out = subprocess.run(
        [sys.executable, str(ROOT / "basecheck.py"), str(db), "--from", "2026-01-01",
         "--until", "2026-01-20", "--cut", "2026-01-31"],
        cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    assert out[0] == "batas 31 Januari: sinyal tuntas 20, terpakai 20, universe likuid 80 kode"
    assert out[1].startswith("arah      n hari |  win_g  dsr_g |  win_t  dsr_t |")
    assert out[2].startswith("SEMUA    20   20 |  100.0   50.0 |  100.0   50.0 |   50.0")
