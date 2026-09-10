"""Uji kandidat gerbang sebelum dipasang.

Ember yang merugi belum tentu layak diblokir: bisa jadi seluruh pasar memang jelek pada
tanggal-tanggal itu. Yang menentukan adalah perbandingan terhadap dasar dari saham likuid
pada tanggal dan horizon yang sama. Metodenya mengikuti basecheck.py, ditambah dasar untuk
klaim FLAT yang tidak ditangani di sana.

    python tools/gate_trial.py
    python tools/gate_trial.py --bets-only
    python tools/gate_trial.py --selftest

Vonis LOLOS hanya kalau batas atas CI95 kelompok yang diblokir ada di bawah nol, dan
kelompok yang disisakan tidak ikut turun. Daya uji terbatas oleh jumlah hari bursa: CI yang
melebar melewati nol berarti belum tahu, bukan tidak ada efek.
"""
from __future__ import annotations

import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import sqlite3  # noqa: E402

import numpy as np  # noqa: E402

import config  # noqa: E402
from app.agents.orchestrator import FLAT_BAND_PCT  # noqa: E402
from app.eval import block_bootstrap  # noqa: E402
from basecheck import THR, _series  # noqa: E402


def matched_base(series, liquid, idx, d: str, h: int, direction: str) -> float | None:
    """Porsi saham likuid yang memenuhi klaim `direction` dari tanggal d sepanjang h sesi.
    UP/DOWN memakai ambang THR yang sama dengan penilaian live; FLAT memakai band FLAT
    (basecheck sengaja tak menangani FLAT, padahal FLAT sepertiga buku)."""
    hit = n = 0
    for tk in liquid:
        i = idx[tk].get(d)
        s = series[tk]
        if i is None or i + h >= len(s):
            continue
        r = (s[i + h][1] - s[i][1]) / s[i][1] * 100
        n += 1
        if direction == "UP":
            hit += r > THR
        elif direction == "DOWN":
            hit += r < -THR
        else:
            hit += abs(r) <= FLAT_BAND_PCT
    return hit / n * 100 if n >= 30 else None


def exante_vol(series_tk, before: str) -> float | None:
    """Stdev return harian 20 sesi SEBELUM tanggal taruhan (bukan gerak yang sudah terjadi)."""
    hist = [c for d, c, _ in series_tk if d < before][-21:]
    if len(hist) < 10:
        return None
    rets = [(hist[i] / hist[i - 1] - 1) * 100 for i in range(1, len(hist)) if hist[i - 1]]
    return st.pstdev(rets) if len(rets) >= 8 else None


def _delta(rows) -> tuple[float, tuple[float, float], int]:
    """Selisih menang vs dasar cocok-tanggal, dirata-rata PER TANGGAL (satuan sama dgn CI)."""
    per_day = defaultdict(list)
    for r in rows:
        per_day[r["d"]].append(r["win"] * 100 - r["base"])
    daily = np.array([st.mean(v) for v in per_day.values()])
    if len(daily) < 10:
        return float(daily.mean()) if len(daily) else float("nan"), (float("nan"),) * 2, len(per_day)
    lo, hi = block_bootstrap(daily, block=5)
    return float(daily.mean()), (lo, hi), len(per_day)


def _report(name: str, blocked: list, kept: list) -> bool:
    print(f"\n=== {name} ===")
    verdict = False
    for label, rows in (("DIBLOKIR", blocked), ("DISISAKAN", kept)):
        if not rows:
            print(f"   {label:10s} kosong")
            continue
        d, (lo, hi), days = _delta(rows)
        win = st.mean(r["win"] * 100 for r in rows)
        base = st.mean(r["base"] for r in rows)
        edge = st.mean(r["edge"] for r in rows)
        ci = "CI95 tak terhitung (<10 hari)" if lo != lo else f"CI95 [{lo:+.1f}, {hi:+.1f}]"
        print(f"   {label:10s} n={len(rows):4d} hari={days:3d}  menang={win:5.1f}%  "
              f"dasar={base:5.1f}%  selisih={d:+5.1f} pp  {ci}  edge={edge:+.2f} pp")
        if label == "DIBLOKIR" and hi == hi and hi < 0:
            verdict = True
    if blocked and kept:
        dk, (lok, hik), _ = _delta(kept)
        db_, _, _ = _delta(blocked)
        print(f"   vonis: {'LOLOS' if verdict else 'BELUM TERBUKTI'} — "
              f"blokir hanya sah bila batas ATAS CI kelompok DIBLOKIR < 0")
        if verdict and hik == hik and hik < 0:
            print("   catatan: sisa buku pun masih di bawah dasarnya sendiri; "
                  "gerbang ini memperbaiki, tapi tak menyelamatkan.")
    return verdict


