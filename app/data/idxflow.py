"""Arus asing RESMI per-saham dari IDX Trading Summary (data otoritatif, sama dgn Stockbit/RTI).

Situs IDX dilindungi Cloudflare "Just a moment" → requests biasa kena 403. TEMBUS pakai
curl_cffi impersonate Chrome (meniru TLS fingerprint browser asli). curl_cffi sudah terpasang
(dependency yfinance). Data END-OF-DAY (per hari bursa) → refresh sesudah tutup. Net asing kuat
mem-prediksi arah lanjutan (momentum dana asing). Disimpan ke data/foreign_flow.json (ponytail:
file JSON, hindari migrasi skema — konsisten dgn dead_tickers/upcoming_ipos).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import config
from app import repo

FOREIGN_FILE = config.DATA_DIR / "foreign_flow.json"   # sesi TERAKHIR (dibaca semua kode)
FOREIGN_DIR = config.DATA_DIR / "foreign_flow"          # arsip per tanggal (bahan backtest)
_URL = "https://www.idx.co.id/primary/TradingSummary/GetStockSummary?length=9999&start=0&date={date}"
_H = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.idx.co.id/id/data-pasar/ringkasan-perdagangan/ringkasan-saham/",
    "Accept-Language": "id,en;q=0.9",
}
WIB = timezone(timedelta(hours=7))


def _fetch(date_str: str) -> list[dict]:
    """Ambil ringkasan 1 hari bursa (YYYYMMDD). [] kalau gagal/kosong/diblok.
    curl_cffi dijalankan di SUBPROCESS terisolasi → crash native-nya tak matikan engine."""
    from app.data.cffi_fetch import safe_get  # impersonate Chrome (subprocess) → lolos Cloudflare
    # config.IDX_PROXY di-set → egress lewat IP residensial/scraping-API (VPS mandiri, tanpa push).
    body = safe_get(_URL.format(date=date_str), _H, timeout=25, proxy=config.IDX_PROXY or None)
    if not body or "Just a moment" in body:
        return []
    try:
        return json.loads(body).get("data", []) or []
    except Exception:  # noqa: BLE001
        return []


def fetch_foreign() -> dict:
    """Cari hari bursa TERAKHIR yg ada datanya (mundur maks 6 hari: lewati weekend/libur/pra-tutup).
    Return {code: {net_val, net_vol, fbuy, fsell, close, date}}."""
    now = datetime.now(WIB)
    for back in range(6):
        d = now - timedelta(days=back)
        if d.weekday() >= 5:  # Sabtu/Minggu: tak ada sesi
            continue
        rows = _fetch(d.strftime("%Y%m%d"))
        if not rows:
            continue
        out: dict[str, dict] = {}
        for r in rows:
            code = r.get("StockCode")
            close = float(r.get("Close") or 0)
            fbuy = float(r.get("ForeignBuy") or 0)   # VOLUME (lembar)
            fsell = float(r.get("ForeignSell") or 0)
            if not code:
                continue
            net_vol = fbuy - fsell
            out[code] = {
                "net_val": round(net_vol * close),   # ≈ Rupiah (vol × harga tutup)
                "net_vol": net_vol, "fbuy": fbuy, "fsell": fsell,
                "close": close, "date": d.strftime("%Y-%m-%d"),
                # BUKU ORDER penutupan (payload sama, 0 request ekstra): antrean beli vs
                # jual tersisa = tekanan demand/supply ke sesi berikutnya (microstructure).
                "bidv": float(r.get("BidVolume") or 0),
                "offerv": float(r.get("OfferVolume") or 0),
                "freq": float(r.get("Frequency") or 0),      # jumlah transaksi (aktivitas)
                "value": float(r.get("Value") or 0),          # nilai diperdagangkan (Rp)
            }
        if out:
            return out
    return {}


def refresh_foreign() -> int:
    """Tarik & simpan arus asing resmi per-saham. Log ringkasan pasar + top in/out-flow."""
    data = fetch_foreign()
    if not data:
        repo.log("engine", "fetch", "arus asing IDX: gagal/kosong (Cloudflare?)", level="warn")
        return 0
    FOREIGN_FILE.write_text(json.dumps(data), encoding="utf-8")
    date = next(iter(data.values()))["date"]
    _archive(data, date)
    total = sum(v["net_val"] for v in data.values())
    ranked = sorted(data.items(), key=lambda kv: kv[1]["net_val"], reverse=True)
    inflow = ", ".join(f"{k}+{v['net_val']/1e9:.0f}M" for k, v in ranked[:3] if v["net_val"] > 0)
    outflow = ", ".join(f"{k}{v['net_val']/1e9:.0f}M" for k, v in ranked[-3:] if v["net_val"] < 0)
    repo.log("engine", "fetch", f"arus asing IDX RESMI {date}: net pasar Rp{total/1e9:+.0f}M, "
             f"{len(data)} saham. Top beli: {inflow or '-'} | Top jual: {outflow or '-'}")
    return len(data)


def _archive(data: dict, date: str) -> None:
    """Simpan salinan per TANGGAL. FOREIGN_FILE hanya menyimpan sesi TERAKHIR dan ditimpa tiap
    refresh, jadi selama ini arus asing tak punya riwayat sama sekali — sinyal non-teknikal yang
    paling menjanjikan di sistem ini TAK PERNAH bisa di-backtest, selamanya (audit 2026-08-31).
    Sekali tulis per tanggal, ~180 KB/hari; tak menimpa file yang sudah ada. Kegagalan arsip
    tak boleh menjatuhkan refresh."""
    try:
        FOREIGN_DIR.mkdir(parents=True, exist_ok=True)
        f = FOREIGN_DIR / f"{date}.json"
        if not f.exists():
            f.write_text(json.dumps(data), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def archived_dates() -> list[str]:
    """Tanggal yang sudah terarsip (urut). Dipakai evaluator saat memvonis sinyal asing."""
    try:
        return sorted(p.stem for p in FOREIGN_DIR.glob("*.json"))
    except Exception:  # noqa: BLE001
        return []


def foreign_on(date: str) -> dict:
    """Snapshot arus asing pada TANGGAL tertentu ({} kalau belum terarsip)."""
    try:
        return json.loads((FOREIGN_DIR / f"{date}.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _load() -> dict:
    try:
        return json.loads(FOREIGN_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def foreign_for(ticker: str) -> dict | None:
    """Arus asing resmi 1 saham (None kalau belum ada)."""
    return _load().get(ticker)


def foreign_pressure(ticker: str) -> float | None:
    """Tekanan asing ternormalisasi [-1..+1] = (beli-jual)/(beli+jual). Bebas ukuran saham.
    < 0 = net jual, > 0 = net beli. None kalau tak ada data / tak ada transaksi asing."""
    f = foreign_for(ticker)
    if not f:
        return None
    tot = (f.get("fbuy") or 0) + (f.get("fsell") or 0)
    if tot <= 0:
        return None
    return round(((f["fbuy"] - f["fsell"]) / tot), 3)


def book_imbalance(ticker: str) -> float | None:
    """Ketimpangan BUKU ORDER penutupan [-1..+1] = (bidVol−offerVol)/(bidVol+offerVol).
    > 0 = antrean BELI lebih tebal (demand overhang → dorongan naik sesi berikutnya);
    < 0 = antrean JUAL lebih tebal (supply overhang). Data EOD resmi IDX — sinyal overnight,
    bukan intraday live. None bila belum ada data / buku kosong."""
    f = foreign_for(ticker)
    if not f:
        return None
    b, o = f.get("bidv") or 0.0, f.get("offerv") or 0.0
    if b + o <= 0:
        return None
    return round((b - o) / (b + o), 3)


def foreign_brief(ticker: str) -> str:
    """Baris untuk prompt analis: net asing nyata + buku order saham ini (hari bursa terakhir)."""
    f = foreign_for(ticker)
    if not f:
        return "(data arus asing IDX belum tersedia)"
    arah = "NET BELI asing" if f["net_val"] > 0 else ("NET JUAL asing" if f["net_val"] < 0 else "netral")
    line = (f"{arah} Rp{f['net_val']/1e9:+.1f} M pada {f['date']} (resmi IDX) — "
            f"beli {f['fbuy']/1e6:.1f}jt vs jual {f['fsell']/1e6:.1f}jt lembar")
    imb = book_imbalance(ticker)
    if imb is not None:
        sisi = "antrean BELI lebih tebal" if imb > 0 else "antrean JUAL lebih tebal"
        line += (f". Buku order tutup: bid {f['bidv']/1e6:.1f}jt vs offer {f['offerv']/1e6:.1f}jt "
                 f"lembar (imbalance {imb:+.2f}, {sisi}; {f.get('freq', 0):.0f} transaksi)")
    return line


if __name__ == "__main__":  # cek cepat
    n = refresh_foreign()
    print("tersimpan:", n, "saham")
    for t in ("BBCA", "ASII", "AMMN"):
        print(t, "->", foreign_brief(t))
