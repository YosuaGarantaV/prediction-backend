"""Peta kesalahan: di mana buku prediksi kehilangan edge, per emiten dan per kondisi.

    python tools/error_gap.py
    python tools/error_gap.py --bets-only   # hanya yang dihitung papan skor
    python tools/error_gap.py --days 30
    python tools/error_gap.py --selftest

`edge` = gerak nyata diarahkan ke klaim, dalam poin persen: UP +actual_pct, DOWN
-actual_pct, FLAT -|actual_pct|. Win-rate menghitung seberapa sering benar, edge
menghitung seberapa besar benarnya.

Tiap ember punya base rate sendiri, jadi bandingkan antar-ember lewat kolom edge, bukan
win-rate. Kandidat gerbang dari sini harus lewat tools/gate_trial.py atau basecheck.py dulu.
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

from app import db  # noqa: E402


def edge(direction: str, actual_pct: float) -> float:
    """Gerak nyata diarahkan ke klaim. FLAT dihukum sebesar gerak apa pun arahnya."""
    if direction == "UP":
        return actual_pct
    if direction == "DOWN":
        return -actual_pct
    return -abs(actual_pct)


def _fetch(days: int | None, bets_only: bool = False):
    # Baris "prediksi 1-hari (besok)" adalah TAMPILAN dan sudah dibuang repo.stats() dari papan
    # skor. Menganalisis kesalahan atas populasi yang berbeda dari yang dinilai = salah sasaran:
    # 1.412 dari 1.757 baris resolved ternyata display (9 Sep 2026).
    q = ("SELECT ticker, direction, probability, expected_pct, actual_pct, outcome, "
         "horizon_days, entry_price, substr(ts,1,10) d FROM predictions "
         "WHERE status='resolved' AND actual_pct IS NOT NULL")
    if bets_only:
        q += " AND reasoning NOT LIKE 'prediksi 1-hari%'"
    if days:
        q += f" AND ts > date('now','-{int(days)} days')"
    rows = [dict(r) for r in db.get_conn().execute(q)]
    for r in rows:
        r["edge"] = edge(r["direction"], r["actual_pct"])
    return rows


def _closes() -> dict[str, list[tuple[str, float]]]:
    px: dict[str, list] = defaultdict(list)
    for r in db.get_conn().execute("SELECT ticker, substr(ts,1,10) d, close FROM prices "
                                   "ORDER BY ticker, ts"):
        if r["close"]:
            px[r["ticker"]].append((r["d"], r["close"]))
    return px


def exante_vol(series: list[tuple[str, float]], before: str) -> float | None:
    """Stdev return harian 20 sesi SEBELUM tanggal prediksi. None kalau riwayat kurang.
    Sengaja ex-ante: mengelompokkan pakai gerak yang sudah terjadi = melingkar."""
    hist = [c for d, c in series if d < before][-21:]
    if len(hist) < 10:
        return None
    rets = [(hist[i] / hist[i - 1] - 1) * 100 for i in range(1, len(hist)) if hist[i - 1]]
    return st.pstdev(rets) if len(rets) >= 8 else None


def _bucket(rows, title, keyf):
    g: dict = defaultdict(list)
    for r in rows:
        k = keyf(r)
        if k is not None:
            g[k].append(r)
    print(f"\n{title}")
    print(f"   {'ember':18s}{'n':>6}{'win%':>8}{'edge pp':>10}")
    for k in sorted(g):
        v = g[k]
        win = sum(x["outcome"] == "win" for x in v) / len(v) * 100
        print(f"   {str(k):18s}{len(v):6d}{win:8.1f}{st.mean(x['edge'] for x in v):+10.2f}")


def main(days: int | None, bets_only: bool = False) -> None:
    rows = _fetch(days, bets_only)
    if not rows:
        print("belum ada prediksi resolved")
        return
    total = sum(r["edge"] for r in rows)
    print(f"{len(rows)} prediksi resolved, {len({r['ticker'] for r in rows})} emiten, "
          f"total edge {total:+.0f} pp"
          + (f" (rentang {days} hari)" if days else ""))

    per: dict[str, dict] = {}
    for r in rows:
        a = per.setdefault(r["ticker"], {"n": 0, "w": 0, "sum": 0.0, "mv": [], "kl": []})
        a["n"] += 1
        a["w"] += r["outcome"] == "win"
        a["sum"] += r["edge"]
        a["mv"].append(abs(r["actual_pct"]))
        a["kl"].append(abs(r["expected_pct"] or 0))

    print(f"\nemiten penyumbang KERUGIAN edge terbesar (kumulatif ke total)")
    print(f"   {'ticker':8s}{'n':>5}{'win%':>7}{'edge tot':>10}{'|gerak|':>9}{'klaim':>8}   kum%")
    cum = 0.0
    for t, a in sorted(per.items(), key=lambda x: x[1]["sum"])[:15]:
        cum += a["sum"]
        print(f"   {t:8s}{a['n']:5d}{a['w']/a['n']*100:7.1f}{a['sum']:+10.1f}"
              f"{st.median(a['mv']):9.2f}{st.median(a['kl']):8.2f}   {cum/total*100:4.1f}%")

    print(f"\nemiten terbaik")
    for t, a in sorted(per.items(), key=lambda x: -x[1]["sum"])[:8]:
        print(f"   {t:8s}{a['n']:5d}{a['w']/a['n']*100:7.1f}{a['sum']:+10.1f}")

    _bucket(rows, "per arah klaim", lambda r: r["direction"])
    _bucket(rows, "per keyakinan", lambda r: (
        "a. <45" if (r["probability"] or 0) < 45 else "b. 45-55" if r["probability"] < 55
        else "c. 55-65" if r["probability"] < 65 else "d. 65+"))
    _bucket(rows, "per horizon", lambda r: f"h={r['horizon_days']}"
            if (r["horizon_days"] or 0) in (1, 3, 5) else None)
    _bucket(rows, "per harga masuk", lambda r: (
        "1. <100" if (r["entry_price"] or 0) < 100 else "2. 100-500" if r["entry_price"] < 500
        else "3. 500-2rb" if r["entry_price"] < 2000 else "4. >2rb"))

    px = _closes()
    def volb(r):
        v = exante_vol(px.get(r["ticker"], []), r["d"])
        if v is None:
            return None
        return ("A. tenang <1,5%" if v < 1.5 else "B. 1,5-3%" if v < 3
                else "C. 3-5%" if v < 5 else "D. liar >5%")
    _bucket(rows, "per volatilitas EX-ANTE (20 sesi sebelum prediksi)", volb)
    print("\nbaca kolom edge, bukan win%: tiap ember punya base rate sendiri.")
    print("gerbang baru dari temuan di sini WAJIB lewat basecheck.py dulu.")


def selftest() -> None:
    assert edge("UP", 2.0) == 2.0
    assert edge("DOWN", -2.0) == 2.0          # turun sesuai klaim = edge positif
    assert edge("DOWN", 2.0) == -2.0
    assert edge("FLAT", 3.0) == -3.0          # FLAT dihukum arah apa pun
    assert edge("FLAT", -3.0) == -3.0
    s = [("2026-01-%02d" % d, 100 + d) for d in range(1, 21)]
    v = exante_vol(s, "2026-01-21")
    assert v is not None and v >= 0
    assert exante_vol(s, "2026-01-03") is None   # riwayat kurang -> jangan mengarang
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        d = None
        if "--days" in sys.argv:
            d = int(sys.argv[sys.argv.index("--days") + 1])
        db.init_db()
        main(d, bets_only="--bets-only" in sys.argv)
