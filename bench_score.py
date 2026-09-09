"""Skor buku prediksi Claude vs mesin, setelah pasar tutup.

    python bench_score.py                 # skor 2026-07-29
    python bench_score.py 2026-07-30      # tanggal lain
    python bench_score.py --selftest
"""
import sys, csv, sqlite3, datetime

DB = "data/prediction.db"
BAND = 0.5  # % ; di dalam +/-BAND dihitung FLAT


def grade(call, chg):
    """Benar/salah 2 cara: band 3-arah, dan tanda saja (FLAT hanya benar bila chg==0)."""
    strict = (call == "UP" and chg >= BAND) or (call == "DOWN" and chg <= -BAND) or \
             (call == "FLAT" and -BAND < chg < BAND)
    sign = (call == "UP" and chg > 0) or (call == "DOWN" and chg < 0) or (call == "FLAT" and chg == 0)
    return strict, sign


def selftest():
    assert grade("UP", 1.2) == (True, True)
    assert grade("UP", 0.3) == (False, True)        # naik tipis: lolos tanda, gagal band
    assert grade("FLAT", 0.3) == (True, False)
    assert grade("FLAT", 0.0) == (True, True)
    assert grade("DOWN", -0.4) == (False, True)
    assert grade("DOWN", 2.0) == (False, False)
    print("selftest ok")


def main(day):
    prev = None
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = list(csv.DictReader(open(f"bench_claude_{day}.csv", encoding="utf-8")))
    res, miss = [], []
    for r in rows:
        t = r["ticker"]
        bar = c.execute("select close from prices where ticker=? and ts like ?", (t, f"{day}%")).fetchone()
        if not bar:
            miss.append(t); continue
        px, close = float(r["px"]), bar[0]
        chg = (close / px - 1) * 100
        s, g = grade(r["dir"], chg)
        res.append((t, r["dir"], float(r["exp"]), round(chg, 2), close, s, g, float(r["conf"])))

    if not res:
        print(f"Belum ada bar {day} di DB. Jalankan lagi setelah data harian masuk."); return
    res.sort(key=lambda x: -x[3])
    print(f"{'TKR':7}{'call':>6}{'exp%':>7}{'real%':>8}{'close':>10}  band sign")
    for t, d, e, chg, close, s, g in ((x[0], x[1], x[2], x[3], x[4], x[5], x[6]) for x in res):
        print(f"{t:7}{d:>6}{e:>7}{chg:>8}{close:>10}   {'OK ' if s else '.  '} {'OK' if g else '.'}")

    n = len(res)
    band = sum(x[5] for x in res); sign = sum(x[6] for x in res)
    mae = sum(abs(x[2] - x[3]) for x in res) / n
    hi = [x for x in res if x[7] >= 55]
    print(f"\nN={n}  hit band-{BAND}%: {band}/{n} = {band/n*100:.1f}%"
          f"   hit tanda: {sign}/{n} = {sign/n*100:.1f}%   MAE={mae:.2f} pp")
    if hi:
        print(f"conf>=55 saja: band {sum(x[5] for x in hi)}/{len(hi)} = {sum(x[5] for x in hi)/len(hi)*100:.1f}%"
              f"   tanda {sum(x[6] for x in hi)}/{len(hi)} = {sum(x[6] for x in hi)/len(hi)*100:.1f}%")
    if miss:
        print("tanpa bar (suspend/belum masuk):", ", ".join(miss))

    # pembanding: prediksi mesin sendiri yang jatuh tempo hari ini
    eng = c.execute(
        "select ticker,direction,entry_price,expected_pct from predictions "
        "where horizon_days=1 and substr(ts,1,10)=? ", (str(datetime.date.fromisoformat(day) - datetime.timedelta(days=1)),)
    ).fetchall()
    if eng:
        ok = 0; tot = 0
        for t, d, entry, exp in eng:
            bar = c.execute("select close from prices where ticker=? and ts like ?", (t, f"{day}%")).fetchone()
            if not bar or not entry: continue
            chg = (bar[0] / entry - 1) * 100
            tot += 1; ok += grade(d, chg)[0]
        if tot:
            print(f"\nMesin (horizon 1d, dibuat H-1): band {ok}/{tot} = {ok/tot*100:.1f}%")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "--selftest": selftest()
    else: main(a[0] if a else "2026-07-29")
