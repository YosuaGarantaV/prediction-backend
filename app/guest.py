"""Kebijakan otorisasi role GUEST — TERPUSAT (satu sumber kebenaran, dipanggil oleh
middleware _guest_guard di server.py). Prinsip: ALLOWLIST / deny-by-default. Guest hanya
boleh menyentuh dashboard + segelintir endpoint baca untuk 5 saham (config.GUEST_TICKERS),
tak pernah data emiten lain, endpoint admin, atau job mahal.

Alur (dipanggil hanya bila session role == 'guest', path diawali /api/):
  check(request, path)   -> None (lolos) atau Response (403/429) untuk MEMBLOKIR.
  filter_response(path, resp) -> saring list JSON ke GUEST_TICKERS saja (anti bocor).

Semua perbandingan ticker via _norm(): upper + strip + buang sufiks .JK — SAMA seperti
data layer menormalkan sebelum query — supaya trik case/suffix/spasi tak bisa menembus."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

from starlette.responses import JSONResponse, Response

import config
from app import auth, repo

# --- Rate limit per-IP (anti-DoS satu-sumber / bot flood) -------------------
# Sliding-window di memori proses (sama pola dgn brute-force auth.py): cukup untuk
# single-instance systemd. Bukan anti-DDoS terdistribusi — itu lapis Caddy/CDN.
_rl_lock = threading.Lock()
_rl_hits: dict[str, list[float]] = {}   # ip -> epoch tiap request dalam window


def _rate_limited(ip: str) -> int:
    """Sisa detik tunggu bila IP ini melewati kuota request (0 = boleh lanjut)."""
    now = time.time()
    win = config.GUEST_RATE_WINDOW_S
    with _rl_lock:
        hits = _rl_hits.setdefault(ip, [])
        hits[:] = [t for t in hits if now - t < win]
        if len(hits) >= config.GUEST_RATE_MAX:
            return max(1, int(hits[0] + win - now) + 1)
        hits.append(now)
        return 0

# --- Endpoint yang BOLEH diakses guest ---------------------------------------
# GET eksak (baca, tanpa parameter ticker di path). Semua di luar daftar ini -> 403.
_GET_WHITELIST = {
    # Pasar & prediksi (data publik / performa model — TANPA uang).
    "/api/overview", "/api/watchlist", "/api/watched", "/api/focus",
    "/api/predictions", "/api/flow", "/api/news", "/api/watch-state",
    "/api/ihsg-intraday", "/api/events", "/api/ipos", "/api/premarket",
    # Performa & pembelajaran mesin (win-rate/faktor/pelajaran — bukan saldo).
    "/api/skill", "/api/factors", "/api/alpha", "/api/backtest",
    "/api/lessons", "/api/conversations", "/api/health",
}
# SENGAJA TIDAK diberikan ke guest (isi REKENING, bukan data pasar):
#   /api/positions /api/trades /api/ledger /api/report  → posisi, transaksi, ekuitas harian
#   /api/logs /api/usage /api/backup /api/mirror /api/datafiles → internal/ops
# Kunci DICOPOT dari respons dict yang lolos allowlist (scrub, bukan tolak — halaman tetap
# render, angka rekening hilang). /api/overview membawa saldo & kurva ekuitas akun.
_SCRUB_DICT = {
    "/api/overview": ("portfolio", "curve", "start_cash"),
}
# GET per-ticker: ticker di path WAJIB ada di GUEST_TICKERS (dinormalkan) atau 403.
_PER_TICKER_GET = ("/api/history/", "/api/conversation/", "/api/news-ticker/")
_RUN_PREFIX = "/api/run/"   # POST analis 1 saham (ticker allowlist + kuota harian)

# List JSON yang harus DISARING ke GUEST_TICKERS sebelum dikirim (anti bocor emiten lain).
# Sisanya (overview/flow/history/conversation) = dict/skop-tunggal, tak perlu disaring.
_LIST_EXACT = {"/api/watchlist", "/api/watched", "/api/focus", "/api/predictions",
               "/api/news", "/api/lessons", "/api/conversations"}
_LIST_PREFIX = ("/api/news-ticker/",)

# /api/predictions untuk GUEST = SHOWCASE rekam jejak: hanya prediksi BERARAH yang terbukti
# BENAR (direction UP/DOWN, status resolved outcome=win); FLAT (datar) & yang belum jatuh
# tempo (open) DIBUANG; maksimal _GUEST_PRED_MAX baris terbaru. Sengaja LINTAS SEMUA emiten
# (bukan cuma GUEST_TICKERS): daftar ini murni performa model baca-saja tanpa data rekening,
# jadi memperluas cakupannya aman — sementara SCOPE KEAMANAN guest (history/run/detail per
# emiten) TETAP 5 emiten. Admin (pemilik) tetap melihat semua (menang/kalah/flat/open, jujur).
_GUEST_PRED_SHOWCASE = {"/api/predictions"}
_GUEST_PRED_MAX = 50
_GUEST_PRED_DIRS = ("UP", "DOWN")           # buang FLAT/datar dari showcase

# Skema/dok API otomatis FastAPI (non-/api/). Membocorkan SELURUH daftar endpoint admin
# (backup/mirror/usage/...) ke guest = pengintaian ruang admin → sembunyikan dari guest.
# Admin (pemilik) tetap bisa membukanya. Dashboard tak memakainya, jadi aman diblok.
_DOCS_DENY = {"/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}


def _norm(raw: str) -> str:
    """Normalisasi ticker persis seperti data layer: upper, strip, buang sufiks .JK."""
    t = (raw or "").strip().upper()
    if t.endswith(".JK"):
        t = t[:-3].strip()
    return t


def _guest_set() -> set[str]:
    """Set ticker guest (dibaca dari config tiap panggilan → ikut env & patch test)."""
    return {_norm(t) for t in config.GUEST_TICKERS if str(t).strip()}


def _daily_limit() -> int:
    return int(config.GUEST_DAILY_ANALYST)


# WIB = UTC+7 tetap (tanpa DST) → hitung tanggal & sisa-ke-tengah-malam tanpa dependency tz.
def _market_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=7)


def _today_key() -> str:
    return _market_now().strftime("%Y-%m-%d")


def _secs_to_midnight() -> int:
    now = _market_now()
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((nxt - now).total_seconds()))


def _deny(msg: str = "akses ditolak untuk guest") -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=403)


def _over_quota() -> JSONResponse:
    wait = _secs_to_midnight()
    return JSONResponse(
        {"error": "kuota analis harian guest habis", "retry_after": wait},
        status_code=429, headers={"Retry-After": str(wait)})


def check(request, path: str) -> Response | None:
    """None = lolos (lanjut ke handler); Response = blokir. Dipanggil untuk SETIAP request
    guest. Deny-by-default pada /api/*: apa pun di luar allowlist -> 403."""
    # Rate-limit DULU (sebelum kerja apa pun) — 1 IP membanjiri = 429, tak sempat menyentuh
    # DB/handler. Berlaku utk SEMUA request guest (termasuk halaman & statis) supaya bot tak
    # bisa memutar lewat aset. Manusia normal jauh di bawah ambang.
    ip = auth.client_ip(request)
    wait = _rate_limited(ip)
    if wait:
        return JSONResponse({"error": "terlalu banyak request", "retry_after": wait},
                            status_code=429, headers={"Retry-After": str(wait)})
    if path in _DOCS_DENY:                       # skema/dok API disembunyikan dari guest
        return _deny("skema API tidak tersedia untuk guest")
    if not path.startswith("/api/"):             # dashboard HTML / statis / halaman → boleh
        return None
    method = request.method
    gset = _guest_set()

    # 1) GET per-ticker: history / conversation / news-ticker
    for pre in _PER_TICKER_GET:
        if path.startswith(pre):
            if method != "GET":
                return _deny()
            if _norm(path[len(pre):]) in gset:
                return None
            return _deny("ticker di luar akses guest")

    # 2) POST analis 1 saham + kuota harian (persisten, per hari+IP)
    if path.startswith(_RUN_PREFIX):
        if method != "POST":
            return _deny()
        if _norm(path[len(_RUN_PREFIX):]) not in gset:
            return _deny("ticker di luar akses guest")
        ip = auth.client_ip(request)
        n = repo.guest_quota_incr(_today_key(), ip)
        if n > _daily_limit():
            return _over_quota()
        return None

    # 3) GET whitelist eksak
    if method == "GET" and path in _GET_WHITELIST:
        return None

    # 4) sisanya (usage/logs/report/backup/mirror/datafiles/run-cycle/run-backtest/
    #    watch/pin/health/... dan method non-GET) -> tolak
    return _deny()


def _item_ok(it: dict, gset: set[str]) -> dict | None:
    """Kembalikan item bila boleh dilihat guest (mungkin di-scrub), atau None untuk dibuang."""
    if not isinstance(it, dict):
        return None
    if it.get("ticker") is not None:                    # baris per-emiten (posisi/prediksi/dll)
        return it if _norm(str(it["ticker"])) in gset else None
    if "tickers" in it:                                 # berita: csv ticker terkait
        toks = {_norm(x) for x in str(it.get("tickers") or "").split(",") if x.strip()}
        if not toks:
            return it                                   # berita makro/global tanpa ticker → boleh
        allowed = toks & gset
        if not allowed:
            return None
        it = dict(it)
        it["tickers"] = ",".join(sorted(allowed))       # scrub: sembunyikan ticker non-guest
        return it
    return it                                           # tak ada info ticker → agregat, boleh


def _filter_list(items: list) -> list:
    gset = _guest_set()
    out = []
    for it in items:
        keep = _item_ok(it, gset)
        if keep is not None:
            out.append(keep)
    return out


async def filter_response(path: str, resp: Response) -> Response:
    """Saring body JSON untuk guest: list → batasi ke GUEST_TICKERS; dict → copot kunci
    rekening (_SCRUB_DICT). Endpoint lain dilewatkan apa adanya."""
    if resp.status_code != 200:
        return resp
    scrub = _SCRUB_DICT.get(path)
    if not (scrub or path in _LIST_EXACT or any(path.startswith(p) for p in _LIST_PREFIX)):
        return resp
    body = b""
    async for chunk in resp.body_iterator:
        body += chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode()
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001 — body bukan JSON valid → kirim ulang apa adanya
        return _rebuild(resp, body)
    if isinstance(data, list):
        if path in _GUEST_PRED_SHOWCASE:           # showcase: prediksi BENAR & BERARAH, lintas emiten
            out = [it for it in data if isinstance(it, dict)
                   and it.get("outcome") == "win"
                   and it.get("direction") in _GUEST_PRED_DIRS][:_GUEST_PRED_MAX]
            return JSONResponse(out, status_code=200)
        return JSONResponse(_filter_list(data), status_code=200)
    if scrub and isinstance(data, dict):
        # Copot kunci rekening (saldo/kurva ekuitas/modal awal) — sisanya (fase pasar, makro,
        # breadth, win-rate model) tetap dikirim supaya dashboard tamu tetap informatif.
        return JSONResponse({k: v for k, v in data.items() if k not in scrub}, status_code=200)
    return _rebuild(resp, body)                          # dict tak terduga → jangan ubah


def _rebuild(resp: Response, body: bytes) -> Response:
    """Bungkus ulang body yang sudah dikonsumsi tanpa header panjang/encoding basi."""
    headers = {k: v for k, v in resp.headers.items()
               if k.lower() not in ("content-length", "content-encoding")}
    return Response(content=body, status_code=resp.status_code, headers=headers,
                    media_type=resp.media_type)
