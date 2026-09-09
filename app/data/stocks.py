"""Ambil harga saham IDX (yfinance, ticker .JK) + hitung indikator teknikal."""
from __future__ import annotations

import logging
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timezone, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

import config
from app import repo

# Bungkam spam "possibly delisted; no price data" dari yfinance (kita tangani sendiri via daftar-mati).
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# Worker paralel untuk refresh harga (jangan terlalu besar agar tak kena rate-limit).
PRICE_WORKERS = 8

WIB = timezone(timedelta(hours=7))


# ---------------------------------------------------------------- market hours
def market_is_open(now: datetime | None = None) -> bool:
    now = (now or datetime.now(timezone.utc)).astimezone(WIB)
    if now.weekday() >= 5:  # Sabtu/Minggu
        return False
    t = now.time()
    o = time(*config.MARKET_OPEN)
    lunch = time(*config.MARKET_LUNCH)
    resume = time(*config.MARKET_RESUME)
    close = time(*config.MARKET_CLOSE)
    return (o <= t < lunch) or (resume <= t < close)


def market_phase(now: datetime | None = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(WIB)
    if now.weekday() >= 5:
        return "weekend"
    t = now.time()
    if t < time(*config.MARKET_OPEN):
        return "pre-open"
    if market_is_open(now):
        return "open"
    if time(*config.MARKET_LUNCH) <= t < time(*config.MARKET_RESUME):
        return "lunch-break"
    return "closed"


# ---------------------------------------------------------------- indicators
def _rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff().dropna()
    if len(delta) < period:
        return 50.0
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return float(round(val, 1)) if pd.notna(val) else 50.0


def _consecutive_down(closes: pd.Series) -> int:
    """Berapa hari berturut-turut turun (untuk aturan 'drop 3 hari -> rebound')."""
    diffs = closes.diff().dropna()
    n = 0
    for d in reversed(diffs.tolist()):
        if d < 0:
            n += 1
        else:
            break
    return n


def _consecutive_up(closes: pd.Series) -> int:
    diffs = closes.diff().dropna()
    n = 0
    for d in reversed(diffs.tolist()):
        if d > 0:
            n += 1
        else:
            break
    return n


def compute_features(df: pd.DataFrame) -> dict:
    """Hitung fitur teknikal dari dataframe OHLCV harian."""
    closes = df["Close"].dropna()
    # IPO baru (1-4 hari trading) TETAP dapat fitur (degraded) — dulu return {} membuat
    # saham baru listing tak pernah punya quote → tak muncul di chart & scanner justru di
    # jendela ARA/ARB paling informatif. Semua indikator di bawah sudah length-guarded.
    if len(closes) < 1:
        return {}
    last = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) >= 2 else last
    sma5 = float(closes.tail(5).mean())
    sma20 = float(closes.tail(20).mean()) if len(closes) >= 20 else sma5
    vol = df["Volume"].dropna()
    avg_vol = float(vol.tail(20).mean()) if len(vol) >= 5 else float(vol.mean() or 0)
    last_vol = float(vol.iloc[-1]) if len(vol) else 0.0
    drop3 = float((last / float(closes.iloc[-4]) - 1) * 100) if len(closes) >= 4 else 0.0
    hi52 = float(closes.tail(252).max())
    lo52 = float(closes.tail(252).min())
    daily_ret = closes.pct_change().dropna()
    volatility = float(daily_ret.tail(20).std() * 100) if len(daily_ret) >= 5 else 0.0

    # MACD (12,26,9)
    macd_hist = 0.0
    if len(closes) >= 26:
        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        macd_hist = float((macd - signal).iloc[-1])

    # Bollinger %b (20,2): 0=band bawah (oversold), 1=band atas (overbought)
    pct_b = 0.5
    if len(closes) >= 20:
        m = closes.tail(20).mean()
        sd = closes.tail(20).std()
        if sd > 0:
            pct_b = float((last - (m - 2 * sd)) / (4 * sd))

    # Momentum 10 hari (rate of change)
    mom10 = float((last / float(closes.iloc[-11]) - 1) * 100) if len(closes) >= 11 else 0.0

    # Support/Resistance (20 hari)
    lo20 = float(closes.tail(20).min())
    hi20 = float(closes.tail(20).max())
    dist_support = (last / lo20 - 1) * 100 if lo20 else 0.0      # ~0 = di support
    dist_resist = (last / hi20 - 1) * 100 if hi20 else 0.0       # ~0 = di resistance
    near_support = dist_support <= 3.0
    near_resistance = dist_resist >= -3.0

    # Divergence harga vs RSI (10 hari)
    bull_div = bear_div = False
    if len(closes) >= 25:
        rsi_now = _rsi(closes)
        rsi_prev = _rsi(closes.iloc[:-10])
        price_chg10 = last / float(closes.iloc[-11]) - 1
        if price_chg10 < -0.01 and rsi_now > rsi_prev:   # harga turun, RSI naik
            bull_div = True
        elif price_chg10 > 0.01 and rsi_now < rsi_prev:  # harga naik, RSI turun
            bear_div = True

    # Streak ARA/ARB (gerak harian mendekati batas auto-reject ~ proxy 19%)
    rets5 = (closes.pct_change() * 100).dropna().tail(5)
    ara_days = int((rets5 >= 19).sum())
    arb_days = int((rets5 <= -19).sum())

    return {
        "last": round(last, 2),
        "change_pct": round((last / prev - 1) * 100, 2) if prev else 0.0,
        "rsi14": _rsi(closes),
        "sma5": round(sma5, 2),
        "sma20": round(sma20, 2),
        "above_sma20": last > sma20,
        "consec_down": _consecutive_down(closes),
        "consec_up": _consecutive_up(closes),
        "cum_change_3d": round(drop3, 2),
        "vol_vs_avg": round(last_vol / avg_vol, 2) if avg_vol else 1.0,
        "pct_from_52w_high": round((last / hi52 - 1) * 100, 1) if hi52 else 0.0,
        "pct_from_52w_low": round((last / lo52 - 1) * 100, 1) if lo52 else 0.0,
        "volatility_20d": round(volatility, 2),
        "macd_hist": round(macd_hist, 2),
        "macd_pos": macd_hist > 0,
        "bollinger_pct_b": round(pct_b, 2),
        "momentum_10d": round(mom10, 2),
        "dist_to_support": round(dist_support, 1),
        "dist_to_resistance": round(dist_resist, 1),
        "near_support": near_support,
        "near_resistance": near_resistance,
        "bull_divergence": bull_div,
        "bear_divergence": bear_div,
        "ara_days_5": ara_days,
        "arb_days_5": arb_days,
        # Umur listing (hari trading di data) — <=20 = IPO baru: fitur ber-window belum
        # matang & pola ARA-streak→distribusi (Aturan 75) berlaku. Dipakai factors/heuristik.
        "days_listed": int(len(closes)),
    }


