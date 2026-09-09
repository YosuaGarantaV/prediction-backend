"""Tingkat dasar COCOK-TANGGAL vs tabel statis `app.eval.BASELINE_WINRATE`.

Bab IV membandingkan win-rate taruhan terhadap tabel seluruh riwayat, sedangkan taruhan
hanya terjadi pada satu rezim menguat. Tabel statis benar sebagai gerbang produksi
(konservatif, tak mengejar rezim) tapi salah sebagai pembanding ilmiah: ia mengukur pasar
lain. Skrip ini menghitung dasar dari saham dan tanggal yang PERSIS dipakai taruhan.

  python basecheck.py [db] [--from YYYY-MM-DD] [--until YYYY-MM-DD] [--cut YYYY-MM-DD]
  python basecheck.py --self-check

`--from/--until` membatasi TANGGAL PREDIKSI, `--cut` membatasi tanggal penilaian, supaya
populasi bisa dibuat persis sama dengan yang dilaporkan naskah (snapshot punya batas waktu).
"""
from __future__ import annotations

import collections
import os
import sqlite3
import statistics
import sys

import numpy as np

import config
from app.eval import baseline_for, block_bootstrap

THR = 0.5  # ambang menang |gerak| %, sama dengan scoring live


def _series(conn: sqlite3.Connection, since: str) -> dict[str, list[tuple[str, float, float]]]:
    """{ticker: [(tanggal, close, turnover)]} terurut, satu baris per tanggal."""
    by: dict[str, dict[str, tuple[float, float]]] = collections.defaultdict(dict)
    for tk, d, close, vol in conn.execute(
        "select ticker, substr(ts,1,10), close, volume from prices where ts >= ?", (since,)
    ):
        if close:
            by[tk][d] = (float(close), float(close) * float(vol or 0))
    return {tk: [(d, *v) for d, v in sorted(days.items())] for tk, days in by.items()}


def _matched_base(series, liquid, idx, d: str, h: int, direction: str) -> float | None:
    """Porsi saham likuid yang bergerak >THR ke arah `direction` dari tanggal d sepanjang h."""
    hit = n = 0
    for tk in liquid:
        i = idx[tk].get(d)
        s = series[tk]
        if i is None or i + h >= len(s):
            continue
        r = (s[i + h][1] - s[i][1]) / s[i][1] * 100
        n += 1
        hit += (r > THR) if direction == "UP" else (r < -THR)
    return hit / n * 100 if n >= 30 else None


def run(db: str = "data/prediction.db", *, start: str = "0000-00-00",
        until: str = "9999-99-99", cut: str = "9999-99-99",
        only: set[str] | None = None, ids: set[int] | None = None) -> dict:
    conn = sqlite3.connect(db)
    bets = [b[:5] for b in conn.execute(
        "select ticker, substr(ts,1,10), direction, horizon_days, outcome, id from predictions "
        "where horizon_days >= 2 and direction in ('UP','DOWN') and outcome in ('win','loss') "
        "and substr(ts,1,10) between ? and ? and coalesce(substr(resolved_at,1,10),'') <= ?",
        (start, until, cut))
        if (only is None or b[0] in only) and (ids is None or b[5] in ids)]
    if not bets:
        raise SystemExit("tak ada taruhan tuntas di DB ini")
    first = min(d for _, d, _, _, _ in bets)
    series = _series(conn, f"{int(first[:4]) - 1}{first[4:]}")  # 1 tahun sebelum taruhan pertama
    idx = {tk: {d: i for i, (d, _, _) in enumerate(s)} for tk, s in series.items()}
    liquid = [tk for tk, s in series.items()
              if statistics.median([t for d, _, t in s if d >= first] or [0]) >= config.MIN_TURNOVER]
    conn.close()  # Windows mengunci berkas selama koneksi hidup; self-check menghapusnya

    rows, cache = [], {}
    for tk, d, direction, h, outcome in bets:
        key = (d, int(h), direction)
        if key not in cache:
            cache[key] = _matched_base(series, liquid, idx, d, int(h), direction)
        if cache[key] is None:
            continue
        rows.append({"ts": d, "dir": direction, "win": outcome == "win",
                     "static": baseline_for(direction, int(h)), "matched": cache[key]})

    out = {"n_bets": len(bets), "n_used": len(rows), "n_liquid": len(liquid)}
    for name, sel in (("SEMUA", rows), ("UP", [r for r in rows if r["dir"] == "UP"]),
                      ("DOWN", [r for r in rows if r["dir"] == "DOWN"])):
        if not sel:
            continue
        per_day, per_day_st = collections.defaultdict(list), collections.defaultdict(list)
        win_day, base_day, base_day_st = (collections.defaultdict(list),
                                          collections.defaultdict(list),
                                          collections.defaultdict(list))
        for r in sel:
            per_day[r["ts"]].append(r["win"] * 100 - r["matched"])
            per_day_st[r["ts"]].append(r["win"] * 100 - r["static"])
            win_day[r["ts"]].append(r["win"] * 100)
            base_day[r["ts"]].append(r["matched"])
            base_day_st[r["ts"]].append(r["static"])

        def per_date(d):
            return round(statistics.mean(statistics.mean(v) for v in d.values()), 1)
        # Estimasi titik HARUS satu satuan dengan selangnya: keduanya rata-rata PER TANGGAL.
        # Selisih rata-rata mentah (per taruhan) memberi bobot lebih pada tanggal yang ramai
        # dan bisa jatuh DI LUAR selangnya sendiri — tabel seperti itu mati di sidang.
        daily = np.array([statistics.mean(v) for v in per_day.values()])
        daily_st = np.array([statistics.mean(v) for v in per_day_st.values()])
        lo, hi = block_bootstrap(daily, block=5) if len(daily) >= 10 else (float("nan"),) * 2
        lo_st, hi_st = (block_bootstrap(daily_st, block=5) if len(daily_st) >= 10
                        else (float("nan"),) * 2)
        out[name] = {
            "n": len(sel), "hari": len(per_day),
            "win": round(statistics.mean(r["win"] * 100 for r in sel), 1),
            "dasar_statis": round(statistics.mean(r["static"] for r in sel), 1),
            "dasar_cocok": round(statistics.mean(r["matched"] for r in sel), 1),
            "selisih_cocok": round(float(daily.mean()), 1),
            "ci95_cocok": (round(lo, 1), round(hi, 1)),
            "selisih_statis": round(float(daily_st.mean()), 1),
            "ci95_statis": (round(lo_st, 1), round(hi_st, 1)),
            # Kolom PER TANGGAL — dipakai kalau angka disajikan dalam tabel. Wajib dipakai
            # bersama: menang_hari - dasar_hari HARUS sama dengan selisih_cocok. Mencampur
            # `win` (per taruhan) dengan `selisih_cocok` (per tanggal) menghasilkan tabel yang
            # aritmetikanya tidak menutup, dan tandanya bisa berbalik.
            "menang_hari": per_date(win_day),
            "dasar_cocok_hari": per_date(base_day),
            "dasar_statis_hari": per_date(base_day_st),
        }
    return out


