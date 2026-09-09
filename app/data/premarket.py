"""Briefing global semalam → 'lean' arah pembukaan IHSG.

IHSG saat OPEN sangat mengikuti Wall Street semalam + Asia pagi + USD/IDR + VIX (risk on/off).
Modul ini MERINGKAS sinyal makro yang SUDAH ditarik (stocks.refresh_macro) jadi satu arah +
alasan, supaya analis tak perlu menafsir angka mentah. Gratis (yfinance), dihitung on-demand.
ponytail: heuristik bobot tetap; kalibrasi via knowledge.py bila perlu.
"""
from __future__ import annotations

from app import repo

# (nama_makro, bobot, arah): arah +1 = kalau NAIK itu BULLISH utk IHSG, -1 = NAIK itu BEARISH
# (mis. USD/IDR naik = rupiah melemah = asing risk-off). Bobot = kekuatan menggerakkan pembukaan.
_DRIVERS = [
    ("S&P 500", 2.0, +1),
    ("Nasdaq", 1.5, +1),
    ("Nikkei 225", 1.0, +1),        # Asia pagi (sesi sama dgn IHSG)
    ("USD/IDR", 1.5, -1),           # rupiah melemah → tekanan jual asing di IDX
    ("Dollar Index (DXY)", 1.0, -1),
    ("VIX (Fear)", 1.5, -1),        # fear global naik → risk-off
    ("US 10Y Yield", 0.5, -1),      # yield AS naik → tekan emerging market
]


def global_brief() -> dict:
    """{'lean': str, 'score': float, 'text': str} — arah & penggerak pembukaan."""
    rows = {r["name"]: (r["change_pct"] or 0.0) for r in repo.all_macro()}
    score = 0.0
    drivers = []
    for name, w, sign in _DRIVERS:
        chg = rows.get(name)
        if chg is None:
            continue
        # Clamp per-driver biar 1 angka ekstrem (mis. VIX +20%) tak mendominasi skor.
        contrib = max(-3.0, min(3.0, w * sign * chg))
        score += contrib
        if abs(chg) >= 0.1:
            drivers.append((abs(contrib), f"{name} {chg:+.2f}%"))
    drivers.sort(reverse=True)
    top = ", ".join(d for _, d in drivers[:5]) or "data makro belum lengkap"
    if score >= 1.5:
        lean = "RISK-ON (cenderung gap-up)"
    elif score <= -1.5:
        lean = "RISK-OFF (cenderung gap-down)"
    else:
        lean = "NETRAL / campuran"
    return {"lean": lean, "score": round(score, 2), "text": top}


def brief_line() -> str:
    b = global_brief()
    return f"{b['lean']} (skor {b['score']:+.1f}) — penggerak utama: {b['text']}"


if __name__ == "__main__":  # cek cepat
    print(brief_line())
