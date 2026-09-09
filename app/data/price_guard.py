"""Price-guard: refresh HARGA cepat (target PRICE_REFRESH_SEC, default 20 dtk) untuk
seluruh universe ber-quote — tanpa LLM, di thread daemon terpisah dari scheduler.

Desain jujur di atas Yahoo gratis (~700 ticker per sweep):
  - Ambil 2 bar harian terakhir per ticker (payload kecil) via jalur batch yfinance yang
    sama dengan refresh_all — TANPA dependency baru. Fitur teknikal TIDAK dihitung ulang
    di sini (butuh 1y history); price/change/volume di-update, features lama dipertahankan
    (hanya field last/change_pct di dalamnya ikut disegarkan).
  - Hanya ticker yang SUDAH punya quote (hasil refresh_all penuh) yang di-sweep: quote baru
    (dengan fitur lengkap) tetap lahir dari refresh_all; loop ini menjaga harganya segar.
  - Throttle Yahoo (429/kosong) PASTI terjadi pada interval agresif → backoff eksponensial
    otomatis (interval melebar, menyempit lagi saat pulih), TIDAK pernah crash engine.
  - TIDAK ada fallback diam-diam: batch gagal/kosong/throttle & ticker basi (miss beruntun
    >= PRICE_STALE_SWEEPS) dicatat via repo.log (agent='price-guard'); harga last-known
    tetap tersaji dari tabel quotes (tidak pernah dikosongkan).
  - Status ringkas (last_success_ts, throttled_count, interval efektif, dst) diekspos ke
    /api/health lewat status().
"""
from __future__ import annotations

import json
import threading
import time as _time

import yfinance as yf

import config
from app import repo, universe
from app.data import stocks

_CHUNK = 50          # simbol per panggilan yf.download (paralel internal per batch)
_PAUSE = 0.25        # napas antar batch (jangan burst penuh)
_PENALTY_MAX = 30.0  # backoff maks = target * ini (20s -> plafon 10 mnt)

_started = False
_miss: dict[str, int] = {}      # ticker -> sweep beruntun tanpa data (deteksi basi)
_stale_logged: set[str] = set()

# Dibaca /api/health lintas-thread (dict flat + GIL = cukup aman untuk telemetri).
STATUS: dict = {
    "running": False,
    "target_sec": None,          # target interval fase pasar sekarang
    "effective_interval_sec": None,  # interval NYATA sweep terakhir (durasi + backoff)
    "last_sweep_secs": None,
    "last_success_ts": None,     # terakhir kali >=1 harga tersimpan
    "sweeps": 0,
    "expected_last_sweep": 0,
    "updated_last_sweep": 0,
    "throttled_count": 0,        # akumulasi sinyal throttle sejak start
    "error_count": 0,
    "stale_tickers": 0,
    "backoff_penalty": 1.0,
    "market": None,
}


def status() -> dict:
    return dict(STATUS)


def _rate_limited(text: str) -> bool:
    low = text.lower()
    return "429" in low or "too many" in low or "rate" in low


def _yf_throttle_errors() -> int:
    """Hitung error rate-limit yang dicatat yfinance pada download terakhir (best-effort;
    API internal — kalau berubah antar versi, kembali 0 tanpa efek samping)."""
    try:
        from yfinance import shared
        return sum(1 for e in (shared._ERRORS or {}).values() if _rate_limited(str(e)))
    except Exception:  # noqa: BLE001
        return 0


def _save_price(t: str, sub, old: dict | None) -> tuple[bool, bool]:
    """Return (ada_data, tersimpan). Persist hanya update ringan di atas quote lama."""
    try:
        sub = sub.dropna(how="all")
        closes = sub["Close"].dropna()
    except Exception:  # noqa: BLE001
        return False, False
    if closes.empty:
        return False, False
    price = round(float(closes.iloc[-1]), 2)
    if old is None or price < config.MIN_PRICE:
        return True, False           # hidup, tapi bukan urusan loop ini (junk / belum ada quote)
    prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else \
        float(old.get("prev_close") or price)
    change_pct = round((price / prev_close - 1) * 100, 2) if prev_close else 0.0
    try:
        feats = json.loads(old["features_json"]) if old.get("features_json") else {}
    except Exception:  # noqa: BLE001
        feats = {}
    feats["last"] = price
    feats["change_pct"] = change_pct
    last_bar = sub.iloc[-1]
    repo.save_quote(t, {
        "ts": repo.now_iso(),
        "price": price,
        "prev_close": round(prev_close, 2),
        "change_pct": change_pct,
        "volume": float(last_bar.get("Volume") or old.get("volume") or 0),
        "day_high": round(float(last_bar.get("High") or price), 2),
        "day_low": round(float(last_bar.get("Low") or price), 2),
        "features": feats,
    })
    return True, True


def _sweep() -> dict:
    """Satu sweep universe ber-quote. Return statistik untuk monitor/log."""
    quotes = {q["ticker"]: q for q in repo.all_quotes()}
    todo = [t for t in universe.get_universe() if t in quotes]
    stats = {"expected": len(todo), "updated": 0, "throttled": 0,
             "errors": 0, "empty_batches": 0}
    for i in range(0, len(todo), _CHUNK):
        chunk = todo[i:i + _CHUNK]
        symbols = [f"{t}.JK" for t in chunk]
        if i:
            _time.sleep(_PAUSE)
        try:
            data = yf.download(symbols, period="2d", interval="1d", group_by="ticker",
                               auto_adjust=False, threads=True, progress=False)
        except Exception as e:  # noqa: BLE001 — batch gagal total: catat, JANGAN crash
            stats["errors"] += 1
            if _rate_limited(str(e)):
                stats["throttled"] += 1
            continue
        stats["throttled"] += _yf_throttle_errors()
        got = 0
        for t in chunk:
            try:
                sub = data[f"{t}.JK"] if len(symbols) > 1 else data
            except Exception:  # noqa: BLE001
                sub = None
            alive = saved = False
            if sub is not None:
                try:
                    alive, saved = _save_price(t, sub, quotes.get(t))
                except Exception:  # noqa: BLE001 — 1 ticker rusak tak menjatuhkan sweep
                    pass
            if alive:
                got += 1
                _miss.pop(t, None)
            else:
                _miss[t] = _miss.get(t, 0) + 1
            if saved:
                stats["updated"] += 1
        if chunk and got == 0:
            stats["empty_batches"] += 1   # 1 batch penuh kosong = indikasi kuat throttle
    return stats


