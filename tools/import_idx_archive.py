"""Isi data/foreign_flow/ dari arsip IDX Trading Summary yang sudah diunduh per tanggal.

Kenapa ada: NEXT_UPDATES item-3 macet sejak 2026-07-17 dengan alasan "arus asing tak
diarsipkan per-tanggal, backtest MUSTAHIL". `idxflow._archive` baru mulai menyimpan
2026-08-31, jadi riwayatnya tetap kosong. Skrip ini mengisi retroaktif dari sumber yang
SAMA (IDX GetStockSummary per hari bursa), skema persis `idxflow.fetch_foreign()` supaya
`idxflow.foreign_on(tanggal)` langsung bisa membacanya, plus 4 kunci OHLC tambahan
(open/high/low/prev) yang dibutuhkan evaluator agar entry bisa dihitung di OPEN t+1.

Sumber boleh .json (mentah GetStockSummary) atau .json.gz berisi baris terpilih.
Tidak menimpa file yang sudah ada.

Jalankan:  python tools/import_idx_archive.py <dir_sumber>
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.data.idxflow import FOREIGN_DIR  # noqa: E402

KEYS = ("StockCode", "Close", "ForeignBuy", "ForeignSell", "BidVolume", "OfferVolume",
        "Frequency", "Value", "OpenPrice", "High", "Low", "Previous")


def convert(rows: list[dict], date: str) -> dict:
    """Baris GetStockSummary -> skema idxflow (net_val dst) + OHLC untuk entry realistis."""
    out: dict[str, dict] = {}
    for r in rows:
        code = r.get("StockCode")
        close = float(r.get("Close") or 0)
        if not code or close <= 0:
            continue
        fb, fs = float(r.get("ForeignBuy") or 0), float(r.get("ForeignSell") or 0)
        out[code] = {
            "net_val": round((fb - fs) * close), "net_vol": fb - fs, "fbuy": fb, "fsell": fs,
            "close": close, "date": date,
            "bidv": float(r.get("BidVolume") or 0), "offerv": float(r.get("OfferVolume") or 0),
            "freq": float(r.get("Frequency") or 0), "value": float(r.get("Value") or 0),
            "open": float(r.get("OpenPrice") or 0), "high": float(r.get("High") or 0),
            "low": float(r.get("Low") or 0), "prev": float(r.get("Previous") or 0),
        }
    return out


def main(src: Path) -> int:
    FOREIGN_DIR.mkdir(parents=True, exist_ok=True)
    written = skipped = empty = 0
    for f in sorted(list(src.glob("*.json")) + list(src.glob("*.json.gz"))):
        date = f.name[:10]
        dst = FOREIGN_DIR / f"{date}.json"
        if dst.exists():
            skipped += 1
            continue
        raw = gzip.decompress(f.read_bytes()) if f.suffix == ".gz" else f.read_bytes()
        payload = json.loads(raw)
        rows = payload.get("data", []) if isinstance(payload, dict) else payload
        data = convert(rows or [], date)
        if not data:
            empty += 1        # hari libur: TIDAK ditulis, biar archived_dates() jujur
            continue
        dst.write_text(json.dumps(data), encoding="utf-8")
        written += 1
    print(f"ditulis {written}, dilewati (sudah ada) {skipped}, kosong/libur {empty} -> {FOREIGN_DIR}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(Path(sys.argv[1])))
