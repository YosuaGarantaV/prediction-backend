"""Backtest aturan prediksi atas data historis — DIJALANKAN OLEH SCRIPT.

Walk-forward: di tiap hari historis, hitung fitur HANYA dari data sampai hari itu
(tanpa look-ahead), terapkan aturan heuristik, lalu cek hasil aktual `horizon` hari
ke depan. Mengukur:
  - win-rate keseluruhan & per-arah (UP/DOWN)
  - split IN-SAMPLE vs OUT-OF-SAMPLE → kalau OOS jatuh jauh = tanda OVERFIT
  - kalibrasi: probabilitas yang diklaim vs realisasi (Brier score)

Dipakai untuk (a) validasi aturan sebelum percaya prediksi live, (b) memberi agen
angka hit-rate historis yang TERKALIBRASI (lihat app/agents/skill.py) tanpa overfit.

CLI:  python -m app.backtest
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

import config
from app import repo
from app.agents.heuristic import score_signals, score_to_view
from app.data.stocks import compute_features

RESULT_FILE = config.DATA_DIR / "backtest.json"
AB_FILE = config.DATA_DIR / "backtest_foreign_ab.json"
AB_MIN = 40  # minimal sampel resolved BER-foreign_pressure sebelum hasil dianggap bermakna


def backtest_foreign_ab(min_samples: int = AB_MIN) -> dict:
    """Evaluator item-3 NEXT_UPDATES: apakah foreign_pressure sbg fitur menaikkan OOS win-rate?

    Arus asing per-ticker TAK diarsipkan historis; satu-satunya sumber = foreign_pressure yang
    direkam `repo.save_prediction` ke factors_json (collector). Jadi backtest ini HANYA bermakna
    setelah cukup prediksi resolved terkumpul DENGAN nilai itu. Kurang dari `min_samples` →
    TIDAK menulis artefak (gate item-3 di check_updates.py tetap HOLD). Bukti, bukan tebakan.

    A/B (proxy jujur, bukan re-scoring penuh): OOS = paruh akhir kronologis. `oos_without` =
    win-rate OOS keseluruhan (baseline). `oos_with` = win-rate OOS pada sinyal yang arus-asingnya
    SEARAH arah prediksi (UP&fp>0 / DOWN&fp<0). Fitur berguna kalau oos_with > oos_without.
    """
    rows = repo.get_conn().execute(
        "SELECT direction, outcome, factors_json FROM predictions "
        "WHERE status='resolved' AND outcome IS NOT NULL AND factors_json IS NOT NULL ORDER BY id"
    ).fetchall()
    recs: list[tuple[int, bool]] = []
    for direction, outcome, fj in rows:
        try:
            fp = (json.loads(fj) or {}).get("foreign_pressure")
        except Exception:  # noqa: BLE001
            fp = None
        if fp is None:
            continue
        agree = (direction == "UP" and fp > 0) or (direction == "DOWN" and fp < 0)
        recs.append((1 if outcome == "win" else 0, agree))
    n = len(recs)
    if n < min_samples:
        return {"status": "insufficient", "n": n, "need": min_samples,
                "note": f"butuh >={min_samples} prediksi resolved ber-foreign_pressure; baru {n}. "
                        "Collector aktif di save_prediction; kumpulkan dulu (jangan tulis artefak)."}
    oos = recs[n // 2:]
    def _wr(rs: list[tuple[int, bool]]) -> float:
        return round(100 * sum(w for w, _ in rs) / len(rs), 1) if rs else 0.0
    with_feat = [r for r in oos if r[1]]
    out = {"status": "ok", "ts": datetime.now(timezone.utc).isoformat(), "n": n,
           "oos_with": _wr(with_feat), "oos_without": _wr(oos),
           "n_with": len(with_feat), "n_oos": len(oos)}
    AB_FILE.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def _ticker_df(ticker: str) -> pd.DataFrame | None:
    rows = repo.price_history(ticker, 400)
    if len(rows) < 60:
        return None
    df = pd.DataFrame(rows)
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                            "close": "Close", "volume": "Volume"})
    return df


def _backtest_ticker(ticker: str, horizon: int) -> list[dict]:
    df = _ticker_df(ticker)
    if df is None:
        return []
    out = []
    n = len(df)
    # mulai dari index 30 (cukup untuk indikator), berhenti horizon hari sebelum akhir
    for i in range(30, n - horizon):
        feats = compute_features(df.iloc[: i + 1])
        if not feats:
            continue
        score, _ = score_signals(feats, news=0)  # backtest = teknikal murni (no look-ahead)
        direction, prob, expected, _ = score_to_view(score)
        if direction == "FLAT":
            continue
        entry = float(df["Close"].iloc[i])
        exit_ = float(df["Close"].iloc[i + horizon])
        if entry <= 0:
            continue
        actual = (exit_ / entry - 1) * 100
        win = (direction == "UP" and actual > 0.5) or (direction == "DOWN" and actual < -0.5)
        out.append({"i": i, "direction": direction, "prob": prob,
                    "actual": actual, "win": win})
    return out


def _compute(horizon: int, max_tickers: int, liquid_only: bool | None = None) -> dict:
    if liquid_only is None:
        liquid_only = config.BACKTEST_LIQUID_ONLY   # default: universe likuid (populasi tradeable)
    tickers = repo.quote_tickers()
    if liquid_only:                      # audit 2026-07-21: samakan populasi backtest dgn universe
        keep = repo.liquid_tickers()     # yang benar-benar bisa ditradingkan (model dilatih di sini).
        tickers = [t for t in tickers if t in keep]  # buang illikuid/beku (bukan sinyal pasar)
    tickers = tickers[:max_tickers]
    recs: list[dict] = []
    for t in tickers:
        try:
            recs.extend(_backtest_ticker(t, horizon))
        except Exception:  # noqa: BLE001
            continue
    return _aggregate(recs, horizon, len(tickers))


def run_backtest(horizon: int = 3, max_tickers: int = 150) -> dict:
    """Backtest 1 horizon (kompatibilitas) + tulis hasil."""
    repo.log("engine", "resolve", f"backtest mulai: horizon {horizon}h")
    result = _compute(horizon, max_tickers)
    RESULT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    repo.log("engine", "resolve",
             f"backtest selesai: {result['total']} sinyal, win-rate {result['win_rate']}% "
             f"(OOS {result['oos_win_rate']}%)")
    return result


def run_horizons(horizons=(1, 2, 3, 5), max_tickers: int = 120) -> dict:
    """Uji beberapa horizon, pilih EDGE terbaik di atas base rate pasar.

    Dipilih dari `edge_oos`, BUKAN `oos_win_rate` mentah: band +/-0,5% memakan 27,8% gerak
    di h=1 tapi cuma 14,6% di h=5, jadi win-rate mentah selalu menanjak dengan horizon walau
    sinyalnya sama kuat. Memilih dari angka mentah = memilih artefak scoring, dan itulah yang
    terjadi sampai 2026-07-29 (h=5 "menang" 46,6% padahal edge-nya paling tipis).
    """
    repo.log("engine", "resolve", f"backtest multi-horizon {list(horizons)} mulai…")
    runs = []
    for h in horizons:
        r = _compute(h, max_tickers)
        if r.get("total"):
            runs.append(r)
    if not runs:
        return run_backtest(3, max_tickers)
    best = max(runs, key=lambda r: r["edge_oos"])
    best["horizons"] = [{"horizon": r["horizon"], "oos": r["oos_win_rate"],
                         "baseline": r["baseline"], "edge": r["edge_oos"],
                         "win": r["win_rate"], "n": r["total"]} for r in runs]
    RESULT_FILE.write_text(json.dumps(best, indent=2), encoding="utf-8")
    repo.log("engine", "resolve",
             f"backtest multi-horizon selesai: horizon terbaik {best['horizon']}h "
             f"edge {best['edge_oos']:+.1f} pp (OOS {best['oos_win_rate']}% vs base "
             f"{best['baseline']}%) | " +
             ", ".join(f"{r['horizon']}h:{r['edge_oos']:+.1f}pp" for r in runs))
    return best


def _wr(recs: list[dict]) -> float:
    if not recs:
        return 0.0
    return round(sum(1 for r in recs if r["win"]) / len(recs) * 100, 1)


def _aggregate(recs: list[dict], horizon: int, n_tickers: int) -> dict:
    total = len(recs)
    if total == 0:
        return {"ts": datetime.now(timezone.utc).isoformat(), "horizon": horizon,
                "tickers": n_tickers, "total": 0, "win_rate": 0.0,
                "oos_win_rate": 0.0, "note": "data historis belum cukup"}

    ups = [r for r in recs if r["direction"] == "UP"]
    downs = [r for r in recs if r["direction"] == "DOWN"]

    # Split in-sample (paruh awal) vs out-of-sample (paruh akhir) berbasis posisi i.
    recs_sorted = sorted(recs, key=lambda r: r["i"])
    mid = len(recs_sorted) // 2
    is_recs, oos_recs = recs_sorted[:mid], recs_sorted[mid:]

    # Kalibrasi: bucket probabilitas vs realisasi
    buckets = []
    for lo, hi in [(50, 60), (60, 70), (70, 101)]:
        b = [r for r in recs if lo <= r["prob"] < hi]
        buckets.append({"range": f"{lo}-{hi if hi <= 100 else 100}",
                        "n": len(b), "claimed": round((lo + hi) / 2, 0),
                        "realized": _wr(b)})
    # Brier score (kalibrasi probabilistik; makin kecil makin baik)
    brier = round(sum((r["prob"] / 100 - (1 if r["win"] else 0)) ** 2
                      for r in recs) / total, 3)

    is_wr, oos_wr = _wr(is_recs), _wr(oos_recs)
    overfit_gap = round(is_wr - oos_wr, 1)
    base = market_baseline(horizon, len(ups), len(downs))
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "horizon": horizon, "tickers": n_tickers, "total": total,
        "win_rate": _wr(recs),
        "up_win_rate": _wr(ups), "up_n": len(ups),
        "down_win_rate": _wr(downs), "down_n": len(downs),
        "is_win_rate": is_wr, "oos_win_rate": oos_wr, "overfit_gap": overfit_gap,
        "brier": brier,
        "baseline": base, "edge_oos": round(oos_wr - base, 1),
        "calibration": buckets,
        "verdict": _verdict(oos_wr, overfit_gap, base),
    }


def market_baseline(horizon: int, up_n: int, down_n: int) -> float:
    """Base rate PASAR untuk komposisi arah backtest — pembanding yang benar, bukan 50%.

    `win` di sini menuntut |gerak| > 0,5% ke arah yang diprediksi (lihat `_backtest_ticker`),
    jadi gerak di dalam band dihitung KALAH untuk UP maupun DOWN. Koin adil pun mentok di
    ~43% (h=5), bukan 50%. Membandingkan ke 50 membuat OOS 46,6% divonis "di bawah acak"
    padahal itu +3,8 pp DI ATAS pasar — bug vonis, bukan model lemah (audit 2026-07-29).
    """
    from app.eval import baseline_for      # lazy: eval menarik numpy/pandas
    if up_n + down_n == 0:
        return 50.0
    return round((up_n * baseline_for("UP", horizon) + down_n * baseline_for("DOWN", horizon))
                 / (up_n + down_n), 1)


def _verdict(oos: float, gap: float, base: float) -> str:
    edge = round(oos - base, 1)
    if edge < 0:
        return (f"LEMAH: OOS {oos}% di bawah base rate pasar {base}% ({edge:+.1f} pp) — "
                f"aturan belum andal, perlu revisi.")
    if gap > 10:
        return "OVERFIT: in-sample jauh di atas out-of-sample — jangan terlalu percaya."
    if edge >= 5:
        return f"BAIK: OOS {oos}% = {edge:+.1f} pp di atas base rate pasar {base}%."
    return (f"SEDANG: OOS {oos}% = {edge:+.1f} pp di atas base rate pasar {base}% — titik "
            f"estimasi positif, BELUM tentu signifikan (unit bukti = tanggal, bukan baris).")


def load_result() -> dict:
    if RESULT_FILE.exists():
        try:
            return json.loads(RESULT_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from app import db
    db.init_db()
    r = run_backtest()
    print(json.dumps(r, indent=2, ensure_ascii=False))