# ---------------------------------------------------------------- fetch
def fetch_ticker(symbol: str) -> dict | None:
    """Ambil 1 saham IDX, simpan harga + quote + fitur. symbol tanpa .JK."""
    yf_sym = f"{symbol}.JK"
    tkr = yf.Ticker(yf_sym)
    try:
        df = tkr.history(period="1y", interval="1d", auto_adjust=False)
    except Exception as e:  # noqa: BLE001
        repo.log("engine", "fetch", f"gagal ambil {symbol}: {e}", level="error", ticker=symbol)
        return None
    if df is None or df.empty:
        return None

    rows = []
    for ts, r in df.tail(260).iterrows():
        rows.append({
            "ts": pd.Timestamp(ts).isoformat(),
            "open": float(r["Open"]), "high": float(r["High"]),
            "low": float(r["Low"]), "close": float(r["Close"]),
            "volume": float(r["Volume"]),
        })
    repo.save_prices(symbol, rows)

    feats = compute_features(df)
    if not feats:
        return None

    # Harga "real": pakai fast_info (last price + previous close) yang lebih
    # mutakhir daripada close harian terakhir. Fallback ke data harian.
    price = feats["last"]
    prev_close = float(df["Close"].iloc[-2]) if len(df) >= 2 else price
    day_high = float(df["High"].iloc[-1])
    day_low = float(df["Low"].iloc[-1])
    last_vol = float(df["Volume"].iloc[-1])
    try:
        fi = tkr.fast_info
        lp = fi.get("lastPrice") if hasattr(fi, "get") else getattr(fi, "last_price", None)
        pc = fi.get("previousClose") if hasattr(fi, "get") else getattr(fi, "previous_close", None)
        if lp and lp > 0:
            price = float(lp)
        if pc and pc > 0:
            prev_close = float(pc)
        dh = getattr(fi, "day_high", None) or (fi.get("dayHigh") if hasattr(fi, "get") else None)
        dl = getattr(fi, "day_low", None) or (fi.get("dayLow") if hasattr(fi, "get") else None)
        if dh:
            day_high = float(dh)
        if dl:
            day_low = float(dl)
    except Exception:  # noqa: BLE001 — fast_info kadang gagal; pakai data harian
        pass

    price = round(price, 2)
    if price < config.MIN_PRICE:  # abaikan gocap/junk
        return None
    change_pct = round((price / prev_close - 1) * 100, 2) if prev_close else feats["change_pct"]
    feats["last"] = price
    feats["change_pct"] = change_pct
    quote = {
        "ts": repo.now_iso(),
        "price": price,
        "prev_close": round(prev_close, 2),
        "change_pct": change_pct,
        "volume": last_vol,
        "day_high": round(day_high, 2),
        "day_low": round(day_low, 2),
        "features": feats,
    }
    repo.save_quote(symbol, quote)
    return quote


