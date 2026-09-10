"""Web server FastAPI: dashboard + JSON API."""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse)
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

import config
from app import auth, backtest, db, events, guest, report, repo
from app.agents import orchestrator, skill
from app.data import news, stocks

app = FastAPI(title="Saham IDX Prediction Engine")
WEB_DIR = Path(__file__).resolve().parent / "web"

# Ticker dari path URL mengalir ke DB (LIKE pattern) dan request eksternal yfinance
# ("<t>.JK") — batasi ke bentuk kode emiten yang sah, tolak sisanya lebih awal.
_TICKER_RE = re.compile(r"^[A-Z0-9]{2,8}$")


def _ticker_or_400(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if not _TICKER_RE.fullmatch(t):
        raise HTTPException(status_code=400, detail="ticker tidak valid")
    return t


# Guard GUEST: role terbatas. Didaftarkan PALING AWAL (decorator pertama) → jadi lapisan
# TERDALAM: eksekusi Session -> auth (wajib login) -> csrf (origin) -> GUEST -> handler.
# Jadi guest hanya diperiksa setelah login & origin lolos (kuota tak terbakar lewat CSRF),
# dan penyaringan respons membungkus handler paling ketat. Non-guest (admin/belum-login)
# dilewatkan utuh → akses admin TIDAK berubah. Airtight: membungkus SELURUH app termasuk
# mount StaticFiles; tiap /api/* di-deny-by-default kecuali allowlist di app/guest.py.
@app.middleware("http")
async def _guest_guard(request: Request, call_next):
    if request.session.get("role") != "guest":
        return await call_next(request)              # admin / non-guest → tanpa batasan
    p = request.url.path
    blocked = guest.check(request, p)                # docs/skema + /api allowlist (deny-default)
    if blocked is not None:
        return blocked                               # 403 / 429
    resp = await call_next(request)
    return await guest.filter_response(p, resp)      # saring list ke GUEST_TICKERS


@app.middleware("http")
async def _csrf_guard(request: Request, call_next):
    """Tolak request PENGUBAH-STATE lintas-origin. Endpoint POST di sini memicu kerja LLM
    mahal (/api/run*, /api/run-cycle, /api/run-backtest) — tanpa penjaga ini, situs web jahat
    yang kamu buka di browser bisa diam-diam menembak fetch() ke localhost dan membakar kuota
    API-mu. Origin cocok host request (dashboard sendiri) → lolos; tanpa Origin (curl/alat
    non-browser) → lolos; Origin beda (evil.com) → 403. Ringan, tanpa dependency baru.

    Endpoint LOGIN (/login, /guest-login) DIKECUALIKAN: itu pintu masuk auth, bukan aksi mahal.
    Submit form login bisa bawa `Origin: null` (mis. setelah redirect / host-alias) → dulu
    ke-blok 'lintas-origin' padahal sah. CSRF-login pun tak berbahaya di sini (paling jauh bikin
    korban jadi TAMU; admin butuh kredensial), dan /login tetap dijaga lockout brute-force."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") \
            and request.url.path not in ("/login", "/guest-login"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return JSONResponse({"error": "permintaan lintas-origin ditolak"}, status_code=403)
    return await call_next(request)


# Guard AUTH: semua rute wajib sesi login KECUALI /login, /logout, dan asset statis
# (/_next/*, /favicon*). Didaftarkan SETELAH _csrf_guard dan SEBELUM SessionMiddleware
# (add_middleware menyisipkan di depan stack) → urutan eksekusi: Session → auth → csrf.
# Middleware membungkus SELURUH app termasuk mount StaticFiles paling bawah — tak ada bypass.
@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    p = request.url.path
    if p in ("/login", "/logout", "/guest-login") or p.startswith("/_next/") or p.startswith("/favicon"):
        return await call_next(request)
    if request.session.get("auth"):
        return await call_next(request)
    if p.startswith("/api/"):
        return JSONResponse({"error": "belum login"}, status_code=401)
    return RedirectResponse("/login", status_code=303)   # 303 → selalu GET /login


# ponytail: SESSION_SECRET kosong → secret acak per-proses (semua sesi putus saat restart;
# tetap aman, hanya kurang nyaman). Produksi wajib set SESSION_SECRET di .env.
app.add_middleware(SessionMiddleware,
                   secret_key=config.SESSION_SECRET or secrets.token_hex(32),
                   same_site="lax", https_only=config.COOKIE_SECURE,
                   max_age=14 * 24 * 3600)
# https_only=Secure flag: cookie sesi HANYA dikirim lewat HTTPS → tak bisa disadap di link
# http:// yang menipu. Default ON (situs live di balik Caddy TLS); COOKIE_SECURE=0 hanya utk
# dev localhost http (kalau tidak, browser buang cookie & login tak pernah "nempel").


# Header keamanan di SETIAP respons (terluar). Murah, tanpa dependency; menutup vektor umum:
# clickjacking (frame-ancestors none / X-Frame-Options), MIME-sniff (nosniff), kebocoran
# referrer, dan memaksa HTTPS (HSTS). CSP sengaja MINIMAL (frame-ancestors + object/base saja)
# supaya TIDAK mematahkan Next.js statis yang meng-inline script/style — CSP script-src ketat
# akan white-screen dashboard, lebih buruk dari mudarat yang dicegah.
_SEC_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "frame-ancestors 'none'; object-src 'none'; base-uri 'self'",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    for k, v in _SEC_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp
# Kompresi respons (terluar). Utama untuk /api/mirror (JSON ~800KB -> ~100KB di kabel tiap
# ~60 dtk); dashboard ikut untung. Caddy tidak meng-encode (cek /etc/caddy/Caddyfile).
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(auth.router)
# UI utama = build statis Next.js (frontend/out). Belum di-build → fallback HTML legacy
# di app/web (fungsi identik, cuma tampilan lama).
UI_DIST = Path(__file__).resolve().parent.parent / "frontend" / "out"

# Tombol Logout melayang: disuntik ke tiap halaman HTML yang disajikan. Elemen ini SIBLING
# di luar root React (#__next) → tak tersapu hidrasi/navigasi SPA, muncul di semua halaman.
_LOGOUT_BTN = (
    '<a href="/logout" title="Keluar sesi" '
    'style="position:fixed;top:12px;right:14px;z-index:99999;background:#20272a;color:#e4685a;'
    'border:1px solid #3b4649;border-radius:8px;padding:6px 12px;'
    'font:600 13px system-ui,sans-serif;text-decoration:none">Logout</a>'
)


def _html_with_logout(path: Path):
    """Sajikan file HTML dengan tombol Logout disuntik sebelum </body>. Gagal baca → FileResponse."""
    try:
        html = path.read_text(encoding="utf-8")
    except OSError:
        return FileResponse(path)
    if "</body>" in html:
        html = html.replace("</body>", _LOGOUT_BTN + "</body>", 1)
    return HTMLResponse(html)


@app.get("/")
def index():
    dist = UI_DIST / "index.html"
    return _html_with_logout(dist if dist.exists() else WEB_DIR / "dashboard.html")


# Halaman detail. Allowlist eksplisit — jangan FileResponse path bebas.
_PAGES = {"saham", "prediksi", "portofolio", "sistem", "panduan"}


@app.get("/p/{page}")
def page(page: str, request: Request):
    """Rute lama /p/... — build Next ada → redirect ke rute baru /<page>; belum → HTML legacy."""
    if page in _PAGES:
        if (UI_DIST / f"{page}.html").exists():
            q = f"?{request.query_params}" if request.query_params else ""
            return RedirectResponse(f"/{page}{q}")
        return _html_with_logout(WEB_DIR / f"{page}.html")
    return JSONResponse({"error": "halaman tidak ada"}, status_code=404)


@app.get("/api/focus")
def focus():
    """Saham FOKUS (config.FOCUS): gaya + kuote + prediksi terbuka per bucket horizon + posisi."""
    by_h = repo.open_predictions_by_horizon()
    out = []
    for ticker, style in config.FOCUS.items():
        q = repo.get_quote(ticker)
        pos = repo.get_position(ticker)
        h = config.FOCUS_STYLES[style]
        bucket = str(h) if h <= 10 else "10"  # bucket UI maks 10; invest (20h) jatuh ke 10+
        preds = by_h.get(ticker, {})
        out.append({
            "ticker": ticker, "style": style, "horizon": h,
            "price": q["price"] if q else None,
            "change_pct": q["change_pct"] if q else None,
            "prediction": preds.get(bucket) or repo.latest_prediction(ticker),
            "position": pos,
        })
    return out


@app.get("/api/watch-state")
def watch_state():
    """{ticker: pinned} untuk semua saham dipantau (star) — UI menandai bintang & pin."""
    return repo.watchlist_state()


@app.post("/api/watch/{ticker}")
def watch(ticker: str):
    """Toggle STAR: masuk/keluar pantauan → jadi PRIORITAS analisis agen. Return status baru."""
    t = _ticker_or_400(ticker)
    return {"ticker": t, "watched": repo.watch_toggle(t)}


@app.post("/api/pin/{ticker}")
def pin(ticker: str):
    """Toggle PIN: saham dipantau yang di-pin tampil menonjol di dashboard. Return status baru."""
    t = _ticker_or_400(ticker)
    return {"ticker": t, "pinned": repo.pin_toggle(t)}


@app.get("/api/watched")
def watched():
    """Saham pantauan (star) diperkaya: harga, prediksi terbaru, GAYA auto (scalp/swing/invest), pin."""
    by_h = repo.open_predictions_by_horizon()
    out = []
    for ticker, pinned in repo.watchlist_state().items():
        q = repo.get_quote(ticker)
        pred = repo.latest_prediction(ticker)
        style = (pred or {}).get("style") or orchestrator._auto_style(ticker)
        # Rencana beli sederhana (hanya prediksi NAIK yang masih open): harga masuk,
        # cut-loss dari backstop engine, take-profit = target prediksi.
        plan = None
        if pred and pred.get("direction") == "UP" and pred.get("status") == "open":
            entry = pred.get("entry_price") or (q["price"] if q else None)
            if entry:
                plan = {
                    "entry": round(entry, 2),
                    "cut_loss": round(entry * (1 + config.STOP_LOSS_PCT / 100), 2),
                    "take_profit": pred.get("target_price")
                        or round(entry * (1 + config.TAKE_PROFIT_PCT / 100), 2),
                }
        out.append({
            "ticker": ticker, "pinned": pinned, "style": style,
            "price": q["price"] if q else None,
            "change_pct": q["change_pct"] if q else None,
            "prediction": pred,
            "plan": plan,
            "by_horizon": by_h.get(ticker, {}),
        })
    # pinned dulu, lalu urut keyakinan prediksi
    out.sort(key=lambda x: (not x["pinned"],
                            -(x["prediction"]["probability"] if x["prediction"] else 0)))
    return out


@app.get("/api/overview")
def overview():
    pf = repo.latest_portfolio()
    stats = repo.prediction_stats()
    return {
        "portfolio": pf,
        "stats": stats,
        "market_phase": stocks.market_phase(),
        "mode": "LLM" if config.USE_LLM else "Heuristik",
        "macro": repo.all_macro(),
        "flow": repo.latest_flow(),
        "curve": repo.portfolio_curve(120),
        "start_cash": config.START_CASH,
    }


@app.get("/api/watchlist")
def watchlist():
    quotes = repo.all_quotes()
    preds = repo.latest_predictions()
    by_h = repo.open_predictions_by_horizon()
    out = []
    for q in quotes:
        feats = json.loads(q.get("features_json") or "{}")
        p = preds.get(q["ticker"])
        out.append({
            "ticker": q["ticker"],
            "price": q["price"],
            "change_pct": q["change_pct"],
            "rsi": feats.get("rsi14"),
            "consec_down": feats.get("consec_down"),
            "prediction": p,                       # utama (terbaru apa pun horizonnya)
            "by_horizon": by_h.get(q["ticker"], {}),  # {1,3,5,10} untuk toggle
        })
    out.sort(key=lambda x: (x["prediction"]["probability"] if x["prediction"] else 0),
             reverse=True)
    return out


@app.get("/api/predictions")
def predictions(limit: int = 60):
    return repo.recent_predictions(min(max(limit, 1), 500))


@app.get("/api/news")
def news_feed(scope: str | None = None):
    """Feed panel "Berita & Sentimen": lebih banyak item, hanya yang SEGAR.
    Jendela & limit dari config (NEWS_DISPLAY_HOURS/LIMIT) — tampilan saja, jendela yang
    dipakai agen untuk analisis (news_for_ticker / news_sentiment_map / _fresh_shock) TIDAK
    ikut berubah."""
    return repo.recent_news(config.NEWS_DISPLAY_LIMIT, scope,
                            max_age_hours=config.NEWS_DISPLAY_HOURS)


@app.get("/api/logs")
def logs(agent: str | None = None, level: str | None = None, limit: int = 150):
    """Log agen. `agent`/`level` menyaring — tanpa itu satu agen paling berisik bisa mengisi
    seluruh jendela dan panel terlihat 'kosong' dari agen lain."""
    if agent and not re.fullmatch(r"[a-z-]{2,20}", agent):
        raise HTTPException(400, "agent tidak valid")
    if level and level not in ("info", "warn", "error", "debug"):
        raise HTTPException(400, "level tidak valid")
    return repo.recent_logs(limit, agent=agent, level=level)


@app.get("/api/logs/agents")
def logs_agents(hours: int = 24):
    """Daftar agen yang bersuara N jam terakhir + hitungannya (isi dropdown filter)."""
    return repo.log_agents(min(max(hours, 1), 24 * 30))


@app.get("/api/usage")
def usage():
    """Debug token: berapa token & output tiap request LLM + ringkasan 24 jam + memo tesis."""
    from app.agents import memo, llm
    return {"summary": repo.usage_summary(), "recent": repo.recent_usage(60),
            "memo": memo.stats(), "cooldowns": llm.cooldowns()}


@app.get("/api/health")
def health():
    """Kesehatan sistem: kesegaran tiap sumber data + provider LLM yang sedang cooldown +
    IHSG (deteksi data-basi). Panel ini menangkap bug seperti IHSG-berhenti-update lebih awal."""
    from app.agents import memo, llm, budget
    from app.data import price_guard
    fresh = repo.source_freshness()
    ihsg = next((m for m in repo.all_macro() if m.get("symbol") == "^JKSE"), None)
    return {
        "freshness": fresh,
        "any_stale": any(f["stale"] for f in fresh),
        "cooldowns": llm.cooldowns(),
        "memo": memo.stats(),
        "price_guard": price_guard.status(),
        "token_budget": budget.status(),
        "ihsg": {"price": ihsg["price"], "change_pct": ihsg["change_pct"], "ts": ihsg["ts"]} if ihsg else None,
    }


@app.get("/api/positions")
def positions():
    out = []
    for p in repo.get_positions():
        q = repo.get_quote(p["ticker"])
        price = q["price"] if q else p["avg_price"]
        out.append({**p, "price": price,
                    "pl_pct": round((price / p["avg_price"] - 1) * 100, 2),
                    "value": round(p["qty"] * price, 0)})
    return out


@app.get("/api/trades")
def trades():
    return repo.recent_trades(60)


@app.get("/api/lessons")
def lessons():
    return repo.recent_lessons(30)


@app.get("/api/factors")
def factors_eval():
    """Papan skor faktor struktural: tiap faktor bernama + berapa kali fired + win-rate.
    Evaluasi harian 'mana faktor yang bekerja'."""
    return repo.factor_scoreboard()


@app.get("/api/ledger")
def ledger(days: int = 30):
    """Ledger performa per-hari: ekuitas, PnL harian, trade, prediksi, win-rate harian."""
    return repo.daily_ledger(min(max(days, 1), 365))  # clamp: hindari days negatif/absurd


@app.get("/api/alpha")
def alpha():
    """Uji alpha: win-rate mentah vs relatif-pasar (excess vs IHSG) per arah — beta vs skill."""
    return repo.alpha_scoreboard()


@app.get("/api/flow")
def flow_snapshot():
    return repo.latest_flow() or {}


@app.get("/api/ihsg-intraday")
def ihsg_intraday():
    """Bar 1-menit IHSG sesi terakhir + close kemarin — grafik live dashboard."""
    return stocks.ihsg_intraday()


@app.get("/api/premarket")
def premarket_brief():
    from app.data import premarket
    return premarket.global_brief()


@app.get("/api/events")
def events_feed():
    return events.upcoming_events(30)


@app.get("/api/ipos")
def upcoming_ipos():
    """IPO yang akan datang (di-highlight) — dipanen otomatis dari berita."""
    return news.upcoming_ipos()


@app.get("/api/backtest")
def backtest_result():
    return backtest.load_result()


@app.get("/api/skill")
def skill_metrics():
    return skill.agent_skill()


@app.get("/api/backup")
def backup(request: Request):
    """Snapshot KONSISTEN seluruh DB (SQLite backup API — aman walau engine live sedang menulis
    WAL). Dipakai mirror lokal untuk mencadangkan state live (portofolio, posisi, trade, prediksi,
    watchlist, dst). Dua lapis: guard auth (login yosua) + SECRET pairing di header. GET murni-baca.
    """
    # Selain sudah login, WAJIB bawa secret pairing yang cocok — hanya mesin yang di-pair yang
    # boleh menarik seluruh DB. Constant-time. Server tanpa MIRROR_SECRET -> tolak semua (fail-closed).
    sent = request.headers.get("x-mirror-secret", "")
    if not config.MIRROR_SECRET or not hmac.compare_digest(sent, config.MIRROR_SECRET):
        raise HTTPException(status_code=403, detail="pairing secret salah")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    src = sqlite3.connect(str(config.DB_PATH))
    dst = sqlite3.connect(path)
    try:
        with dst:
            src.backup(dst)          # snapshot atomik ke file temp
    finally:
        dst.close()
        src.close()
    # BackgroundTask: hapus file temp SETELAH terkirim penuh.
    return FileResponse(path, filename="prediction-backup.db",
                        media_type="application/octet-stream",
                        background=BackgroundTask(os.unlink, path))


# Kueri mirror inkremental. STATE kecil = kirim penuh (mirror me-replace isi tabel ->
# penghapusan ikut tertangkap: watchlist un-star, posisi tutup). APPEND = jendela terbaru
# (mirror INSERT OR REPLACE by PK -> baris baru + update spt prediksi resolve; sejarah lokal
# utuh). Jendela >> laju baris/menit -> sync 60 dtk tak pernah bolong; batas dipilih agar
# payload tetap kecil (teks conversations/news/agent_logs dominan).
_MIRROR_QUERIES = {
    "quotes": "SELECT * FROM quotes",
    "positions": "SELECT * FROM positions",
    "watchlist": "SELECT * FROM watchlist",
    "macro": "SELECT * FROM macro",
    "flow": "SELECT * FROM flow",
    "portfolio": "SELECT * FROM portfolio ORDER BY ts DESC LIMIT 200",
    "trades": "SELECT * FROM trades ORDER BY id DESC LIMIT 200",
    "predictions": "SELECT * FROM predictions ORDER BY id DESC LIMIT 300",
    "news": "SELECT * FROM news ORDER BY id DESC LIMIT 200",
    "agent_logs": "SELECT * FROM agent_logs ORDER BY id DESC LIMIT 150",
    "conversations": "SELECT * FROM conversations ORDER BY id DESC LIMIT 15",
    "lessons": "SELECT * FROM lessons ORDER BY id DESC LIMIT 100",
}


@app.get("/api/mirror")
def mirror_delta(request: Request):
    """Snapshot JSON KOMPAK data panas untuk sync inkremental mirror (bukan DB penuh 13MB —
    itu /api/backup, dipakai sekali saat start). Gate IDENTIK /api/backup: guard login /api/
    + X-Mirror-Secret constant-time; server tanpa MIRROR_SECRET -> tolak semua (fail-closed).
    GET murni-baca."""
    sent = request.headers.get("x-mirror-secret", "")
    if not config.MIRROR_SECRET or not hmac.compare_digest(sent, config.MIRROR_SECRET):
        raise HTTPException(status_code=403, detail="pairing secret salah")
    conn = db.get_conn()
    out: dict[str, list[dict]] = {}
    for table, sql in _MIRROR_QUERIES.items():
        out[table] = [dict(r) for r in conn.execute(sql)]
    # Bar harga 2 hari terakhir (list harga tetap segar). Cutoff prefix TANGGAL: aman untuk
    # campuran format ts di DB ('...T...' / '... ...' / offset +07:00) karena perbandingan
    # string 'YYYY-MM-DD' <= varian mana pun di tanggal yang sama.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    out["prices"] = [dict(r) for r in
                     conn.execute("SELECT * FROM prices WHERE ts >= ?", (cutoff,))]
    return out


# File data non-DB yang boleh ditarik mirror (universe/IPO/dead-list dsb hidup di data/,
# BUKAN di SQLite → /api/backup & /api/mirror tidak membawanya). Whitelist eksplisit:
# nama di luar daftar tidak akan pernah terkirim. Cap ukuran per file = pagar memori.
_DATAFILE_WHITELIST = ("idx_universe.txt", "discovered_ipos.txt", "discovered_at.json",
                       "upcoming_ipos.json", "dead_tickers.json", "foreign_flow.json",
                       "anomaly_snap.json", "model.json", "backtest.json")
_DATAFILE_MAX_BYTES = 2_000_000


@app.get("/api/datafiles")
def datafiles(request: Request):
    """{nama_file: isi_teks} untuk file data whitelist yang ada. Gate IDENTIK /api/backup:
    guard login /api/ + X-Mirror-Secret constant-time; tanpa MIRROR_SECRET → tolak semua
    (fail-closed). GET murni-baca."""
    sent = request.headers.get("x-mirror-secret", "")
    if not config.MIRROR_SECRET or not hmac.compare_digest(sent, config.MIRROR_SECRET):
        raise HTTPException(status_code=403, detail="pairing secret salah")
    out: dict[str, str] = {}
    for name in _DATAFILE_WHITELIST:
        p = config.DATA_DIR / name
        try:
            if p.is_file() and p.stat().st_size <= _DATAFILE_MAX_BYTES:
                out[name] = p.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001 — 1 file rusak tidak menutup sisanya
            continue
    return out


@app.post("/api/run-backtest")
def run_backtest():
    threading.Thread(target=backtest.run_backtest, daemon=True).start()
    return {"status": "backtest dijalankan di background"}


@app.get("/api/conversations")
def conversations():
    return repo.recent_conversations(25)


@app.get("/api/conversation/{ticker}")
def conversation(ticker: str):
    return repo.latest_conversation(_ticker_or_400(ticker)) or {}


@app.get("/api/news-ticker/{ticker}")
def news_ticker(ticker: str):
    """Berita spesifik emiten (7 hari) — untuk halaman detail saham."""
    return repo.news_for_ticker(_ticker_or_400(ticker), limit=20, max_age_hours=168)


@app.get("/api/history/{ticker}")
def history(ticker: str):
    from app.data import msci
    ticker = _ticker_or_400(ticker)
    return {
        "ticker": ticker,
        "prices": repo.price_history(ticker, 120),
        "prediction": repo.latest_prediction(ticker),
        "predictions": repo.predictions_for_ticker(ticker, 25),  # penanda di chart
        "fundamentals": repo.get_fundamentals_row(ticker),  # cache-read saja
        "msci_weight": msci.weight(ticker),
    }


@app.post("/api/run-cycle")
def run_cycle():
    threading.Thread(target=orchestrator.run_cycle, daemon=True).start()
    return {"status": "siklus agen dijalankan di background"}


@app.post("/api/run/{ticker}")
def run_ticker(ticker: str, horizon: int | None = None):
    """Analisis 1 saham. `horizon` opsional (1/3/5) = agen persempit prediksi ke jendela itu."""
    ticker = _ticker_or_400(ticker)
    if horizon not in (None, 1, 3, 5):
        horizon = None
    threading.Thread(target=orchestrator.run_one, args=(ticker,),
                     kwargs={"horizon": horizon}, daemon=True).start()
    return {"status": f"analisis {ticker} dijalankan"
            + (f" (horizon {horizon} hari)" if horizon else "")}


@app.post("/api/manual-decide/{ticker}")
def manual_decide(ticker: str, body: dict):
    """Keputusan MANUAL (operator eksternal berperan CTO) — lewat pipeline
    risk-gate & eksekusi PERSIS SAMA dgn keputusan LLM (regime/trend/evidence gate, kalibrasi,
    re-gate BUY, fee, MAX_ALLOC, stop-loss) via trader._sanitize + orchestrator._commit; tak
    ada jalur eksekusi terpisah. Body JSON opsional: direction/probability/action/horizon_days/
    expected_pct/size_pct/reasoning/key_factors — default aman via _sanitize bila kosong."""
    from app.agents import trader as _trader
    t = _ticker_or_400(ticker)
    quote = repo.get_quote(t)
    if not quote:
        stocks.fetch_ticker(t)
        quote = repo.get_quote(t)
    if not quote:
        raise HTTPException(status_code=404, detail=f"{t}: tidak ada quote")
    decision = _trader._sanitize(t, body or {}, quote)
    decision["_analyst_view"] = f"[MANUAL — operator eksternal]\n{decision.get('reasoning', '')}"
    pid = orchestrator._commit(decision)
    return {"ticker": t, "prediction_id": pid, "decision": decision}


@app.get("/api/report", response_class=PlainTextResponse)
def daily_report():
    report.save_to_file()
    return report.build_markdown()


# Rute halaman Next: export menghasilkan <page>.html FLAT plus folder <page>/ berisi payload
# RSC (tanpa index.html) → StaticFiles memilih folder lalu 404. Route eksplisit menang atas
# mount, langsung sajikan <page>.html; belum di-build → fallback HTML legacy.
def _ui_page(name: str):
    f = UI_DIST / f"{name}.html"
    return _html_with_logout(f if f.exists() else WEB_DIR / f"{name}.html")


for _p in _PAGES:
    app.add_api_route(f"/{_p}", (lambda p: lambda: _ui_page(p))(_p), methods=["GET"])

# UI Next.js: mount PALING AKHIR — route API/halaman di atas selalu menang; sisanya
# (asset /_next, favicon, dll) dilayani file statis hasil `npm run build` di frontend/out.
if UI_DIST.exists():
    app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")
