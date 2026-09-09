"""Konstituen MSCI Indonesia Index + bobot — DIKELOLA SCRIPT (data publik MSCI).

Saham di indeks MSCI = magnet utama dana asing (reksa dana/ETF global pasif & aktif
mengacu ke indeks ini). Saat rebalancing MSCI (efektif akhir Feb/Mei/Agu/Nov), bobot
berubah → arus dana asing besar masuk/keluar konstituen. Bobot perkiraan per Q1-2026
(sumber: factsheet MSCI Indonesia). Perbarui saat review berikutnya.
"""
from __future__ import annotations

# ticker -> bobot indeks (%) perkiraan
MSCI_INDONESIA: dict[str, float] = {
    "BMRI": 22.3, "BBCA": 22.1, "BBRI": 14.4, "ASII": 8.0, "TLKM": 7.2,
    "AMMN": 4.5, "BBNI": 4.4, "DSSA": 4.2, "UNTR": 2.9, "GOTO": 2.5,
    "BREN": 2.0, "AADI": 1.6, "ICBP": 1.6, "KLBF": 1.4, "MDKA": 1.3,
    "CPIN": 1.1, "ADMR": 1.0, "INDF": 1.0, "ANTM": 0.9, "MEDC": 0.8,
    "TPIA": 0.8, "BRPT": 0.7, "PGAS": 0.6, "SMGR": 0.5, "INKP": 0.5,
}


def is_msci(ticker: str) -> bool:
    return ticker.upper() in MSCI_INDONESIA


def weight(ticker: str) -> float:
    return MSCI_INDONESIA.get(ticker.upper(), 0.0)


def msci_note(ticker: str) -> str:
    """Catatan untuk prompt agen."""
    w = weight(ticker)
    if w <= 0:
        return "Bukan konstituen MSCI Indonesia (sensitivitas arus asing pasif rendah)."
    return (f"KONSTITUEN MSCI Indonesia (bobot ~{w}%). Magnet dana asing; sensitif "
            f"rebalancing MSCI (Feb/Mei/Agu/Nov).")
