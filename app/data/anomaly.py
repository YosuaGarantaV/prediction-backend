"""Detektor anomali harga/volume — informasi TERCEPAT yang gratis: harga bergerak
SEBELUM berita terbit (institusi/insider trade duluan, media menyusul menit-jam kemudian).

Teknik "Bloomberg tanpa Bloomberg": bandingkan snapshot quotes sekarang vs ~N menit lalu
(quotes sudah di-refresh 2-5 mnt oleh scheduler → nol API tambahan). Gerak >= MOVE_PCT
dalam jendela + turnover berarti = EVENT, disuntik ke tabel news sebagai sumber
'anomali-pasar' → otomatis mengalir ke SEMUA pipa yang ada: ranking scanner
(news_sentiment_map), prompt analis (news_for_ticker), dan narasi prediksi. Konsumen baru: nol.

ponytail: snapshot 1 file JSON; ambang statis MOVE_PCT/MIN_TURNOVER — kalibrasi ulang bila
terlalu berisik (lihat log 'anomali terdeteksi' per hari).
"""
from __future__ import annotations

import json
import time

import config
from app import repo

_SNAP = config.DATA_DIR / "anomaly_snap.json"

MOVE_PCT = 2.0        # gerak minimal dalam jendela antar-snapshot (menit) = luar biasa
MIN_TURNOVER = 1e9    # perputaran minimal dlm jendela (Rp1 M) — saring noise illikuid
MAX_WINDOW_S = 45 * 60  # snapshot lebih tua dari ini = basi (engine sempat mati), skip
DEDUP_HOURS = 2       # 1 anomali per ticker per 2 jam (tren panjang bukan 10 event)


def _load_snap() -> dict:
    try:
        return json.loads(_SNAP.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def scan() -> int:
    """Deteksi & catat anomali. Return jumlah event baru. Aman dipanggil kapan pun
    (di luar jam pasar harga beku → delta 0 → tak ada event)."""
    from app.data import stocks
    if stocks.market_phase() != "open":
        return 0
    now = time.time()
    old = _load_snap()
    quotes = repo.all_quotes()
    snap: dict[str, dict] = {}
    events = 0
    for q in quotes:
        t, p, v = q["ticker"], q.get("price") or 0.0, q.get("volume") or 0.0
        if p <= 0:
            continue
        snap[t] = {"p": p, "v": v, "ts": now}
        o = old.get(t)
        if not o or not o.get("p") or now - o["ts"] > MAX_WINDOW_S:
            continue
        move = (p / o["p"] - 1) * 100
        turn = max(0.0, v - (o.get("v") or 0.0)) * p   # volume yfinance = kumulatif harian
        if abs(move) < MOVE_PCT or turn < MIN_TURNOVER:
            continue
        mins = int((now - o["ts"]) / 60)
        if _recent_anomaly(t):
            continue
        arah = "MELONJAK" if move > 0 else "ANJLOK"
        repo.save_news({
            "scope": "local", "source": "anomali-pasar",
            # url unik per ticker+jam → UNIQUE constraint = dedup lapis kedua
            "url": f"anomaly://{t}/{int(now // 3600)}",
            "title": f"ANOMALI PASAR: {t} {arah} {move:+.1f}% dalam {mins} mnt "
                     f"(turnover Rp{turn / 1e9:.1f} M) — harga bergerak sebelum berita",
            "summary": "Deteksi pergerakan tak biasa dari data harga live; cek katalis "
                       "(filing/berita/aksi korporasi) yang mungkin belum terbit.",
            "tickers": [t],
            # NETRAL/0 — BUKAN katalis (forensik 2026-07-17): deteksi ini SINYAL RISIKO
            # (pump illikuid pra-berita), tapi impact +2 dulu membuatnya LOLOS pilar
            # "berita spesifik" evidence-gate & escape trend-gate → bukti SIRKULER
            # (harga gerak → "berita" → gate lolos → BUY kejar pump). Kasus JELI: 6x BUY
            # averaging-down @995→930, -Rp1.09jt = 92% drawdown akun. Alert tetap tampil
            # utk manusia & memicu buru-katalis di bawah; hanya tak lagi menyaru katalis.
            "sentiment": "neutral",
            "impact": 0,
        })
        events += 1
        repo.log("engine", "fetch", f"anomali terdeteksi: {t} {move:+.1f}%/{mins}mnt "
                 f"turnover Rp{turn / 1e9:.1f}M", ticker=t)
        # LOOP INTEL TERTUTUP: harga sudah bergerak → BURU katalisnya SEKARANG (OSINT
        # ter-target Google when:48h + Yahoo per-ticker; throttle internal 45 mnt) —
        # jangan tunggu refresh RSS umum 8 menit. Analis siklus berikut membaca hasilnya.
        try:
            from app.data import news as _news
            _news.fetch_ticker_news(t)
        except Exception:  # noqa: BLE001
            pass
    try:
        _SNAP.write_text(json.dumps(snap), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return events


def _recent_anomaly(ticker: str) -> bool:
    try:
        row = repo.get_conn().execute(
            "SELECT 1 FROM news WHERE source='anomali-pasar' AND tickers=? "
            "AND ts >= datetime('now', ?) LIMIT 1", (ticker, f"-{DEDUP_HOURS} hours"),
        ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":  # cek cepat manual
    print("anomali baru:", scan())
