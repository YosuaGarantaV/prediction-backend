"""Kalender hari bursa IDX: lewati akhir pekan DAN libur nasional/cuti bersama.

Dipakai menghitung TANGGAL KADALUARSA prediksi dalam HARI BURSA (bukan kalender). Contoh:
prediksi 1-hari dibuat Jumat → jatuh tempo Senin (Sabtu/Minggu tak dihitung); kalau Senin
libur → mundur ke Selasa.

Sumber libur 2026: pengumuman resmi BEI Peng-00171/BEI.POP/09-2025 (SKB 3 Menteri). Bisa
berubah bila pemerintah merevisi — perbarui `IDX_HOLIDAYS` saat kalender tahun baru terbit.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

# Tanggal bursa TUTUP (di luar Sabtu/Minggu). Format ISO 'YYYY-MM-DD'.
IDX_HOLIDAYS: set[str] = {
    # 2026 (resmi BEI)
    "2026-01-01",  # Tahun Baru Masehi
    "2026-01-16",  # Isra Mikraj
    "2026-02-16", "2026-02-17",  # Imlek + cuti bersama
    "2026-03-18", "2026-03-19",  # Nyepi + cuti bersama
    "2026-03-20", "2026-03-23", "2026-03-24",  # rangkaian Idul Fitri 1447H
    "2026-04-03",  # Wafat Isa Almasih
    "2026-05-01",  # Hari Buruh
    "2026-05-14", "2026-05-15",  # Kenaikan Isa Almasih + cuti bersama
    "2026-05-27", "2026-05-28",  # Idul Adha 1447H + cuti bersama
    "2026-06-01",  # Hari Lahir Pancasila
    "2026-06-16",  # Tahun Baru Islam 1448H
    "2026-08-17",  # Kemerdekaan RI
    "2026-08-25",  # Maulid Nabi
    "2026-12-24", "2026-12-25",  # cuti bersama + Natal
    "2026-12-31",  # libur akhir tahun bursa
}


def is_trading_day(d: date) -> bool:
    """Hari bursa? = Senin-Jumat DAN bukan libur terdaftar."""
    return d.weekday() < 5 and d.isoformat() not in IDX_HOLIDAYS


def next_trading_day(d: date) -> date:
    """Hari bursa PERTAMA setelah `d` (skip weekend + libur)."""
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def add_trading_days(start: date, n: int) -> date:
    """Tanggal `n` HARI BURSA setelah `start` (n>=1). Lewati weekend + libur nasional.
    n<=0 → kembalikan hari bursa >= start (atau start bila sudah hari bursa)."""
    if n <= 0:
        d = start
        while not is_trading_day(d):
            d = next_trading_day(d)
        return d
    d = start
    for _ in range(n):
        d = next_trading_day(d)
    return d


WIB = timezone(timedelta(hours=7))
# IDX tutup ~16:00 WIB — sebelum jam ini harga hari berjalan belum final.
CLOSE_WIB = time(16, 0)


def last_closed_session(now: datetime) -> date:
    """Tanggal sesi bursa TERAKHIR yang sudah TUTUP pada `now` (tz-aware → WIB).
    Sabtu/Minggu/libur → hari bursa terakhir sebelumnya; hari bursa sebelum 16:00 → kemarin."""
    n = now.astimezone(WIB)
    d = n.date()
    if is_trading_day(d) and n.time() >= CLOSE_WIB:
        return d
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def has_new_session(made: datetime, now: datetime) -> bool:
    """Sudah ada sesi bursa yang TUTUP setelah prediksi dibuat?

    Kunci kebenaran evaluasi: kalau belum, harga acuan masih ANGKA YANG SAMA dengan entry
    (akhir pekan, libur, atau prediksi dibuat sesudah closing). Menilai menang/kalah di titik
    itu menghasilkan 0.0% palsu — FLAT selalu 'benar', UP/DOWN selalu 'meleset' — lalu masuk
    ke lessons dan meracuni prompt siklus berikutnya. Ganti tanggal kalender ≠ harga baru."""
    return last_closed_session(now) > last_closed_session(made)


def trading_days_between(a: date, b: date) -> int:
    """Jumlah hari bursa yang berlalu dari `a` (eksklusif) sampai `b` (inklusif). Untuk resolve."""
    if b <= a:
        return 0
    n, cur = 0, a
    while cur < b:
        cur += timedelta(days=1)
        if is_trading_day(cur):
            n += 1
    return n


if __name__ == "__main__":  # cek cepat: Jumat 2026-07-10 + 1 hari bursa = Senin 13
    from datetime import date as _d
    assert add_trading_days(_d(2026, 7, 10), 1) == _d(2026, 7, 13), add_trading_days(_d(2026, 7, 10), 1)
    # Kamis sebelum Jumat Agung 2026-04-03: +1 hari bursa lompati Jumat libur + weekend → Senin 6
    assert add_trading_days(_d(2026, 4, 2), 1) == _d(2026, 4, 6), add_trading_days(_d(2026, 4, 2), 1)

    # has_new_session — kasus nyata bug 0.0% (ANTM id 189, dibuat Jumat 18:26 WIB sesudah
    # closing lalu 'dinilai' Sabtu pagi saat harga masih persis harga Jumat).
    def _w(y, m, d, hh, mm=0):
        return datetime(y, m, d, hh, mm, tzinfo=WIB)
    jumat_sore = _w(2026, 7, 17, 18, 26)          # dibuat SESUDAH closing Jumat
    assert not has_new_session(jumat_sore, _w(2026, 7, 18, 9))    # Sabtu: harga beku
    assert not has_new_session(jumat_sore, _w(2026, 7, 19, 13))   # Minggu: harga beku
    assert not has_new_session(jumat_sore, _w(2026, 7, 20, 10))   # Senin pagi: sesi belum tutup
    assert has_new_session(jumat_sore, _w(2026, 7, 20, 16, 30))   # Senin sesudah closing: sah
    # Entry intraday Jumat → closing Jumat SUDAH informasi baru, boleh dinilai akhir pekan.
    assert has_new_session(_w(2026, 7, 17, 10), _w(2026, 7, 18, 9))
    print("market_calendar ok")
