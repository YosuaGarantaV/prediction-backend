"""Kalender katalis pasar ke depan (forward-looking) — dihitung oleh SCRIPT.

Memberi agen konteks "apa yang MUNGKIN terjadi": rebalancing MSCI, rapat The Fed
(FOMC), rapat BI (RDG), rilis data AS (CPI, NFP), window dressing akhir kuartal,
musim laporan keuangan. Tanggal berulang dihitung; FOMC pakai jadwal perkiraan.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

# Jadwal FOMC (perkiraan; verifikasi di federalreserve.gov). Hari kedua = pengumuman.
FOMC_2026 = [date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
             date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9)]


def _first_friday(y: int, m: int) -> date:
    d = date(y, m, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7)  # 4 = Jumat


def _nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    d = date(y, m, 1)
    first = d + timedelta(days=(weekday - d.weekday()) % 7)
    return first + timedelta(weeks=n - 1)


def _last_business_day(y: int, m: int) -> date:
    last = date(y, m, calendar.monthrange(y, m)[1])
    while last.weekday() >= 5:
        last -= timedelta(days=1)
    return last


def _months_ahead(today: date, n: int):
    for k in range(n + 1):
        m = today.month - 1 + k
        yield today.year + m // 12, m % 12 + 1


def upcoming_events(days: int = 30, today: date | None = None) -> list[dict]:
    """Daftar katalis dalam `days` hari ke depan, urut tanggal."""
    today = today or date.today()
    horizon = today + timedelta(days=days)
    ev: list[dict] = []

    def add(d: date, name: str, impact: str, note: str):
        if today <= d <= horizon:
            ev.append({"date": d.isoformat(), "days_ahead": (d - today).days,
                       "name": name, "impact": impact, "note": note})

    # FOMC (Fed)
    for d in FOMC_2026:
        add(d, "FOMC The Fed", "tinggi",
            "Keputusan suku bunga AS. Hawkish→tekanan ke IHSG/rupiah; dovish→positif.")

    for y, m in _months_ahead(today, days // 28 + 1):
        add(_first_friday(y, m), "US Non-Farm Payrolls", "tinggi",
            "Data tenaga kerja AS. Kuat→USD naik, tekanan emerging market.")
        add(date(y, m, 13), "US CPI (perkiraan)", "tinggi",
            "Inflasi AS. Panas→ekspektasi rate hike→risk-off.")
        add(_nth_weekday(y, m, 2, 3), "BI Rapat Dewan Gubernur (perkiraan)", "tinggi",
            "Suku bunga BI. Cut→positif bank/properti; hold/naik→hati-hati.")
        # MSCI semi-annual review effective (Feb/May/Aug/Nov, akhir bulan)
        if m in (2, 5, 8, 11):
            add(_last_business_day(y, m), "MSCI Rebalancing efektif", "tinggi",
                "Bobot indeks MSCI berubah → arus dana asing besar masuk/keluar big-cap.")
        # Window dressing akhir kuartal
        if m in (3, 6, 9, 12):
            add(_last_business_day(y, m), "Akhir kuartal (window dressing)", "sedang",
                "Big-cap LQ45 cenderung dijaga/naik menjelang akhir periode.")
        # Musim laporan keuangan
        if m in (1, 4, 7, 10):
            add(date(y, m, 20), "Musim laporan keuangan", "sedang",
                "Rilis kinerja emiten → volatilitas tinggi per saham.")

    ev.sort(key=lambda x: x["date"])
    return ev


def events_brief(days: int = 21) -> str:
    """Ringkasan teks untuk diinject ke prompt agen."""
    ev = upcoming_events(days)
    if not ev:
        return "(tidak ada katalis terjadwal dalam waktu dekat)"
    return "\n".join(f"- H-{e['days_ahead']} {e['date']} {e['name']} "
                     f"[{e['impact']}]: {e['note']}" for e in ev[:10])