def run_loop() -> None:
    global _stale_logged
    STATUS["running"] = True
    penalty = 1.0
    last_sig = ""
    last_print_sig = None
    problem_run = 0
    while True:
        phase = stocks.market_phase()
        target = config.PRICE_REFRESH_SEC if phase == "open" \
            else config.PRICE_REFRESH_CLOSED_SEC
        t0 = _time.time()
        try:
            stats = _sweep()
        except Exception as e:  # noqa: BLE001 — loop TIDAK boleh mati
            STATUS["error_count"] += 1
            penalty = min(penalty * 2, _PENALTY_MAX)
            try:
                repo.log("price-guard", "fetch", f"sweep crash (tetap hidup): {e}",
                         level="error")
            except Exception:  # noqa: BLE001
                pass
            _time.sleep(min(target * penalty, 600))
            continue
        dur = _time.time() - t0
        stale = sorted(t for t, m in _miss.items() if m >= config.PRICE_STALE_SWEEPS)
        throttled = stats["throttled"] + stats["empty_batches"]

        if stats["updated"]:
            STATUS["last_success_ts"] = repo.now_iso()
        problems = bool(throttled or stats["errors"]
                        or (stats["expected"] and stats["updated"] == 0))
        if problems:
            penalty = min(max(penalty * 2, 2.0), _PENALTY_MAX)
            problem_run += 1
            total_fail = stats["expected"] and stats["updated"] == 0
            level = "error" if total_fail else "warn"
            sig = f"{level}:{throttled > 0}"
            # Anti-spam: log saat kondisi BERUBAH atau tiap 10 sweep bermasalah beruntun.
            if sig != last_sig or problem_run % 10 == 1:
                msg = (f"harga {stats['updated']}/{stats['expected']} tersimpan; "
                       f"throttle/kosong={throttled}, batch error={stats['errors']}"
                       + (", SEMUA GAGAL — sajikan harga last-known" if total_fail else
                          " — harga yang gagal tetap pakai last-known")
                       + f"; backoff -> interval ~{int(target * penalty)}s")
                repo.log("price-guard", "fetch", msg, level=level,
                         payload={**stats, "stale": len(stale), "penalty": penalty})
            last_sig = sig
        else:
            penalty = max(1.0, penalty / 2)
            problem_run = 0
            if last_sig:
                repo.log("price-guard", "fetch",
                         f"pulih: sweep bersih {stats['updated']}/{stats['expected']} "
                         f"dalam {dur:.1f}s", level="info")
                last_sig = ""

        new_stale = set(stale) - _stale_logged
        if new_stale:
            repo.log("price-guard", "fetch",
                     f"{len(new_stale)} ticker BASI (tanpa harga segar "
                     f">= {config.PRICE_STALE_SWEEPS} sweep): "
                     f"{', '.join(sorted(new_stale)[:15])}"
                     + ("..." if len(new_stale) > 15 else "")
                     + " — dashboard tetap tampilkan harga terakhir yang diketahui",
                     level="warn", payload={"stale_total": len(stale)})
        _stale_logged = set(stale)

        sleep_s = max(1.0, target * penalty - dur)
        eff = dur + sleep_s
        STATUS.update(target_sec=target, effective_interval_sec=round(eff, 1),
                      last_sweep_secs=round(dur, 1), sweeps=STATUS["sweeps"] + 1,
                      expected_last_sweep=stats["expected"],
                      updated_last_sweep=stats["updated"],
                      throttled_count=STATUS["throttled_count"] + throttled,
                      error_count=STATUS["error_count"] + stats["errors"],
                      stale_tickers=len(stale), backoff_penalty=penalty, market=phase)
        # Anti-spam stdout: cetak hanya saat kondisi (throttle/jumlah-stale/fase) BERUBAH, atau
        # denyut tiap 20 sweep — bukan tiap sweep (~35s). Kurangi bising journald drastis tanpa
        # kehilangan sinyal (perubahan langsung tercetak). Status penuh tetap live di /api/health.
        _psig = (throttled > 0, len(stale), phase)
        if _psig != last_print_sig or STATUS["sweeps"] % 20 == 0:
            print(f"[price-guard] {_time.strftime('%H:%M:%S')} sweep#{STATUS['sweeps']}: "
                  f"{stats['updated']}/{stats['expected']} harga dlm {dur:.1f}s "
                  f"(market={phase}, target={target}s, efektif~{eff:.0f}s, "
                  f"throttle={throttled}, stale={len(stale)})", flush=True)
            last_print_sig = _psig
        _time.sleep(sleep_s)


def start_background() -> threading.Thread | None:
    """Mulai loop sebagai daemon thread. Idempotent (aman dipanggil ulang)."""
    global _started
    if _started:
        return None
    _started = True
    t = threading.Thread(target=run_loop, daemon=True, name="price-guard")
    t.start()
    return t
