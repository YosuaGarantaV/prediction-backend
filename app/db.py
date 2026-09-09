"""SQLite: koneksi + skema. Menyimpan SEMUA log, harga, berita, prediksi, trade."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

import config

_local = threading.local()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def get_conn() -> sqlite3.Connection:
    """Satu koneksi per-thread (aman dipakai scheduler + web server bersamaan)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


@contextmanager
def tx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


SCHEMA = """
-- Harga historis (OHLCV) per interval
CREATE TABLE IF NOT EXISTS prices (
    ticker   TEXT NOT NULL,
    ts       TEXT NOT NULL,          -- ISO datetime
    open     REAL, high REAL, low REAL, close REAL,
    volume   REAL,
    PRIMARY KEY (ticker, ts)
);

-- Snapshot quote terbaru tiap saham
CREATE TABLE IF NOT EXISTS quotes (
    ticker      TEXT PRIMARY KEY,
    ts          TEXT,
    price       REAL,
    prev_close  REAL,
    change_pct  REAL,
    volume      REAL,
    day_high    REAL,
    day_low     REAL,
    features_json TEXT               -- indikator teknikal terhitung
);

-- Sinyal makro global (minyak, emas, USD/IDR, indeks dunia)
CREATE TABLE IF NOT EXISTS macro (
    name        TEXT PRIMARY KEY,
    symbol      TEXT,
    ts          TEXT,
    price       REAL,
    change_pct  REAL
);

-- Berita lokal + global
CREATE TABLE IF NOT EXISTS news (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT,
    scope       TEXT,                -- local | global
    source      TEXT,
    title       TEXT,
    url         TEXT UNIQUE,
    summary     TEXT,
    tickers     TEXT,                -- csv ticker terkait (hasil tagging)
    sentiment   TEXT,                -- positive | negative | neutral
    impact      INTEGER DEFAULT 0    -- -3..+3 dampak ke pasar
);

-- Prediksi dari agen
CREATE TABLE IF NOT EXISTS predictions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT,
    ticker        TEXT,
    direction     TEXT,              -- UP | DOWN | FLAT
    probability   REAL,              -- 0..100 (% keyakinan arah)
    horizon_days  INTEGER,           -- prediksi untuk berapa hari ke depan
    entry_price   REAL,              -- harga saat prediksi dibuat
    target_price  REAL,
    expected_pct  REAL,              -- ekspektasi % perubahan
    reasoning     TEXT,
    factors_json  TEXT,              -- faktor kunci (teknikal/berita/makro)
    status        TEXT DEFAULT 'open',   -- open | resolved
    resolved_at   TEXT,
    actual_pct    REAL,
    outcome       TEXT               -- win | loss | flat
);

-- Log aktivitas agen (full thinking log)
CREATE TABLE IF NOT EXISTS agent_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT,
    agent     TEXT,                  -- analyst | trader | engine
    phase     TEXT,                  -- fetch | analyze | decide | trade | resolve
    level     TEXT DEFAULT 'info',   -- info | warn | error | debug
    ticker    TEXT,
    message   TEXT,
    payload_json TEXT
);

-- Transaksi paper trading
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT,
    ticker        TEXT,
    side          TEXT,              -- BUY | SELL
    qty           REAL,
    price         REAL,
    gross         REAL,
    fee           REAL,
    net           REAL,
    reason        TEXT,
    prediction_id INTEGER
);

-- Posisi terbuka
CREATE TABLE IF NOT EXISTS positions (
    ticker     TEXT PRIMARY KEY,
    qty        REAL,
    avg_price  REAL,
    opened_at  TEXT
);

-- Snapshot nilai portofolio dari waktu ke waktu
CREATE TABLE IF NOT EXISTS portfolio (
    ts          TEXT PRIMARY KEY,
    cash        REAL,
    equity      REAL,              -- nilai posisi (mark-to-market)
    total       REAL,
    pnl         REAL,
    pnl_pct     REAL
);

-- Fundamental emiten (dari yfinance, di-cache; refresh berkala)
CREATE TABLE IF NOT EXISTS fundamentals (
    ticker         TEXT PRIMARY KEY,
    ts             TEXT,
    per            REAL, forward_pe REAL, pbv REAL, roe REAL,
    div_yield      REAL, profit_margin REAL,
    earnings_growth REAL, revenue_growth REAL, debt_equity REAL, beta REAL,
    rec_key        TEXT, target_price REAL, mcap REAL,
    sector         TEXT, industry TEXT
);

-- Proxy aliran dana asing & breadth pasar (snapshot)
CREATE TABLE IF NOT EXISTS flow (
    ts            TEXT PRIMARY KEY,
    foreign_proxy REAL,            -- miliar Rp (estimasi tekanan beli/jual big-cap)
    flow_score    REAL,            -- -1..+1 sentimen
    breadth_up    REAL,            -- % saham naik
    advancers     INTEGER,
    decliners     INTEGER,
    label         TEXT,
    top_inflow    TEXT,
    top_outflow   TEXT
);

-- Percakapan/debat antar-agen (Analyst <-> Trader) yang bisa dibaca manusia
CREATE TABLE IF NOT EXISTS conversations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT,
    ticker        TEXT,
    prediction_id INTEGER,
    analyst       TEXT,            -- tesis lengkap dari Analyst
    trader        TEXT,            -- kritik + keputusan dari Trader
    summary       TEXT             -- ringkasan keputusan (arah, %, aksi)
);

-- Pelajaran / memori perbaikan diri (self-tuning loop)
CREATE TABLE IF NOT EXISTS lessons (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT,
    ticker        TEXT,
    kind          TEXT,            -- win | loss | insight
    lesson        TEXT,
    prediction_id INTEGER
);

-- Pantauan pilihan user (star → PRIORITAS agen; pin → tampil di dashboard). Dinamis dari UI.
CREATE TABLE IF NOT EXISTS watchlist (
    ticker    TEXT PRIMARY KEY,
    added_at  TEXT,
    pinned    INTEGER DEFAULT 0
);

-- Kuota harian analis untuk role GUEST. Key = (hari, ip) → tahan restart & re-login
-- (bukan di cookie/memori). Guest bersihkan cookie tetap tak me-reset counter.
CREATE TABLE IF NOT EXISTS guest_quota (
    day    TEXT NOT NULL,          -- YYYY-MM-DD (zona pasar)
    ip     TEXT NOT NULL,          -- IP klien asli (hop terakhir X-Forwarded-For)
    count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, ip)
);

-- Papan skor ABLASI (app.agents.shadow): tiga lengan menilai kandidat yang SAMA pada
-- tanggal, harga masuk, dan horizon yang sama. Terpisah dari `predictions` supaya tak
-- satu pun angka produksi (win-rate, kalibrasi, gerbang, portofolio) ikut berubah.
CREATE TABLE IF NOT EXISTS shadow (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch        TEXT,              -- pengikat satu kandidat: ticker|waktu
    arm          TEXT,              -- mas | heur | llm1
    ts           TEXT,
    ticker       TEXT,
    direction    TEXT,
    probability  REAL,
    horizon_days INTEGER,
    entry_price  REAL,
    expires_at   TEXT,
    status       TEXT DEFAULT 'open',
    resolved_at  TEXT,
    actual_pct   REAL,
    outcome      TEXT,
    meta_json    TEXT
);

CREATE INDEX IF NOT EXISTS idx_shadow_open ON shadow(status);
CREATE INDEX IF NOT EXISTS idx_shadow_batch ON shadow(batch);
CREATE INDEX IF NOT EXISTS idx_conv_ticker ON conversations(ticker);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON agent_logs(ts);
CREATE INDEX IF NOT EXISTS idx_pred_ticker ON predictions(ticker);
CREATE INDEX IF NOT EXISTS idx_pred_status ON predictions(status);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news(ts);
CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker, ts);
"""

