"""Keterbukaan Informasi IDX = sumber PRIMER tercepat (filing emiten langsung ke bursa,
terbit SEBELUM media menulisnya). Dilindungi Cloudflare → tembus via curl_cffi impersonate
(sama trik idxflow). Hanya filing BERDAMPAK HARGA yang dialirkan ke pipeline berita (UMA/
volatilitas, dividen, aksi korporasi, akuisisi, suspensi, obligasi-gagal, dll); noise rutin
(NAV harian ETF, bukti iklan, laporan tahunan) dibuang."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

import config
from app import repo
from app.data import news as news_mod

_URL = ("https://www.idx.co.id/primary/ListedCompany/GetAnnouncement"
        "?indexFrom=1&pageSize={n}&dateFrom=&dateTo=&lang=id&keyword=")
_H = {"Accept": "application/json",
      "Referer": "https://www.idx.co.id/id/perusahaan-tercatat/keterbukaan-informasi/",
      "Accept-Language": "id,en;q=0.9"}
WIB = timezone(timedelta(hours=7))

# Filing penggerak harga — hanya ini yang jadi "berita" (sisanya administratif/rutin).
HIGH_VALUE = (
    "volatilitas", "uma", "dividen", "aksi korporasi", "akuisisi", "merger", "penggabungan",
    "pengambilalihan", "stock split", "pemecahan saham", "saham bonus", "rights", "hmetd",
    "tender", "pembelian kembali", "buyback", "penambahan modal", "private placement",
    "pailit", "pkpu", "suspensi", "pencabutan", "delisting", "restrukturisasi", "gagal bayar",
    "wanprestasi", "obligasi", "sukuk", "waran", "ipo", "penawaran umum", "material",
    "perubahan pengendali", "pengendalian", "divestasi", "spin off", "spin-off",
    # Audit 2026-07-11: TOWR "Penandatanganan Perubahan Perjanjian Fasilitas" (pendanaan
    # material, filing Jumat 22:59 = penggerak Senin) LOLOS filter lama. Penandatanganan/
    # kontrak = aksi material yang lazim menggerakkan harga.
    "penandatanganan", "kontrak",
    # Audit 2026-07-15: pengumuman surveilans BEI (HSC/konsentrasi kepemilikan, notasi khusus,
    # papan pemantauan khusus) SEBELUMNYA terbuang → engine buta ke rilis resmi DCII/BYAN/MORA/
    # FILM dsb. Float kecil + flag regulator = volatilitas/manipulasi tinggi → penggerak nyata.
    "konsentrasi", "hsc", "high shareholding", "notasi khusus", "pemantauan khusus",
    "pemantauan", "efek bersifat ekuitas",
    # Laporan perubahan kepemilikan pemegang >5%, pengendali, dan afiliasi. Tidak cocok
    # dengan "Laporan Bulanan Registrasi Pemegang Efek" yang rutin.
    "kepemilikan",
)


# Dicocokkan sebagai kata utuh: "uma" sebagai substring ikut cocok dengan "pengumuman",
# sehingga pengumuman bursa rutin lolos sebagai UMA.
_WORD_ONLY = ("uma", "hsc")


def _is_impactful(title: str) -> bool:
    t = title.lower()
    return (any(k in t for k in HIGH_VALUE if k not in _WORD_ONLY)
            or any(re.search(rf"\b{k}\b", t) for k in _WORD_ONLY))


def fetch_disclosures(n: int = 40) -> list[dict]:
    """Tarik n pengumuman terbaru (curl_cffi tembus Cloudflare). [] kalau gagal.
    curl_cffi di SUBPROCESS terisolasi → crash native-nya tak matikan engine."""
    from app.data.cffi_fetch import safe_get
    # config.IDX_PROXY di-set → egress lewat IP residensial/scraping-API (VPS bisa fetch sendiri).
    body = safe_get(_URL.format(n=n), _H, timeout=20, proxy=config.IDX_PROXY or None)
    if not body or "Just a moment" in body:
        return []
    try:
        replies = json.loads(body).get("Replies") or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in replies:
        p = it.get("pengumuman") or {}
        title = (p.get("JudulPengumuman") or "").strip()
        if not title:
            continue
        att = it.get("attachments") or []
        url = (att[0].get("FullSavePath") if att and att[0].get("FullSavePath")
               else f"idx-disc-{p.get('Id') or p.get('FinalId') or title[:30]}")
        out.append({"code": (p.get("Kode_Emiten") or "").strip(),
                    "title": title, "tgl": p.get("TglPengumuman") or "", "url": url})
    return out


def _to_utc_iso(tgl: str) -> str:
    """TglPengumuman = WIB naif → UTC iso. Fallback ke sekarang kalau parse gagal."""
    try:
        dt = datetime.fromisoformat(tgl).replace(tzinfo=WIB)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return repo.now_iso()


# Cloudflare memblok IP datacenter (VPS) → jangan hammer + spam log tiap siklus. Setelah gagal,
# mundur _BACKOFF_S sebelum coba lagi (reset saat sukses). Fix penuh = IDX_PROXY / push residensial.
_BACKOFF_S = 1800
_next_try = 0.0
_last_ok = 0.0
# IDX menerbitkan 90-210 filing per hari bursa, jadi jendela 40 cukup untuk siklus 60 detik
# tapi bisa terlewati sesudah backoff 30 menit. Habis jeda: tarik lebar sekali, lalu hemat lagi.
_WIDE_N = 250


def refresh_disclosures() -> int:
    """Konversi filing berdampak → item berita (scope ticker, emiten ter-tag) → pipeline analis."""
    global _next_try, _last_ok
    if time.time() < _next_try:
        return 0                                  # masih dalam backoff → lewati diam-diam (tak spam)
    recs = fetch_disclosures(40 if time.time() - _last_ok < 300 else _WIDE_N)
    if not recs:
        _next_try = time.time() + _BACKOFF_S      # blok datacenter → mundur 30 mnt, berhenti spam
        repo.log("engine", "fetch", "disclosure IDX: gagal (Cloudflare, backoff 30mnt)", level="warn")
        return 0
    _next_try = 0.0                               # sukses → reset backoff
    _last_ok = time.time()
    saved = 0
    for r in recs:
        if not _is_impactful(r["title"]):
            continue
        code = r["code"]
        sentiment, impact = news_mod._sentiment(r["title"])
        # filing aksi korporasi resmi = sinyal nyata → minimal impact 1 (jangan 0/diabaikan)
        if impact == 0:
            impact, sentiment = 1, "neutral"
        item = {
            "ts": _to_utc_iso(r["tgl"]),
            "scope": "ticker" if code else "local",
            "source": "IDX Keterbukaan Informasi",
            "title": f"[Filing IDX] {r['title']}" + (f" ({code})" if code else ""),
            "url": r["url"], "summary": "",
            "tickers": [code] if code else [],
            "sentiment": sentiment, "impact": impact,
        }
        if repo.save_news(item):
            saved += 1
    repo.log("engine", "fetch", f"disclosure IDX: {saved} filing berdampak (dari {len(recs)} terbaru)")
    return saved


if __name__ == "__main__":  # cek cepat
    for r in fetch_disclosures(15):
        mark = "IMPACT" if _is_impactful(r["title"]) else "  skip"
        print(f"  {mark} [{r['code'] or '-':6}] {r['title'][:60]}")