def _persist_df(symbol: str, df: pd.DataFrame) -> dict | None:
    """Simpan harga + quote dari dataframe OHLCV (dipakai batch download universe).
    Harga = close terakhir (akurat saat tutup; intraday delay ~15 mnt)."""
    df = df.dropna(how="all")
    if df is None or df.empty or "Close" not in df:
        return None
    feats = compute_features(df)
    if not feats:
        return None
    price = round(feats["last"], 2)
    if price < config.MIN_PRICE:  # filter gocap/junk
        return None
    rows = [{
        "ts": pd.Timestamp(ts).isoformat(),
        "open": float(r["Open"]), "high": float(r["High"]),
        "low": float(r["Low"]), "close": float(r["Close"]),
        "volume": float(r["Volume"]),
    } for ts, r in df.tail(260).iterrows() if pd.notna(r["Close"])]
    repo.save_prices(symbol, rows)
    quote = {
        "ts": repo.now_iso(), "price": price,
        "prev_close": round(float(df["Close"].iloc[-2]), 2) if len(df) >= 2 else price,
        "change_pct": feats["change_pct"],
        "volume": float(df["Volume"].iloc[-1]),
        "day_high": float(df["High"].iloc[-1]), "day_low": float(df["Low"].iloc[-1]),
        "features": feats,
    }
    repo.save_quote(symbol, quote)
    return quote


def refresh_all() -> int:
    """Refresh SELURUH universe via batch download (skala ratusan saham, hemat request).
    Saham < MIN_PRICE otomatis dilewati."""
    from app import universe
    tickers = universe.get_universe()
    n = 0
    missed: set[str] = set()   # tanpa data sama sekali → kandidat delisted
    alive: set[str] = set()    # dapat data (meski difilter gocap) → hidup
    # Chunk lebih kecil + jeda → turunkan tekanan curl_cffi (penyebab crash native exit-4).
    CHUNK = 30
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        symbols = [f"{t}.JK" for t in chunk]
        if i:
            _time.sleep(0.4)  # ponytail: napas antar-batch; supervisor serve.py tangani sisa crash
        try:
            # threads=False: hindari crash native curl_cffi di Windows saat beban tinggi
            # (penyebab proses mati exit-4). ponytail: stabil > sedikit lebih lambat.
            data = yf.download(symbols, period="1y", interval="1d", group_by="ticker",
                               auto_adjust=False, threads=False, progress=False)
        except Exception as e:  # noqa: BLE001
            repo.log("engine", "fetch", f"batch gagal ({chunk[0]}..): {e}", level="warn")
            continue  # batch gagal (jaringan) → JANGAN tandai mati
        for t in chunk:
            try:
                sub = data[f"{t}.JK"] if len(symbols) > 1 else data
            except Exception:  # noqa: BLE001
                sub = None
            if sub is None or sub.dropna(how="all").empty:
                missed.add(t)   # delisted/suspend: tak ada data harga
                continue
            alive.add(t)
            try:
                if _persist_df(t, sub):
                    n += 1
            except Exception:  # noqa: BLE001
                continue
    universe.update_dead(missed, alive)  # auto-skip yang mati di siklus berikutnya
    repo.log("engine", "fetch", f"refresh harga selesai: {n}/{len(tickers)} saham "
             f"(>= Rp{int(config.MIN_PRICE)}, {len(missed)} tanpa data, pasar: {market_phase()})")
    return n


HOT_MAX = 45  # batas hot-set: cukup untuk yang kritis, kecil agar tak spam/crash


def hot_tickers() -> list[str]:
    """Saham KRITIS yang harus selalu live: posisi dipegang + prediksi terbuka + big-cap asing.
    Urut prioritas (posisi dulu) lalu dipotong HOT_MAX."""
    from app.data.flow import FOREIGN_FAVORITES
    ordered = ([p["ticker"] for p in repo.get_positions()]
               + repo.open_prediction_tickers()
               + sorted(FOREIGN_FAVORITES))
    return list(dict.fromkeys(ordered))[:HOT_MAX]