def main(dbpath: str, bets_only: bool = False) -> None:
    conn = sqlite3.connect(dbpath)
    # Baris "prediksi 1-hari (besok) — forecast." adalah TAMPILAN, dan repo.stats() memang
    # sudah membuangnya dari papan skor (hidden_display_1d). Menguji gerbang atas populasi
    # yang berbeda dari populasi yang dinilai = menjawab pertanyaan yang salah, jadi
    # --bets-only menyamakan populasinya dengan papan skor.
    where = ("where outcome in ('win','loss') and actual_pct is not null"
             + (" and reasoning not like 'prediksi 1-hari%'" if bets_only else ""))
    bets = [dict(zip(("id", "tk", "d", "dir", "h", "outcome", "actual", "entry", "prob",
                      "exp"), b))
            for b in conn.execute(
                "select id, ticker, substr(ts,1,10), direction, horizon_days, outcome, "
                "actual_pct, entry_price, probability, expected_pct from predictions " + where)]
    if not bets:
        raise SystemExit("tak ada taruhan tuntas")
    first = min(b["d"] for b in bets)
    series = _series(conn, f"{int(first[:4]) - 1}{first[4:]}")
    conn.close()
    idx = {tk: {d: i for i, (d, _, _) in enumerate(s)} for tk, s in series.items()}
    liquid = [tk for tk, s in series.items()
              if st.median([t for d, _, t in s if d >= first] or [0]) >= config.MIN_TURNOVER]

    rows, cache, skipped = [], {}, 0
    for b in bets:
        h = int(b["h"] or 1)
        key = (b["d"], h, b["dir"])
        if key not in cache:
            cache[key] = matched_base(series, liquid, idx, b["d"], h, b["dir"])
        if cache[key] is None:
            skipped += 1
            continue
        a = b["actual"]
        rows.append({"d": b["d"], "dir": b["dir"], "h": h, "tk": b["tk"],
                     "win": b["outcome"] == "win", "base": cache[key],
                     "edge": a if b["dir"] == "UP" else -a if b["dir"] == "DOWN" else -abs(a),
                     "vol": exante_vol(series.get(b["tk"], []), b["d"]),
                     "entry": b["entry"] or 0, "prob": b["prob"] or 0,
                     "exp": b["exp"] or 0})
    print(f"{len(rows)} taruhan terpakai dari {len(bets)} ({skipped} tanpa dasar cocok), "
          f"{len(liquid)} saham likuid jadi pembanding")

    def split(pred):
        blocked = [r for r in rows if pred(r)]
        return blocked, [r for r in rows if not pred(r)]

    results = {}
    results["FLAT"] = _report("kandidat 1: buang semua klaim FLAT",
                              *split(lambda r: r["dir"] == "FLAT"))
    results["VOL5"] = _report("kandidat 3: blokir volatilitas ex-ante > 5%",
                              *split(lambda r: (r["vol"] or 0) > 5))
    results["PENNY"] = _report("kandidat 4: blokir harga masuk < Rp100",
                               *split(lambda r: 0 < r["entry"] < 100))
    results["VOL5_ARAH"] = _report("kandidat 3b: vol > 5% TAPI hanya klaim berarah (UP/DOWN)",
                                   *split(lambda r: (r["vol"] or 0) > 5 and r["dir"] != "FLAT"))
    results["FLAT_LIAR"] = _report("kandidat 1b: FLAT hanya di saham liar (vol > 5%)",
                                   *split(lambda r: r["dir"] == "FLAT" and (r["vol"] or 0) > 5))
    results["KLAIM_BESAR"] = _report("kandidat 5: blokir klaim >= 3% (paling berani)",
                                     *split(lambda r: abs(r["exp"] or 0) >= 3))
    results["KLAIM_KECIL"] = _report("kandidat 6: blokir klaim < 0,75% (paling kecil)",
                                     *split(lambda r: 0 < abs(r["exp"] or 0) < 0.75))
    results["FLAT_TENANG"] = _report("kandidat 1c: FLAT di saham tenang (vol <= 5%)",
                                     *split(lambda r: r["dir"] == "FLAT"
                                            and (r["vol"] or 0) <= 5))
    print("\nringkas:", ", ".join(f"{k}={'LOLOS' if v else 'belum'}" for k, v in results.items()))


def selftest() -> None:
    s = [("2026-01-01", 100.0, 1e12), ("2026-01-02", 110.0, 1e12), ("2026-01-03", 90.0, 1e12)]
    series = {f"T{i}": s for i in range(40)}
    idx = {tk: {d: i for i, (d, _, _) in enumerate(v)} for tk, v in series.items()}
    liq = list(series)
    up = matched_base(series, liq, idx, "2026-01-01", 1, "UP")
    dn = matched_base(series, liq, idx, "2026-01-01", 1, "DOWN")
    fl = matched_base(series, liq, idx, "2026-01-01", 1, "FLAT")
    assert up == 100.0 and dn == 0.0 and fl == 0.0, (up, dn, fl)
    assert matched_base(series, liq[:5], idx, "2026-01-01", 1, "UP") is None  # < 30 saham
    flat_ok = matched_base({"A": [("d1", 100.0, 1e12), ("d2", 100.5, 1e12)]} | {},
                           ["A"], {"A": {"d1": 0, "d2": 1}}, "d1", 1, "FLAT")
    assert flat_ok is None                                    # 1 saham saja -> tak cukup
    naik = [("2026-01-%02d" % d, 100.0 + d, 0.0) for d in range(1, 21)]
    assert exante_vol(naik, "2026-01-21") is not None
    assert exante_vol(naik, "2026-01-03") is None
    r = [{"d": "2026-01-01", "win": True, "base": 40.0}] * 3
    m, ci, days = _delta(r)
    assert round(m, 1) == 60.0 and days == 1 and ci[0] != ci[0]   # <10 hari -> CI NaN
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main(str(config.DB_PATH), bets_only="--bets-only" in sys.argv)