def _self_check() -> None:
    """Universe sintetis: separuh saham naik 2%, separuh turun 2% tiap hari."""
    conn = sqlite3.connect(":memory:")
    conn.execute("create table prices (ticker text, ts text, close real, volume real)")
    conn.execute("create table predictions (id integer primary key, ticker text, ts text, "
                 "direction text, horizon_days int, outcome text, resolved_at text)")
    days = [f"2026-01-{i:02d}" for i in range(1, 31)]
    for k in range(80):
        up, price = k % 2 == 0, 1000.0
        for d in days:
            conn.execute("insert into prices values (?,?,?,?)",
                         (f"T{k:03d}", d, price, 1e9))
            price *= 1.02 if up else 0.98
    for d in days[:20]:  # taruhan UP pada saham yang memang naik → menang 100%
        conn.execute("insert into predictions values (null,'T000',?,'UP',2,'win',?)", (d, d))
    conn.commit()
    tmp = "basecheck_selfcheck.db"
    if os.path.exists(tmp):
        os.remove(tmp)
    conn.execute(f"vacuum into '{tmp}'")
    got = run(tmp)
    os.remove(tmp)
    assert got["UP"]["win"] == 100.0, got
    assert 49 <= got["UP"]["dasar_cocok"] <= 51, f"dasar cocok harus ~50%, dapat {got['UP']}"
    print("self-check OK: dasar cocok-tanggal = %.1f%% pada universe 50/50" %
          got["UP"]["dasar_cocok"])


def _arg(flag: str, default: str) -> str:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        pos = [a for a in sys.argv[1:] if not a.startswith("--")
               and sys.argv[sys.argv.index(a) - 1] not in ("--from", "--until", "--cut")]
        res = run(pos[0] if pos else "data/prediction.db",
                  start=_arg("--from", "0000-00-00"), until=_arg("--until", "9999-99-99"),
                  cut=_arg("--cut", "9999-99-99"))
        print(f"taruhan tuntas {res['n_bets']}, terpakai {res['n_used']}, "
              f"universe likuid {res['n_liquid']} kode\n")
        print(f"{'kelompok':8} {'n':>4} {'hari':>5} {'win%':>6} {'statis':>7} "
              f"{'cocok':>7} {'d_statis':>8} {'d_cocok':>8}  CI95 d_cocok")
        for k in ("SEMUA", "UP", "DOWN"):
            r = res.get(k)
            if r:
                ci = ("hari tidak cukup" if r["ci95_cocok"][0] != r["ci95_cocok"][0]
                      else str(r["ci95_cocok"]))
                print(f"{k:8} {r['n']:>4} {r['hari']:>5} {r['win']:>6.1f} "
                      f"{r['dasar_statis']:>7.1f} {r['dasar_cocok']:>7.1f} "
                      f"{r['selisih_statis']:>+8.1f} {r['selisih_cocok']:>+8.1f}  {ci}")