# Search engine berita (FTS5) — index full-text atas korpus OSINT (title+summary) supaya AGEN
# bisa MENCARI berita relevan on-demand (bm25), bukan cuma menerima brief yang disuntik. External
# content (content='news') → tak menggandakan teks; trigger jaga sinkron dgn insert/update/delete.
# Dipisah dari SCHEMA: dilewati mulus kalau build SQLite tanpa FTS5 (repo.search_news → LIKE).
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS news_fts USING fts5(
    title, summary, content='news', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS news_ai AFTER INSERT ON news BEGIN
    INSERT INTO news_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS news_ad AFTER DELETE ON news BEGIN
    INSERT INTO news_fts(news_fts, rowid, title, summary)
        VALUES ('delete', old.id, old.title, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS news_au AFTER UPDATE ON news BEGIN
    INSERT INTO news_fts(news_fts, rowid, title, summary)
        VALUES ('delete', old.id, old.title, old.summary);
    INSERT INTO news_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
"""

FTS_ENABLED = False   # di-set init_db; repo.search_news pakai LIKE bila False


def _add_column(conn, table: str, col: str, decl: str) -> None:
    """Tambah kolom idempoten (migrasi ringan tanpa tool eksternal)."""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db() -> None:
    global FTS_ENABLED
    conn = get_conn()
    conn.executescript(SCHEMA)
    # Migrasi: kolom baru prediksi (fitur 2026-07-13) — expires_at (kadaluarsa hari-bursa)
    # + style (scalp/swing/invest auto). ALTER idempoten agar DB lama ikut ter-upgrade.
    _add_column(conn, "predictions", "expires_at", "TEXT")
    _add_column(conn, "predictions", "style", "TEXT")
    conn.commit()
    # Search engine berita: aktifkan FTS5 bila build SQLite mendukung (sebagian tidak).
    try:
        conn.executescript(FTS_SCHEMA)
        # Backfill sekali: kalau index kosong tapi korpus ada → rebuild dari news (external content).
        n_fts = conn.execute("SELECT COUNT(*) c FROM news_fts").fetchone()["c"]
        n_news = conn.execute("SELECT COUNT(*) c FROM news").fetchone()["c"]
        if n_fts < n_news:
            conn.execute("INSERT INTO news_fts(news_fts) VALUES('rebuild')")
        conn.commit()
        FTS_ENABLED = True
    except sqlite3.OperationalError:
        FTS_ENABLED = False   # tak ada FTS5 → repo.search_news fallback LIKE
    # Pastikan ada baris kas awal kalau portofolio kosong.
    cur = conn.execute("SELECT COUNT(*) AS n FROM portfolio")
    if cur.fetchone()["n"] == 0:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO portfolio(ts, cash, equity, total, pnl, pnl_pct) VALUES (?,?,?,?,?,?)",
            (now, config.START_CASH, 0.0, config.START_CASH, 0.0, 0.0),
        )
        conn.commit()
