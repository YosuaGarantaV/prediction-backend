"""Sumber IPO OTORITATIF: scrape e-IPO (platform IPO resmi BEI) — sumber yang sama
dipakai broker/Stockbit. Memberi kode+nama+status TERSTRUKTUR, jauh lebih akurat &
lengkap daripada memungut dari berita. Aman gagal (fallback ke panen berita).
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone

import requests

from app import repo
from app.data import news

URL = "https://e-ipo.co.id/id/ipo/index"
_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept-Language": "id-ID,id;q=0.9"}
# Status yang artinya penawaran sudah kelar → boleh dicoba masuk universe (tradeable).
# Sisanya (book building / waiting / offering) = belum bisa beli → highlight saja.
_TRADEABLE = ("closed", "listing", "pencatatan", "allotment", "penjatahan")


def _parse(page: str) -> list[dict]:
    """Ekstrak (code, company, status) dari HTML e-IPO. Fungsi MURNI (mudah dites)."""
    h3s = [(m.start(), re.sub(r"<[^>]+>", "", m.group(1)).strip())
           for m in re.finditer(r"<h3>(.*?)</h3>", page, re.S)]
    out: list[dict] = []
    for m in re.finditer(r'<h5 class="nobottommargin">\s*(PT[^<]+?)\s*<br\s*/?>\(([A-Z]{4})\)', page):
        company = _html.unescape(m.group(1)).strip()
        status = next((s for pos, s in reversed(h3s) if pos < m.start()), "")
        out.append({"code": m.group(2), "company": company, "status": status})
    return out


def fetch_ipos() -> list[dict]:
    """Ambil daftar IPO resmi dari e-IPO (browser UA → tembus, beda dgn bot biasa)."""
    try:
        page = requests.get(URL, headers=_H, timeout=15).text
    except Exception:  # noqa: BLE001
        return []
    return _parse(page)


def refresh_ipos() -> int:
    """Tarik e-IPO → highlight SEMUA + daftarkan yang penawarannya sudah kelar ke universe."""
    recs = fetch_ipos()
    if not recs:
        repo.log("engine", "fetch", "e-IPO: tak ada data (dilewati, panen berita tetap jalan)",
                 level="warn")
        return 0
    from app import universe
    tradeable = {r["code"] for r in recs if any(t in r["status"].lower() for t in _TRADEABLE)}
    if tradeable:
        universe.add_discovered(tradeable)
    news.add_official_ipos(recs)
    repo.log("engine", "fetch",
             f"e-IPO resmi: {len(recs)} IPO ({', '.join(r['code'] for r in recs)})")
    return len(recs)
