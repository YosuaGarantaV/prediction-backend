"""Helper akses data (insert/query) + logging terpusat ke DB & file."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import config
from app.db import get_conn, tx

# --- File logger (mirror dari agent_logs ke data/logs/engine.log) ---
_file_logger = logging.getLogger("engine")
if not _file_logger.handlers:
    _file_logger.setLevel(logging.INFO)
    fh = logging.FileHandler(config.LOG_DIR / "engine.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    _file_logger.addHandler(fh)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(obj: Any) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False, default=str)


# ------------------------------------------------------------------ logging
def log(agent: str, phase: str, message: str, *, level: str = "info",
        ticker: str | None = None, payload: Any = None) -> None:
    """Catat satu baris aktivitas agen — tersimpan ke DB dan file."""
    with tx() as conn:
        conn.execute(
            "INSERT INTO agent_logs(ts, agent, phase, level, ticker, message, payload_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (now_iso(), agent, phase, level, ticker, message, _dumps(payload)),
        )
    _file_logger.info("[%s/%s/%s] %s%s", agent, phase, level,
                      f"{ticker} " if ticker else "", message)


def recent_logs(limit: int = 120, agent: str | None = None,
                level: str | None = None) -> list[dict]:
    """Log terbaru, boleh disaring per agen/level.

    Filter ini ada karena satu agen bisa menenggelamkan sisanya: 2026-09-02 tercatat 11.518
    dari 27.925 baris (41%) berasal dari agen 'llm' saja, sehingga 150 baris terakhir yang
    dikirim ke panel hampir tak pernah memuat suara analis/CTO/dewan.
    """
    where, args = [], []
    if agent:
        where.append("agent = ?")
        args.append(agent)
    if level:
        where.append("level = ?")
        args.append(level)
    sql = "SELECT * FROM agent_logs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    rows = get_conn().execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def log_agents(hours: int = 24) -> list[dict]:
    """Agen mana saja yang benar-benar bersuara N jam terakhir + jumlah barisnya.
    Dipakai panel log untuk menyusun filter — dan untuk melihat agen yang DIAM."""
    rows = get_conn().execute(
        "SELECT agent, level, COUNT(*) n FROM agent_logs "
        "WHERE ts > datetime('now', ?) GROUP BY agent, level ORDER BY n DESC",
        (f"-{max(1, int(hours))} hours",),
    ).fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
        d = agg.setdefault(r["agent"], {"agent": r["agent"], "n": 0, "warn": 0, "error": 0})
        d["n"] += r["n"]
        if r["level"] in ("warn", "error"):
            d[r["level"]] += r["n"]
    return sorted(agg.values(), key=lambda d: -d["n"])


def recent_usage(limit: int = 60) -> list[dict]:
    """Log token per-request LLM (phase='usage') — untuk panel debug 'berapa token & output'."""
    rows = get_conn().execute(
        "SELECT ts, message, payload_json FROM agent_logs WHERE phase='usage' "
        "ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        try:
            p = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except Exception:  # noqa: BLE001
            p = {}
        out.append({
            "ts": r["ts"],
            "provider": p.get("provider"), "model": p.get("model"),
            "prompt_tokens": p.get("prompt_tokens"),
            "completion_tokens": p.get("completion_tokens"),
            "total_tokens": p.get("total_tokens"),
            "output": (p.get("output") or "")[:280],
        })
    return out


def usage_summary() -> dict:
    """Ringkasan token 24 jam terakhir: total & per provider/model (untuk KPI biaya)."""
    cutoff = _age_cutoff(24)
    rows = get_conn().execute(
        "SELECT payload_json FROM agent_logs WHERE phase='usage' AND ts>=?", (cutoff,)
    ).fetchall()
    total = 0
    calls = 0
    per: dict[str, dict] = {}
    for r in rows:
        try:
            p = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except Exception:  # noqa: BLE001
            continue
        t = p.get("total_tokens") or 0
        total += t
        calls += 1
        key = f"{p.get('provider')}/{p.get('model')}"
        d = per.setdefault(key, {"calls": 0, "tokens": 0})
        d["calls"] += 1
        d["tokens"] += t
    top = sorted(per.items(), key=lambda kv: kv[1]["tokens"], reverse=True)
    return {"window_h": 24, "total_tokens": total, "calls": calls,
            "by_model": [{"model": k, **v} for k, v in top]}


# ------------------------------------------------------------------ prices
def save_prices(ticker: str, rows: Iterable[dict]) -> None:
    with tx() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO prices(ticker, ts, open, high, low, close, volume) "
            "VALUES (:ticker, :ts, :open, :high, :low, :close, :volume)",
            [{"ticker": ticker, **r} for r in rows],
        )


def save_quote(ticker: str, q: dict) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO quotes(ticker, ts, price, prev_close, change_pct, "
            "volume, day_high, day_low, features_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (ticker, q.get("ts"), q.get("price"), q.get("prev_close"),
             q.get("change_pct"), q.get("volume"), q.get("day_high"),
             q.get("day_low"), _dumps(q.get("features"))),
        )


def get_quote(ticker: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM quotes WHERE ticker=?", (ticker,)).fetchone()
    return dict(row) if row else None


def all_quotes() -> list[dict]:
    rows = get_conn().execute("SELECT * FROM quotes ORDER BY ticker").fetchall()
    return [dict(r) for r in rows]


def quote_tickers() -> list[str]:
    """Ticker yang punya quote tersimpan (sudah terfilter >= MIN_PRICE)."""
    rows = get_conn().execute("SELECT ticker FROM quotes ORDER BY ticker").fetchall()
    return [r["ticker"] for r in rows]


def liquid_tickers(min_turnover: float | None = None, days: int | None = None) -> set[str]:
    """Ticker yang BENAR-BENAR bisa dieksekusi: >= MIN_LIQUID_FRAC sesi dalam `days` terakhir
    ber-turnover (close x volume) >= min_turnover.

    Pakai FRAKSI SESI, bukan rata-rata turnover: satu hari ramai (pom-pom/ARA) tak boleh
    meloloskan saham yang 29 hari lain beku. Lihat catatan config.MIN_TURNOVER untuk bukti
    kenapa gate ini ada. Saham tanpa riwayat harga sama sekali → TIDAK lolos (fail-closed).
    """
    mt = config.MIN_TURNOVER if min_turnover is None else min_turnover
    win = config.LIQUID_WINDOW_DAYS if days is None else days
    conn = get_conn()
    rows = conn.execute(
        "SELECT ticker, AVG(CASE WHEN close*volume >= ? THEN 1.0 ELSE 0.0 END) frac, COUNT(*) n "
        "FROM (SELECT ticker, close, volume FROM prices "
        "      WHERE ts >= date('now', ?) AND close IS NOT NULL AND volume IS NOT NULL) "
        "GROUP BY ticker", (mt, f"-{win} day"),
    ).fetchall()
    # Syarat RIWAYAT terpisah dari syarat turnover. Syarat lama `n >= 5` hanya menghitung sesi
    # DI DALAM jendela 30 hari, jadi listing seumur 9 sesi tetap lolos: JELI dibeli 6x pada
    # 17 Jul 2026 saat usianya 9 sesi dan menyumbang -Rp1,68 jt = 38% seluruh kerugian
    # (audit 2026-09-08). Saham tanpa MIN_LIQUID_HISTORY sesi belum punya rentang harga yang
    # bisa dinilai. Terukur di DB live 8 Sep: 258 ticker lolos gate, 258 juga lolos setelah
    # syarat ini — aturan ini menggigit listing baru saja, bukan menyusutkan universe.
    hist = {r["ticker"] for r in conn.execute(
        "SELECT ticker FROM prices GROUP BY ticker HAVING COUNT(*) >= ?",
        (config.MIN_LIQUID_HISTORY,)).fetchall()}
    return {r["ticker"] for r in rows
            if r["n"] >= 5 and r["frac"] >= config.MIN_LIQUID_FRAC and r["ticker"] in hist}


def price_history(ticker: str, limit: int = 200) -> list[dict]:
    rows = get_conn().execute(
        "SELECT ts, open, high, low, close, volume FROM prices WHERE ticker=? "
        "ORDER BY ts DESC LIMIT ?", (ticker, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


# ------------------------------------------------------------------ macro
def save_macro(name: str, symbol: str, price: float, change_pct: float) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO macro(name, symbol, ts, price, change_pct) VALUES (?,?,?,?,?)",
            (name, symbol, now_iso(), price, change_pct),
        )


def all_macro() -> list[dict]:
    rows = get_conn().execute("SELECT * FROM macro ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def source_freshness() -> list[dict]:
    """Umur (jam) data terbaru tiap sumber + status → panel kesehatan. MENANGKAP bug data-basi
    (mis. IHSG berhenti update) sebelum menyetir prediksi. stale kalau umur > ambang wajar."""
    conn = get_conn()
    now = datetime.now(timezone.utc)

    def age_h(ts) -> float | None:
        if not ts:
            return None
        try:
            d = datetime.fromisoformat(ts)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return round((now - d).total_seconds() / 3600, 1)
        except Exception:  # noqa: BLE001
            return None

    # Fundamental SENGAJA di-cache fundamentals.CACHE_DAYS hari (fundamental berubah lambat,
    # fetch hanya utk kandidat yg dianalisis LLM). Ambang lama 12 jam melawan desain itu:
    # umur 12-72 jam = bekerja persis seperti rancangannya tapi dilaporkan BASI — alarm palsu
    # terjamin, dan alarm yang selalu merah membuat kegagalan ASLI tak terlihat. Diturunkan
    # dari CACHE_DAYS supaya tak bisa menyimpang lagi (+12 jam kelonggaran: refresh baru
    # terjadi saat ada siklus analisis, bukan tepat saat cache kedaluwarsa).
    from app.data.fundamentals import CACHE_DAYS as _FUND_CACHE_DAYS
    # (label, query max-ts, ambang stale jam) — ambang = seberapa lama sebelum dicurigai basi
    specs = [
        ("harga saham", "SELECT MAX(ts) FROM quotes", 1.0),
        ("berita", "SELECT MAX(ts) FROM news", 1.5),
        ("makro/indeks", "SELECT MAX(ts) FROM macro", 1.0),
        ("arus asing (proxy)", "SELECT MAX(ts) FROM flow", 1.0),
        ("prediksi", "SELECT MAX(ts) FROM predictions", 6.0),
        ("fundamental", "SELECT MAX(ts) FROM fundamentals", _FUND_CACHE_DAYS * 24 + 12),
    ]
    out = []
    for label, q, thresh in specs:
        ts = conn.execute(q).fetchone()[0]
        a = age_h(ts)
        out.append({"source": label, "age_h": a, "ts": ts,
                    "stale": a is None or a > thresh, "threshold_h": thresh})

    # Arus asing RESMI (idxflow.py, file JSON bukan tabel DB) — celah ditemukan 2026-07-13:
    # panel kesehatan tak pernah memantau sumber ini; kalau job refresh_foreign diam-diam
    # rusak (mis. Cloudflare bypass berhenti tembus), tak ada yang memberi tahu. Pakai MTIME
    # file (kapan job TERAKHIR BERHASIL menulis) — bukan tanggal isinya (yg wajar tertinggal
    # 1 hari bursa saat market masih buka; itu bukan basi). Ambang 8j > interval job 2j/4j.
    foreign_file = config.DATA_DIR / "foreign_flow.json"
    try:
        mtime = foreign_file.stat().st_mtime
        a = round((now.timestamp() - mtime) / 3600, 1)
    except OSError:
        a = None
    out.append({"source": "arus asing (resmi IDX)", "age_h": a, "ts": None,
                "stale": a is None or a > 8.0, "threshold_h": 8.0})
    return out


# ------------------------------------------------------------------ fundamentals
def save_fundamentals(f: dict) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fundamentals(ticker, ts, per, forward_pe, pbv, roe, "
            "div_yield, profit_margin, earnings_growth, revenue_growth, debt_equity, beta, "
            "rec_key, target_price, mcap, sector, industry) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["ticker"], now_iso(), f.get("per"), f.get("forward_pe"), f.get("pbv"),
             f.get("roe"), f.get("div_yield"), f.get("profit_margin"),
             f.get("earnings_growth"), f.get("revenue_growth"), f.get("debt_equity"),
             f.get("beta"), f.get("rec_key"), f.get("target_price"), f.get("mcap"),
             f.get("sector"), f.get("industry")),
        )


def get_fundamentals_row(ticker: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM fundamentals WHERE ticker=?", (ticker,)).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------ flow
def save_flow(f: dict) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO flow(ts, foreign_proxy, flow_score, breadth_up, "
            "advancers, decliners, label, top_inflow, top_outflow) VALUES (?,?,?,?,?,?,?,?,?)",
            (now_iso(), f["foreign_proxy"], f["flow_score"], f["breadth_up"],
             f["advancers"], f["decliners"], f["label"], f["top_inflow"], f["top_outflow"]),
        )


def latest_flow() -> dict | None:
    row = get_conn().execute("SELECT * FROM flow ORDER BY ts DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------ news
def save_news(item: dict) -> bool:
    """Return True kalau berita baru (belum ada url-nya)."""
    try:
        with tx() as conn:
            conn.execute(
                "INSERT INTO news(ts, scope, source, title, url, summary, tickers, sentiment, impact) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (item.get("ts", now_iso()), item.get("scope"), item.get("source"),
                 item.get("title"), item.get("url"), item.get("summary"),
                 ",".join(item.get("tickers", [])), item.get("sentiment", "neutral"),
                 int(item.get("impact", 0))),
            )
        return True
    except Exception:
        return False  # url duplikat (UNIQUE)


def _dedup_news(rows: list[dict]) -> list[dict]:
    """Buang berita yang judulnya kembar (1 peristiwa dari banyak feed) → tak dihitung berkali-kali."""
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        key = re.sub(r"\W+", "", (r.get("title") or "").lower())[:60]
        if key and key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _age_cutoff(max_age_hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()


def recent_news(limit: int = 60, scope: str | None = None,
                max_age_hours: float = 72) -> list[dict]:
    q = "SELECT * FROM news WHERE ts>=?"
    args: list = [_age_cutoff(max_age_hours)]
    if scope:
        q += " AND scope=?"
        args.append(scope)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(limit * 2)  # ambil lebih, sisakan ruang utk dedup
    rows = [dict(r) for r in get_conn().execute(q, args).fetchall()]
    return _dedup_news(rows)[:limit]


def news_for_ticker(ticker: str, limit: int = 10,
                    max_age_hours: float = 72) -> list[dict]:
    """Berita relevan & SEGAR (default 72 jam). Berita/FILING khusus emiten DIPRIORITASKAN —
    jangan sampai tergusur berita makro global yang lebih baru (mis. filing weekend)."""
    cutoff = _age_cutoff(max_age_hours)
    conn = get_conn()
    own = [dict(r) for r in conn.execute(
        "SELECT * FROM news WHERE tickers LIKE ? AND ts>=? ORDER BY ts DESC LIMIT ?",
        (f"%{ticker}%", cutoff, limit),
    ).fetchall()]
    glob = [dict(r) for r in conn.execute(
        "SELECT * FROM news WHERE scope='global' AND ts>=? ORDER BY ts DESC LIMIT ?",
        (cutoff, limit * 2),
    ).fetchall()]
    return _dedup_news(own + glob)[:limit]


def search_news(query: str, limit: int = 6, max_age_days: int | None = None) -> list[dict]:
    """SEARCH ENGINE berita: cari korpus OSINT full-text (FTS5 bm25), fallback LIKE.
    Dipakai agen (tool search_news) untuk menarik berita relevan on-demand di luar brief.
    Sanitasi anti-injeksi FTS5: query dipecah jadi token alfanumerik saja (buang sintaks
    MATCH), digabung OR untuk recall, di-rank bm25 (paling relevan dulu)."""
    import re as _re
    from app import db
    terms = [t for t in _re.findall(r"[0-9A-Za-z]+", query or "") if len(t) >= 2][:8]
    if not terms:
        return []
    args: list = []
    where_age = ""
    if max_age_days:
        where_age = " AND n.ts >= ?"
        args_age = [(datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()]
    else:
        args_age = []
    conn = get_conn()
    if getattr(db, "FTS_ENABLED", False):
        # tiap term di-QUOTE → jadi frasa literal, kebal keyword FTS5 (AND/OR/NOT/NEAR) & sintaks
        match = " OR ".join(f'"{t}"' for t in terms)
        sql = ("SELECT n.* FROM news_fts f JOIN news n ON n.id=f.rowid "
               "WHERE news_fts MATCH ?" + where_age + " ORDER BY bm25(news_fts) LIMIT ?")
        args = [match] + args_age + [limit]
        try:
            rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
            return _dedup_news(rows)[:limit]
        except sqlite3.OperationalError:
            pass                                          # korpus/index anomali → jatuh ke LIKE
    like = " OR ".join(["(n.title LIKE ? OR n.summary LIKE ?)"] * len(terms))
    args = []
    for t in terms:
        args += [f"%{t}%", f"%{t}%"]
    sql = (f"SELECT n.* FROM news n WHERE ({like})" + where_age +
           " ORDER BY n.ts DESC LIMIT ?")
    rows = [dict(r) for r in conn.execute(sql, args + args_age + [limit]).fetchall()]
    return _dedup_news(rows)[:limit]


def ticker_news_key(ticker: str, max_age_hours: float = 72) -> tuple[int, int]:
    """(jumlah, id maks) berita KHUSUS emiten dlm jendela — kunci deteksi 'ada berita baru'
    untuk memoization tesis analis. Berita baru utk ticker → key berubah → tesis dianalisis ulang."""
    cutoff = _age_cutoff(max_age_hours)
    r = get_conn().execute(
        "SELECT COUNT(*) c, COALESCE(MAX(id),0) m FROM news WHERE tickers LIKE ? AND ts>=?",
        (f"%{ticker}%", cutoff),
    ).fetchone()
    return (r["c"], r["m"])


def news_sentiment_map(max_age_hours: float = 24) -> dict[str, int]:
    """Skor sentimen berita PER-TICKER (jumlah impact, clamp -5..+5) dlewat SATU query.
    Dipakai SCANNER agar saham berkatalis (mis. minyak naik → MEDC/PGAS) ikut naik ranking
    kandidat — sebelumnya scan memakai news=0 (buta berita), jadi katalis terkuat tak terlihat."""
    cutoff = _age_cutoff(max_age_hours)
    rows = get_conn().execute(
        "SELECT tickers, impact FROM news WHERE ts>=? AND tickers IS NOT NULL AND tickers!=''",
        (cutoff,),
    ).fetchall()
    scores: dict[str, int] = {}
    for r in rows:
        imp = int(r["impact"] or 0)
        if not imp:
            continue
        for t in str(r["tickers"]).split(","):
            t = t.strip().upper()
            if t:
                scores[t] = scores.get(t, 0) + imp
    return {t: max(-5, min(5, s)) for t, s in scores.items()}


# ------------------------------------------------------------------ predictions
def save_prediction(p: dict) -> int:
    # Tag mode engine (agent vs pipeline) di TIAP prediksi — chokepoint tunggal, semua jalur
    # lewat sini → compare_modes.py bisa memisah win-rate per mode (A/B minggu agent).
    import config
    from app import market_calendar as _cal
    from datetime import date
    factors = p.get("factors") or {}
    factors.setdefault("engine_mode", config.ENGINE_MODE)
    # COLLECTOR item-3 (NEXT_UPDATES): rekam foreign_pressure SAAT prediksi dibuat. Arus asing
    # tak diarsipkan per-tanggal, jadi snapshot-di-titik-prediksi = satu-satunya cara mengumpulkan
    # bukti untuk backtest_foreign_ab nanti. Baca cache murni (foreign_for). Gagal → None; JANGAN
    # pernah ganggu penyimpanan prediksi.
    if "foreign_pressure" not in factors:
        try:
            from app.data import idxflow
            factors["foreign_pressure"] = idxflow.foreign_pressure(p["ticker"])
        except Exception:  # noqa: BLE001
            factors["foreign_pressure"] = None
    # COLLECTOR BAYANGAN (mulai 2026-08-31) — DUA model dicatat berdampingan pada baris
    # prediksi yang SAMA supaya nanti bisa diadu di atas populasi identik, bukan dua backtest
    # berpopulasi beda. Tak satu pun dari nilai ini menyetir keputusan; semuanya hanya bukti:
    #   model_p   = app.model.predict_proba (logreg lama; None sejak AUC 0,451 gagal gerbang)
    #   xsec_pct  = app.xsec percentil cross-sectional M0 (gagal F7 tilt-beta 20 Jul, dipantau)
    #   raw_prob  = keyakinan SEBELUM skill.calibrated_probability → bahan fit KEEP_SPREAD
    #   score     = skor komposit heuristik → bahan uji tanda-terbalik (bayangan) tanpa
    #               mengubah apa pun di produksi
    # Semuanya best-effort; kegagalan apa pun TIDAK boleh mengganggu penyimpanan prediksi.
    if "model_p" not in factors:
        try:
            from app import model as _m
            q = get_quote(p["ticker"]) or {}
            feats = json.loads(q.get("features_json") or "{}")
            factors["model_p"] = _m.predict_proba(feats)
        except Exception:  # noqa: BLE001
            factors["model_p"] = None
    if "xsec_pct" not in factors:
        try:
            from app import xsec as _xs
            factors["xsec_pct"] = _xs.shadow_for(p["ticker"])
        except Exception:  # noqa: BLE001
            factors["xsec_pct"] = None
    # Kadaluarsa dalam HARI BURSA (lewati weekend + libur nasional). Prediksi 1-hari Jumat →
    # jatuh tempo Senin; kalau Senin libur → mundur ke Selasa (permintaan user 2026-07-13).
    horizon = int(p["horizon_days"] or 1)
    # expires_at dari tanggal WIB (BUKAN zona server/UTC). Bug 2026-07-17: prediksi dibuat malam
    # UTC = pagi WIB hari berikutnya; dgn date.today() UTC, expires jatuh di tanggal WIB yang SAMA
    # dgn pembuatan → langsung "due" (today WIB >= expires) → di-resolve mid-sesi, horizon efektif 0.
    expires_at = _cal.add_trading_days(datetime.now(_WIB).date(), max(1, horizon)).isoformat()
    style = p.get("style") or (factors.get("style") if isinstance(factors, dict) else None)
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO predictions(ts, ticker, direction, probability, horizon_days, "
            "entry_price, target_price, expected_pct, reasoning, factors_json, status, "
            "expires_at, style) VALUES (?,?,?,?,?,?,?,?,?,?, 'open', ?, ?)",
            (now_iso(), p["ticker"], p["direction"], p["probability"], p["horizon_days"],
             p.get("entry_price"), p.get("target_price"), p.get("expected_pct"),
             p.get("reasoning"), _dumps(factors), expires_at, style),
        )
        return cur.lastrowid


# ------------------------------------------------------------------ watchlist (pantauan user)
def watch_add(ticker: str) -> None:
    with tx() as conn:
        conn.execute("INSERT OR IGNORE INTO watchlist(ticker, added_at, pinned) VALUES (?,?,0)",
                     (ticker.upper(), now_iso()))


def watch_remove(ticker: str) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM watchlist WHERE ticker=?", (ticker.upper(),))


def watch_toggle(ticker: str) -> bool:
    """Star/unstar → kembalikan status baru (True=dipantau)."""
    t = ticker.upper()
    if get_conn().execute("SELECT 1 FROM watchlist WHERE ticker=?", (t,)).fetchone():
        watch_remove(t)
        return False
    watch_add(t)
    return True


def pin_toggle(ticker: str) -> bool:
    """Pin/unpin (hanya berlaku bila sudah dipantau) → kembalikan status pin baru."""
    t = ticker.upper()
    row = get_conn().execute("SELECT pinned FROM watchlist WHERE ticker=?", (t,)).fetchone()
    if not row:
        watch_add(t)
        new = 1
    else:
        new = 0 if row["pinned"] else 1
    with tx() as conn:
        conn.execute("UPDATE watchlist SET pinned=? WHERE ticker=?", (new, t))
    return bool(new)


def watched_tickers() -> list[str]:
    rows = get_conn().execute("SELECT ticker FROM watchlist ORDER BY added_at DESC").fetchall()
    return [r["ticker"] for r in rows]


def watched_set() -> set[str]:
    return set(watched_tickers())


def pinned_tickers() -> list[str]:
    rows = get_conn().execute(
        "SELECT ticker FROM watchlist WHERE pinned=1 ORDER BY added_at DESC").fetchall()
    return [r["ticker"] for r in rows]


def watchlist_state() -> dict[str, bool]:
    """{ticker: pinned} untuk semua yang dipantau — dipakai UI menandai bintang/pin."""
    rows = get_conn().execute("SELECT ticker, pinned FROM watchlist").fetchall()
    return {r["ticker"]: bool(r["pinned"]) for r in rows}


# ------------------------------------------------------------------ kuota guest
def guest_quota_incr(day: str, ip: str) -> int:
    """Naikkan counter analis guest untuk (hari, ip) secara ATOMIK, kembalikan nilai baru.
    Persisten di DB → restart service / re-login / hapus cookie TIDAK me-reset. UPSERT satu
    pernyataan (tanpa race read-modify-write)."""
    with tx() as conn:
        conn.execute(
            "INSERT INTO guest_quota(day, ip, count) VALUES (?,?,1) "
            "ON CONFLICT(day, ip) DO UPDATE SET count = count + 1",
            (day, ip),
        )
        row = conn.execute(
            "SELECT count FROM guest_quota WHERE day=? AND ip=?", (day, ip)).fetchone()
    return int(row["count"]) if row else 0


def guest_quota_get(day: str, ip: str) -> int:
    """Counter analis guest saat ini untuk (hari, ip) — 0 bila belum ada baris."""
    row = get_conn().execute(
        "SELECT count FROM guest_quota WHERE day=? AND ip=?", (day, ip)).fetchone()
    return int(row["count"]) if row else 0


def latest_prediction(ticker: str) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM predictions WHERE ticker=? ORDER BY id DESC LIMIT 1", (ticker,)
    ).fetchone()
    return dict(row) if row else None


def latest_predictions() -> dict[str, dict]:
    """Prediksi OPEN terbaru per ticker — sinyal 'sekarang' untuk watchlist.
    Dulu ambil baris terakhir APA PUN statusnya → bangkai resolved berumur seminggu
    (85% seragam pra-kalibrasi) nampang seolah sinyal hari ini. Open-only = jujur."""
    rows = get_conn().execute(
        "SELECT p.* FROM predictions p JOIN (SELECT ticker, MAX(id) mid FROM predictions "
        "WHERE status='open' GROUP BY ticker) g ON p.id=g.mid"
    ).fetchall()
    return {r["ticker"]: dict(r) for r in rows}


def open_predictions_by_horizon() -> dict[str, dict[str, dict]]:
    """{ticker: {bucket: prediksi}} — prediksi OPEN terbaru per ticker per BUCKET horizon
    (1=besok, 3, 5, 10) untuk toggle horizon di UI. Bucket = pembulatan ke {1,3,5,10}."""
    rows = get_conn().execute(
        "SELECT p.* FROM predictions p JOIN (SELECT ticker, horizon_days, MAX(id) mid "
        "FROM predictions WHERE status='open' GROUP BY ticker, horizon_days) g ON p.id=g.mid"
    ).fetchall()
    out: dict[str, dict[str, dict]] = {}
    for r in rows:
        h = r["horizon_days"] or 3
        bucket = "1" if h <= 1 else "3" if h <= 3 else "5" if h <= 6 else "10"
        d = out.setdefault(r["ticker"], {})
        prev = d.get(bucket)
        if prev is None or r["id"] > prev["id"]:   # id terbesar per bucket menang
            d[bucket] = dict(r)
    return out


_WIB = timezone(timedelta(hours=7))


def _trading_days_elapsed(made: datetime, now: datetime) -> int:
    """Hari BURSA (Sen-Jum) yang berlalu antara dua waktu, dihitung di zona WIB.
    Prediksi dibuat Sabtu tak boleh 'jatuh tempo' Minggu (harga beku → resolve acak).
    ponytail: weekday saja; libur nasional bursa tak terdaftar → toleransi ±1 hari."""
    d0 = made.astimezone(_WIB).date()
    d1 = now.astimezone(_WIB).date()
    n = 0
    cur = d0
    while cur < d1:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def open_predictions() -> list[dict]:
    """Semua prediksi berstatus 'open' (belum resolve), horizon terlewati atau belum."""
    rows = get_conn().execute("SELECT * FROM predictions WHERE status='open'").fetchall()
    return [dict(r) for r in rows]


def open_predictions_for_ticker(ticker: str) -> list[dict]:
    """Semua prediksi 'open' utk 1 ticker (dipakai supersede-check: cegah beberapa taruhan
    MAIN nyala bersamaan buat ticker yg sama — lihat orchestrator._supersede_stale)."""
    rows = get_conn().execute(
        "SELECT * FROM predictions WHERE status='open' AND ticker=?", (ticker,)
    ).fetchall()
    return [dict(r) for r in rows]


def open_prediction_id(ticker: str, direction: str, horizon: int | None = None) -> int | None:
    """ID prediksi open TERTUA utk ticker+arah+horizon — guard anti-duplikat: 1 sinyal = 1
    taruhan. (Audit 2026-07-01: 978/1161 open = duplikat, FORU DOWN x35 → 1 sinyal dihitung
    35x saat resolve = statistik menggelembung.) Horizon ikut kunci: taruhan 1 hari ≠ 3 hari
    (tombol UI 1/3/5 harus bisa mencatat taruhan baru di jendela beda)."""
    q = "SELECT id FROM predictions WHERE status='open' AND ticker=? AND direction=?"
    args: list = [ticker, direction]
    if horizon is not None:
        q += " AND horizon_days=?"
        args.append(horizon)
    row = get_conn().execute(q + " ORDER BY id LIMIT 1", args).fetchone()
    return row["id"] if row else None


def prediction_is_due(r: dict, now: datetime) -> bool:
    """Prediksi ini sudah MENJALANI horizonnya (layak dinilai menang/kalah)? Utamakan
    `expires_at` (holiday-aware, dihitung `market_calendar.add_trading_days`): due bila sesi
    bursa terakhir yang TUTUP >= expires_at. Baris lama tanpa expires_at → fallback hitung
    hari-bursa weekday (_trading_days_elapsed).

    SATU definisi dipakai dua pemanggil: `open_predictions_due` (jatuh tempo wajar) DAN
    `orchestrator._supersede_one` (digantikan analisis baru). Sebelum 2026-08-13 supersede
    memakai penjaga yang lebih longgar ("sudah ada 1 sesi tutup") sehingga taruhan 3-5 hari
    dinilai pada penutupan HARI PERTAMA — audit: 34 dari 90 prediksi headline dinilai pada
    umur rata-rata 0,61 hari untuk horizon rata-rata 3,9 hari (17,6% menang vs 39,3% untuk
    yang benar-benar jalan penuh). Itu mengukur taruhan 1-hari lalu melabelinya 3-hari."""
    from app import market_calendar as _cal
    # Penjaga: JANGAN resolve sebelum ada sesi bursa yang TUTUP setelah prediksi dibuat.
    # Pakai sesi, bukan tanggal kalender: Sabtu/Minggu/libur/pra-closing = harga masih
    # sama persis dgn entry → 0.0% palsu. Fix 2026-07-19 (lihat market_calendar.has_new_session).
    try:
        if not _cal.has_new_session(datetime.fromisoformat(r["ts"]), now):
            return False
    except Exception:  # noqa: BLE001
        pass
    exp = r.get("expires_at") if isinstance(r, dict) else None
    if exp:
        try:
            return _cal.last_closed_session(now) >= datetime.fromisoformat(exp).date()
        except Exception:  # noqa: BLE001
            pass
    try:
        made = datetime.fromisoformat(r["ts"])
    except Exception:  # noqa: BLE001
        return False
    return _trading_days_elapsed(made, now) >= r["horizon_days"]


def open_predictions_due(now: datetime) -> list[dict]:
    """Prediksi 'open' yang sudah jatuh tempo -> siap dievaluasi."""
    return [r for r in open_predictions() if prediction_is_due(r, now)]


def open_prediction_tickers() -> list[str]:
    """Ticker yang punya prediksi BELUM resolve → harus tetap fresh untuk evaluasi akurat."""
    rows = get_conn().execute(
        "SELECT DISTINCT ticker FROM predictions WHERE status='open'").fetchall()
    return [r["ticker"] for r in rows]


def resolve_prediction(pid: int, actual_pct: float, outcome: str) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE predictions SET status='resolved', resolved_at=?, actual_pct=?, outcome=? "
            "WHERE id=?", (now_iso(), actual_pct, outcome, pid),
        )


def supersede_prediction(pid: int) -> None:
    """Tandai prediksi 'superseded': DIGANTIKAN analisis lebih baru SEBELUM sempat dinilai
    (dibuat & diganti di hari yang sama). BUKAN menang/kalah — belum jadi taruhan tuntas →
    dikecualikan dari statistik win-rate DAN dari daftar UI (tak ada 'salah' palsu di hari
    yg sama). Baris tetap ada di DB utk audit."""
    with tx() as conn:
        conn.execute(
            "UPDATE predictions SET status='superseded', resolved_at=? WHERE id=?",
            (now_iso(), pid),
        )


def ticker_track_record(ticker: str, last_n: int = 5) -> dict | None:
    """Rekam jejak prediksi RESOLVED di 1 saham — memori per-agen per-saham: agen membaca
    hasil nyatanya sendiri di saham ini sebelum memutuskan (belajar dari kesalahan sendiri,
    bukan hanya lessons global). None kalau belum ada riwayat."""
    conn = get_conn()
    tal = conn.execute(
        "SELECT direction, COUNT(*) n, SUM(outcome='win') w FROM predictions "
        "WHERE ticker=? AND status='resolved' GROUP BY direction", (ticker,),
    ).fetchall()
    if not tal:
        return None
    recent = conn.execute(
        "SELECT direction, probability, outcome, actual_pct FROM predictions "
        "WHERE ticker=? AND status='resolved' ORDER BY id DESC LIMIT ?", (ticker, last_n),
    ).fetchall()
    return {
        "by_direction": {r["direction"]: {"n": r["n"], "win": r["w"]} for r in tal},
        "recent": [dict(r) for r in recent],
    }


def predictions_for_ticker(ticker: str, limit: int = 25) -> list[dict]:
    """Riwayat prediksi 1 saham (untuk penanda di chart: kapan dibuat, arah, hasil)."""
    rows = get_conn().execute(
        "SELECT ts, direction, probability, horizon_days, entry_price, target_price, "
        "status, outcome, actual_pct, resolved_at FROM predictions "
        "WHERE ticker=? AND status IN ('open','resolved') ORDER BY id DESC LIMIT ?",
        (ticker, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_predictions(limit: int = 50) -> list[dict]:
    # 'superseded' (draft diganti sebelum dinilai) dikecualikan — bukan taruhan nyata,
    # tak perlu mengotori tabel prediksi (user tak lihat 'salah' palsu hari-sama).
    rows = get_conn().execute(
        "SELECT * FROM predictions WHERE status!='superseded' ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def resolved_predictions(limit: int = 500) -> list[dict]:
    # Forecast display 1-hari (reasoning 'prediksi 1-hari...') BUKAN taruhan engine —
    # dikeluarkan agar calibrated_probability/skill belajar dari taruhan nyata, bukan
    # display (44% win, n=176, mencemari shrinkage — forensik 2026-07-07).
    # Jaring kedua: buang resolusi yang belum melewati sesi bursa mana pun. Dulu penjaganya
    # SQL tanggal WIB (`date(resolved_at,'+7h') > date(ts,'+7h')`, fix 2026-07-17) — itu
    # meloloskan akhir pekan/libur DAN membuang observasi yang sebenarnya SAH (dibuat pagi,
    # ditutup sesudah closing di hari yang sama). Sekarang satu definisi saja, sama dengan
    # penjaga di orchestrator/open_predictions_due: market_calendar.has_new_session.
    # ponytail: filter di Python, bukan SQL, karena kalender libur hidup di Python; tabel
    # prediksi masih ratusan baris — pindahkan ke SQL kalau sudah puluhan ribu.
    from app import market_calendar as _cal
    rows = get_conn().execute(
        "SELECT direction, probability, actual_pct, outcome, horizon_days, ts, resolved_at "
        "FROM predictions "
        "WHERE status='resolved' AND NOT (horizon_days=1 AND reasoning LIKE 'prediksi 1-hari%') "
        "ORDER BY id DESC", ()
    ).fetchall()
    out = []
    for r in rows:
        try:
            if not _cal.has_new_session(datetime.fromisoformat(r["ts"]),
                                        datetime.fromisoformat(r["resolved_at"])):
                continue
        except (TypeError, ValueError):
            continue          # ts/resolved_at hilang atau rusak -> tak bisa diverifikasi, buang
        out.append(dict(r))
        if len(out) >= limit:
            break
    return out


def alpha_scoreboard() -> list[dict]:
    """UJI ALPHA: win-rate MENTAH vs RELATIF-PASAR (excess vs IHSG) per arah. Menang mentah bisa
    palsu (ikut pasar turun); alpha = saham kalahkan/ungguli pasar = skill relatif nyata.
    Agregasi murni dari data (entry ts, resolved_at, actual_pct) + IHSG — tanpa migrasi skema."""
    conn = get_conn()
    ih = {r["d"]: r["close"] for r in
          conn.execute("SELECT substr(ts,1,10) d, close FROM prices WHERE ticker='IHSG'")}
    ihdays = sorted(ih)

    def ihsg_at(d: str):
        prev = None
        for x in ihdays:
            if x <= d:
                prev = ih[x]
            else:
                break
        return prev

    rows = conn.execute(
        "SELECT ts, resolved_at, direction, actual_pct FROM predictions "
        "WHERE status='resolved' AND resolved_at IS NOT NULL AND actual_pct IS NOT NULL"
    ).fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
        i0, i1 = ihsg_at(r["ts"][:10]), ihsg_at(r["resolved_at"][:10])
        if not i0 or not i1:
            continue
        mkt = (i1 / i0 - 1) * 100
        stk = r["actual_pct"]
        ex = stk - mkt
        dr = r["direction"]
        a = agg.setdefault(dr, {"direction": dr, "n": 0, "raw": 0, "alpha": 0, "exsum": 0.0})
        a["n"] += 1
        a["raw"] += (stk > 0.5 if dr == "UP" else stk < -0.5 if dr == "DOWN" else abs(stk) <= 1.5)
        a["alpha"] += (ex > 0.3 if dr == "UP" else ex < -0.3 if dr == "DOWN" else abs(ex) <= 1.0)
        a["exsum"] += ex * (1 if dr == "UP" else -1 if dr == "DOWN" else 0)
    out = []
    for a in agg.values():
        if a["n"] < 5:
            continue
        out.append({"direction": a["direction"], "n": a["n"],
                    "raw_win_rate": round(a["raw"] / a["n"] * 100, 1),
                    "alpha_win_rate": round(a["alpha"] / a["n"] * 100, 1),
                    "avg_excess": round(a["exsum"] / a["n"], 2)})
    return sorted(out, key=lambda x: -x["n"])


def daily_ledger(days: int = 30) -> list[dict]:
    """Ledger performa PER-HARI (agregasi dari data yg sudah ada — tanpa tabel baru):
    ekuitas akhir, PnL harian, jumlah trade, prediksi dibuat, prediksi resolved + win-rate.
    Inti evaluasi trading: kurva ekuitas + akurasi harian, deteksi hari buruk cepat."""
    conn = get_conn()
    eq: dict[str, float] = {}
    for r in conn.execute("SELECT ts, total FROM portfolio ORDER BY ts"):
        eq[r["ts"][:10]] = r["total"]          # snapshot TERAKHIR tiap tanggal menang
    trades_d: dict[str, int] = {}
    for r in conn.execute("SELECT ts FROM trades"):
        trades_d[r["ts"][:10]] = trades_d.get(r["ts"][:10], 0) + 1
    made: dict[str, int] = {}
    for r in conn.execute("SELECT ts FROM predictions"):
        made[r["ts"][:10]] = made.get(r["ts"][:10], 0) + 1
    res: dict[str, list] = {}
    for r in conn.execute("SELECT resolved_at, outcome FROM predictions "
                          "WHERE status='resolved' AND resolved_at IS NOT NULL"):
        d = r["resolved_at"][:10]
        rec = res.setdefault(d, [0, 0])
        rec[0] += 1
        rec[1] += 1 if r["outcome"] == "win" else 0
    out = []
    prev = None
    for d in sorted(eq):
        e = eq[d]
        rn, rw = res.get(d, [0, 0])
        out.append({
            "date": d, "equity": round(e), "pnl_day": round(e - prev) if prev is not None else 0,
            "trades": trades_d.get(d, 0), "predictions": made.get(d, 0),
            "resolved": rn, "win_rate": round(rw / rn * 100, 1) if rn else None,
        })
        prev = e
    return out[-days:]


def factor_scoreboard(min_n: int = 3) -> list[dict]:
    """Evaluasi tiap FAKTOR STRUKTURAL bernama: berapa kali fired di prediksi resolved & win-rate-nya.
    Inilah 'evaluasi besok' — lihat faktor mana yang benar-benar prediktif vs noise. Arah faktor
    (dir) harus SELARAS arah prediksi menang: fmenang = faktor mendukung outcome benar."""
    # SATU definisi resolusi sah — sama dengan resolved_predictions(). Penjaga lama di sini
    # adalah SQL tanggal WIB (`date(resolved_at,'+7 hours') > date(ts,'+7 hours')`) yang
    # justru MEMBUANG penutupan bracket yang sah: diukur 2026-07-30 di live, 73 dari 423 baris
    # (17%) hilang, dan yang hilang adalah gerakan TERBESAR (prediksi dibuat pagi lalu ditutup
    # sesudah closing di hari yang sama). Papan skor faktor ini menyetir prompt evaluator →
    # butanya justru pada kasus paling informatif.
    from app import market_calendar as _cal
    rows = get_conn().execute(
        "SELECT direction, outcome, factors_json, ts, resolved_at FROM predictions "
        "WHERE status='resolved' AND factors_json IS NOT NULL "
        "AND NOT (horizon_days=1 AND reasoning LIKE 'prediksi 1-hari%')"
    ).fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
        try:
            if not _cal.has_new_session(datetime.fromisoformat(r["ts"]),
                                        datetime.fromisoformat(r["resolved_at"])):
                continue
        except (TypeError, ValueError):
            continue      # ts/resolved_at hilang atau rusak -> tak bisa diverifikasi, buang
        try:
            sigs = (json.loads(r["factors_json"]) or {}).get("signals") or []
        except Exception:  # noqa: BLE001
            continue
        for s in sigs:
            name = s.get("name")
            if not name:
                continue
            d = agg.setdefault(name, {"factor": name, "n": 0, "win": 0})
            d["n"] += 1
            if r["outcome"] == "win":
                d["win"] += 1
    out = [{**d, "win_rate": round(d["win"] / d["n"] * 100, 1)}
           for d in agg.values() if d["n"] >= min_n]
    return sorted(out, key=lambda x: -x["n"])


def prediction_stats() -> dict:
    """Papan skor headline — SATU populasi dengan `resolved_predictions()`: taruhan nyata
    saja (forecast display 1-hari & resolusi yang belum melewati sesi bursa dibuang).

    Dulu fungsi ini punya SQL sendiri yang menghitung SEMUA baris resolved → headline 45,9%
    (n=307) padahal 79% isinya forecast display 1-hari, sementara kartu Skill di halaman yang
    SAMA memakai populasi lain (44,6%, n=65). Dua angka berbeda di satu halaman = tak bisa
    dipercaya (audit 2026-07-27). Sekarang satu sumber kebenaran.

    `hidden_*` = keputusan yang sengaja TIDAK masuk papan skor. Ditampilkan supaya papan skor
    tak terlihat mustahil bagus: separuh output engine memang tak pernah dinilai.
    """
    rows = resolved_predictions(1_000_000)
    wins = sum(1 for r in rows if r["outcome"] == "win")
    losses = sum(1 for r in rows if r["outcome"] == "loss")
    conn = get_conn()

    def _n(sql: str) -> int:
        return conn.execute(sql).fetchone()[0] or 0

    total = wins + losses
    return {
        "resolved": total, "wins": wins, "losses": losses,
        "win_rate": round(wins / total * 100, 1) if total else 0.0,
        "open": _n("SELECT COUNT(*) FROM predictions WHERE status='open'"),
        # draft diganti analisis lebih baru sebelum sempat dinilai (lihat supersede_prediction)
        "hidden_superseded": _n("SELECT COUNT(*) FROM predictions WHERE status='superseded'"),
        # forecast tampilan "besok", bukan taruhan engine (lihat _commit_1d)
        "hidden_display_1d": _n("SELECT COUNT(*) FROM predictions WHERE status='resolved' "
                                "AND horizon_days=1 AND reasoning LIKE 'prediksi 1-hari%'"),
        # FLAT multi-hari tak pernah masuk tabel predictions — jejaknya cuma di log.
        # ponytail: cocokkan teks log; kalau pesannya diubah, angka ini jadi 0 (bukan salah hitung).
        "hidden_flat": _n("SELECT COUNT(*) FROM agent_logs WHERE phase='analyze' "
                          "AND message LIKE '%FLAT multi-hari%'"),
    }


def count_predictions() -> int:
    """Jumlah baris tabel prediksi. Dipakai run_cycle untuk melaporkan berapa baris baru yang
    benar-benar ditulis; menghitung panggilan `_commit` melebihkan angkanya karena duplikat
    dan keputusan yang digerbang tetap terhitung."""
    return get_conn().execute("SELECT COUNT(*) FROM predictions").fetchone()[0]


# ------------------------------------------------------------------ conversations
def save_conversation(ticker: str, analyst: str, trader: str, summary: str,
                      prediction_id: int | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO conversations(ts, ticker, prediction_id, analyst, trader, summary) "
            "VALUES (?,?,?,?,?,?)",
            (now_iso(), ticker, prediction_id, analyst, trader, summary),
        )


def latest_conversation(ticker: str) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM conversations WHERE ticker=? ORDER BY id DESC LIMIT 1", (ticker,)
    ).fetchone()
    return dict(row) if row else None


def recent_conversations(limit: int = 20) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM conversations ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ lessons
def add_lesson(kind: str, lesson: str, *, ticker: str | None = None,
               prediction_id: int | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO lessons(ts, ticker, kind, lesson, prediction_id) VALUES (?,?,?,?,?)",
            (now_iso(), ticker, kind, lesson, prediction_id),
        )


def recent_lessons(limit: int = 15) -> list[dict]:
    # 'agent-note' = memori per-ticker analis (diinject via agent_notes); 'auto-rule' =
    # aturan dinamis evaluator (diinject via active_auto_rules) — keduanya JANGAN dobel di sini.
    rows = get_conn().execute(
        "SELECT * FROM lessons WHERE kind NOT IN ('agent-note','auto-rule') "
        "ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def active_auto_rules(limit: int | None = None) -> list[str]:
    """Aturan dinamis EVALUATOR harian — hanya BATCH TERBARU (satu ts) yang aktif;
    batch baru menggantikan total batch lama (bukan menumpuk)."""
    rows = get_conn().execute(
        "SELECT lesson FROM lessons WHERE kind='auto-rule' AND ts="
        "(SELECT MAX(ts) FROM lessons WHERE kind='auto-rule') ORDER BY id LIMIT ?",
        (limit or config.AUTO_RULES_MAX,)
    ).fetchall()
    return [r["lesson"] for r in rows]


def agent_notes(ticker: str, limit: int = 3) -> list[str]:
    """Catatan yang ditulis AGEN SENDIRI (tool remember) ttg saham ini — memori per-agen,
    diinject lagi saat ia menganalisis saham yang sama (identitas = peran + memorinya)."""
    rows = get_conn().execute(
        "SELECT lesson FROM lessons WHERE kind='agent-note' AND ticker=? "
        "ORDER BY id DESC LIMIT ?", (ticker, limit)
    ).fetchall()
    return [r["lesson"] for r in rows]


# ------------------------------------------------------------------ trades & portfolio
def record_trade(t: dict) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO trades(ts, ticker, side, qty, price, gross, fee, net, reason, prediction_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), t["ticker"], t["side"], t["qty"], t["price"], t["gross"],
             t["fee"], t["net"], t.get("reason"), t.get("prediction_id")),
        )
        return cur.lastrowid


def last_sell_ts(ticker: str) -> str | None:
    """Waktu (ISO) penjualan TERAKHIR ticker — untuk cooldown re-entry (anti-churn)."""
    row = get_conn().execute(
        "SELECT ts FROM trades WHERE ticker=? AND side='SELL' ORDER BY id DESC LIMIT 1",
        (ticker,)).fetchone()
    return row["ts"] if row else None


def last_buy_ts(ticker: str) -> str | None:
    """Waktu (ISO) pembelian TERAKHIR ticker — untuk gerbang 1 entri/hari (anti-churn fee)."""
    row = get_conn().execute(
        "SELECT ts FROM trades WHERE ticker=? AND side='BUY' ORDER BY id DESC LIMIT 1",
        (ticker,)).fetchone()
    return row["ts"] if row else None


def recent_trades(limit: int = 50) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_positions() -> list[dict]:
    rows = get_conn().execute("SELECT * FROM positions WHERE qty > 0 ORDER BY ticker").fetchall()
    return [dict(r) for r in rows]


def get_position(ticker: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()
    return dict(row) if row else None


def upsert_position(ticker: str, qty: float, avg_price: float, opened_at: str) -> None:
    with tx() as conn:
        if qty <= 0:
            conn.execute("DELETE FROM positions WHERE ticker=?", (ticker,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO positions(ticker, qty, avg_price, opened_at) VALUES (?,?,?,?)",
                (ticker, qty, avg_price, opened_at),
            )


def get_cash() -> float:
    row = get_conn().execute(
        "SELECT cash FROM portfolio ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return row["cash"] if row else config.START_CASH


def snapshot_portfolio(cash: float, equity: float) -> dict:
    total = cash + equity
    pnl = total - config.START_CASH
    pnl_pct = pnl / config.START_CASH * 100 if config.START_CASH else 0.0
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO portfolio(ts, cash, equity, total, pnl, pnl_pct) "
            "VALUES (?,?,?,?,?,?)", (now_iso(), cash, equity, total, pnl, pnl_pct),
        )
    return {"cash": cash, "equity": equity, "total": total, "pnl": pnl, "pnl_pct": pnl_pct}


def latest_portfolio() -> dict:
    row = get_conn().execute(
        "SELECT * FROM portfolio ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else {"cash": config.START_CASH, "equity": 0,
                                  "total": config.START_CASH, "pnl": 0, "pnl_pct": 0}


def portfolio_curve(limit: int = 200) -> list[dict]:
    rows = get_conn().execute(
        "SELECT ts, total, pnl_pct FROM portfolio ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in reversed(rows)]
