"""Cek filter & parse disclosure IDX (python test_disclosure.py) — tanpa jaringan."""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from app.data import disclosure


def test_impactful():
    # filing penggerak harga → True
    assert disclosure._is_impactful("Keterbukaan Informasi terkait Aksi Korporasi - Dividen Tunai")
    assert disclosure._is_impactful("Penjelasan atas Volatilitas Transaksi")   # UMA
    assert disclosure._is_impactful("Rencana Penggabungan Usaha")               # merger
    assert disclosure._is_impactful("Keterbukaan Informasi Rencana Transaksi Material")
    assert disclosure._is_impactful("LAPORAN KEPEMILIKAN ATAU SETIAP PERUBAHAN KEPEMILIKAN "
                                    "SAHAM PERUSAHAAN TERBUKA")                 # pemegang >5%
    # noise administratif → False
    # "uma" pernah cocok sebagai substring di dalam "PengUMAn" → 9 baris ETF masuk sbg berita.
    assert not disclosure._is_impactful("Pengumuman Bursa Pencatatan Tambahan ETF")
    assert not disclosure._is_impactful("Laporan Bulanan Registrasi Pemegang Efek")
    assert not disclosure._is_impactful("Penyampaian Bukti Iklan Hasil RUPS")
    assert not disclosure._is_impactful("Laporan Harian atas Nilai Aktiva Bersih")
    assert not disclosure._is_impactful("Laporan Hasil Public Expose - Tahunan")
    print("impactful filter ok")


def test_to_utc():
    # WIB 19:00 → UTC 12:00
    assert disclosure._to_utc_iso("2026-06-26T19:15:00").startswith("2026-06-26T12:15")
    # parse gagal → tak crash (fallback ke now, string ISO)
    assert "T" in disclosure._to_utc_iso("garbage")
    print("to_utc ok")


if __name__ == "__main__":
    test_impactful()
    test_to_utc()
    print("ALL OK")