def refresh_hot() -> int:
    """Refresh harga CEPAT hanya untuk hot-set → 'live' tanpa membombardir yfinance.
    Hanya saat pasar BUKA (di luar jam harga beku, tak perlu). 1 batch kecil = aman dari crash."""
    if not market_is_open():
        return 0
    tickers = hot_tickers()
    if not tickers:
        return 0
    symbols = [f"{t}.JK" for t in tickers]
    try:
        data = yf.download(symbols, period="6mo", interval="1d", group_by="ticker",
                           auto_adjust=False, threads=False, progress=False)
    except Exception as e:  # noqa: BLE001
        repo.log("engine", "fetch", f"hot refresh gagal: {e}", level="warn")
        return 0
    n = 0
    for t in tickers:
        try:
            sub = data[f"{t}.JK"] if len(symbols) > 1 else data
            if sub is None or sub.dropna(how="all").empty:
                continue
            if _persist_df(t, sub):
                n += 1
        except Exception:  # noqa: BLE001
            continue
    repo.log("engine", "fetch", f"hot refresh: {n}/{len(tickers)} saham live (pasar buka)")
    return n


def fetch_indices() -> None:
    """Simpan riwayat indeks (IHSG dll) ke tabel prices agar bisa di-chart."""
    for name, sym in config.INDICES.items():
        try:
            df = yf.Ticker(sym).history(period="6mo", interval="1d")
            if df is None or df.empty:
                continue
            rows = [{
                "ts": pd.Timestamp(ts).isoformat(),
                "open": float(r["Open"]), "high": float(r["High"]),
                "low": float(r["Low"]), "close": float(r["Close"]),
                "volume": float(r["Volume"]),
            } for ts, r in df.tail(180).iterrows()]
            repo.save_prices(name, rows)
        except Exception:  # noqa: BLE001
            continue


def _quote_latest(sym: str) -> tuple[float, float] | None:
    """(harga_terkini, close_sebelumnya) — fast_info (quote REAL-TIME) dulu, fallback
    daily history yang di-dropna. BUG NYATA 2026-07-02: history(period='5d') Yahoo kadang
    mengembalikan window BASI (IHSG berhenti di close 2 hari lalu → dashboard tampil
    -3.05% kemarin seolah hari ini) dan Close NaN (tersimpan NULL). Quote live + dropna
    + tolak NaN menutup keduanya."""
    t = yf.Ticker(sym)
    try:
        fi = t.fast_info
        last, prev = float(fi.last_price), float(fi.previous_close)
        if last == last and prev == prev and last > 0 and prev > 0:  # x==x → bukan NaN
            return last, prev
    except Exception:  # noqa: BLE001
        pass
    try:
        df = t.history(period="1mo", interval="1d")
        closes = df["Close"].dropna() if df is not None and not df.empty else None
        if closes is not None and len(closes) >= 2:
            return float(closes.iloc[-1]), float(closes.iloc[-2])
    except Exception:  # noqa: BLE001
        pass
    return None


_INTRADAY_CACHE: dict = {"t": 0.0, "data": None}


def ihsg_intraday() -> dict:
    """Bar 1-menit IHSG sesi bursa terakhir — untuk grafik live di dashboard.
    Cache singkat: banyak tab dashboard tidak boleh membombardir Yahoo."""
    ttl = 55 if market_is_open() else 600
    if _INTRADAY_CACHE["data"] and _time.time() - _INTRADAY_CACHE["t"] < ttl:
        return _INTRADAY_CACHE["data"]
    points: list[dict] = []
    prev_close = None
    try:
        tkr = yf.Ticker("^JKSE")
        df = tkr.history(period="1d", interval="1m")
        if df is not None and not df.empty:
            closes = df["Close"].dropna()
            points = [{"ts": pd.Timestamp(ts).isoformat(), "price": round(float(v), 2)}
                      for ts, v in closes.items()]
        pc = getattr(tkr.fast_info, "previous_close", None)
        if pc and pc == pc:  # x==x → bukan NaN
            prev_close = round(float(pc), 2)
    except Exception:  # noqa: BLE001 — gagal fetch → pakai cache lama di bawah
        pass
    if not points:  # gagal → pertahankan data lama (jangan kosongkan grafik)
        return _INTRADAY_CACHE["data"] or {"points": [], "prev_close": None, "market_open": market_is_open()}
    data = {"points": points, "prev_close": prev_close, "market_open": market_is_open()}
    _INTRADAY_CACHE.update(t=_time.time(), data=data)
    return data


def refresh_macro() -> None:
    """Ambil sinyal makro global (minyak, emas, USD/IDR, indeks) — quote terkini, bukan bar basi."""
    for name, sym in config.MACRO_SIGNALS.items():
        q = _quote_latest(sym)
        if not q:
            continue  # gagal → PERTAHANKAN nilai lama (jangan timpa dgn kosong/NaN)
        last, prev = q
        chg = (last / prev - 1) * 100 if prev else 0.0
        repo.save_macro(name, sym, round(last, 2), round(chg, 2))
    repo.log("engine", "fetch", "refresh sinyal makro global selesai")
