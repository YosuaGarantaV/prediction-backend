"""ANALIS PENUH kondisi pasar + cek kesehatan SEMUA sumber data. Sekali jalan, selesai.

Tidak butuh server hidup — refresh tiap sumber langsung, lalu sintesis. Tiap sumber
dibungkus try/except: satu gagal tak mematikan briefing. Jalankan:
    .venv\\Scripts\\python.exe tools\\market_brief.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
from app import db, repo
from app.data import flow as flow_mod
from app.data import idxflow, news, premarket, stocks
from app import events, model

SRC = []  # (nama, status) hasil cek sumber


def step(name, fn, *a, **k):
    t = time.time()
    try:
        r = fn(*a, **k)
        SRC.append((name, f"OK ({time.time()-t:.0f}s) {r if isinstance(r,(int,str)) else ''}"))
        return r
    except Exception as e:  # noqa: BLE001
        SRC.append((name, f"GAGAL: {repr(e)[:90]}"))
        return None


def hr(t):
    print(f"\n{'='*64}\n{t}\n{'='*64}")


def main():
    db.init_db()
    phase = stocks.market_phase()
    hr("CEK SEMUA SUMBER DATA (refresh live)")
    step("makro global (yfinance)", stocks.refresh_macro)
    step("indeks IHSG (yfinance)", stocks.fetch_indices)
    if stocks.market_is_open():
        step("harga hot-set live (yfinance)", stocks.refresh_hot)
    step("berita 65 sumber + YouTube", news.refresh_news)
    step("arus asing RESMI IDX (Cloudflare)", idxflow.refresh_foreign)
    step("proxy arus asing + breadth", flow_mod.compute_flow)
    step("model statistik (latih)", model.train)
    for name, st in SRC:
        flag = "[OK]  " if st.startswith("OK") else "[!!]  "
        print(f"{flag}{name:38} {st}")

    # ---------- MACRO / GLOBAL ----------
    macro = {m["name"]: m for m in repo.all_macro()}
    def chg(n):
        m = macro.get(n); return (m["change_pct"] if m and m["change_pct"] is not None else None)
    def px(n):
        m = macro.get(n); return (m["price"] if m else None)

    hr(f"KONDISI PASAR — {datetime.now():%Y-%m-%d %H:%M} WIB | fase bursa: {phase}")
    gb = premarket.global_brief()
    print(f"LEAN PEMBUKAAN: {gb['lean']}  (skor {gb['score']:+.1f})")
    print(f"  penggerak: {gb['text']}")
    ihsg = macro.get("IHSG")
    if ihsg:
        print(f"IHSG: {px('IHSG'):,.0f}  ({chg('IHSG'):+.2f}%)" if chg('IHSG') is not None
              else f"IHSG: {px('IHSG')}")

    print("\nSINYAL GLOBAL (semalam / live):")
    order = ["S&P 500", "Nasdaq", "Nikkei 225", "Hang Seng", "Shanghai",
             "Dollar Index (DXY)", "USD/IDR", "US 10Y Yield", "VIX (Fear)",
             "WTI Crude Oil", "Brent Oil", "Gold", "Copper", "Coal (proxy ITMG)", "Bitcoin"]
    for n in order:
        c = chg(n)
        if c is None:
            continue
        arrow = "▲" if c > 0 else ("▼" if c < 0 else "·")
        print(f"  {arrow} {n:22} {c:+6.2f}%")

    # ---------- BREADTH + ARUS ASING ----------
    hr("BREADTH & ARUS ASING")
    f = repo.latest_flow()
    if f:
        print(f"PROXY: {f['label']} (skor {f['flow_score']}) | breadth {f['breadth_up']}% naik "
              f"({f['advancers']} naik / {f['decliners']} turun)")
        print(f"  inflow proxy: {f['top_inflow']} | outflow: {f['top_outflow']}")
    fdata = idxflow._load()
    if fdata:
        date = next(iter(fdata.values()))["date"]
        tot = sum(v["net_val"] for v in fdata.values())
        ranked = sorted(fdata.items(), key=lambda kv: kv[1]["net_val"], reverse=True)
        ibuy = ", ".join(f"{k}+{v['net_val']/1e9:.0f}M" for k, v in ranked[:4] if v["net_val"] > 0)
        isell = ", ".join(f"{k}{v['net_val']/1e9:.0f}M" for k, v in ranked[-4:] if v["net_val"] < 0)
        print(f"RESMI IDX {date}: NET PASAR Rp{tot/1e9:+.0f}M ({len(fdata)} saham)")
        print(f"  top beli asing: {ibuy or '-'}")
        print(f"  top jual asing: {isell or '-'}")

    # ---------- GAINERS / LOSERS ----------
    quotes = [q for q in repo.all_quotes() if q.get("change_pct") is not None]
    quotes.sort(key=lambda q: q["change_pct"], reverse=True)
    big = [q for q in quotes if (q.get("price") or 0) * (q.get("volume") or 0) > 5e9]  # likuid
    if big:
        hr("PERGERAKAN SAHAM LIKUID (turnover > Rp5M)")
        print("TOP NAIK:  " + " | ".join(f"{q['ticker']} {q['change_pct']:+.1f}%" for q in big[:6]))
        print("TOP TURUN: " + " | ".join(f"{q['ticker']} {q['change_pct']:+.1f}%" for q in big[-6:]))

    # ---------- BERITA PENGGERAK ----------
    hr("BERITA PENGGERAK (36 jam, market-wide & berdampak)")
    rows = repo.recent_news(limit=150, max_age_hours=36)
    movers = [n for n in rows if n["scope"] == "global" or abs(int(n.get("impact") or 0)) >= 1]
    movers.sort(key=lambda n: (abs(int(n.get("impact") or 0)), n["ts"]), reverse=True)
    POL = ("prabowo", "trump", "apbn", "gaji guru", "kopdes", "mbg", "pajak", "ppn",
           "subsidi", "danantara", "reshuffle", "fiskal", "anggaran", "the fed", "powell")
    pol = [n for n in movers if any(k in (n["title"] + " " + (n["summary"] or "")).lower() for k in POL)]
    print("— POLITIK / KEBIJAKAN (sumber guncangan mendadak) —")
    for n in pol[:6]:
        print(f"  [{n['sentiment']} {int(n['impact'] or 0):+d}] {n['title'][:90]}")
    if not pol:
        print("  (tak ada berita politik/kebijakan signifikan)")
    print("— LAIN (makro/geopolitik/komoditas) —")
    seen = {id(n) for n in pol}
    for n in [m for m in movers if id(m) not in seen][:8]:
        print(f"  [{n['sentiment']} {int(n['impact'] or 0):+d}|{n['scope']}] {n['title'][:88]}")

    # ---------- KATALIS + IPO ----------
    hr("KATALIS MENDATANG & IPO")
    for e in events.upcoming_events(21)[:6]:
        print(f"  H-{e['days_ahead']} {e['date']} {e['name']} [{e['impact']}]")
    ipos = news.upcoming_ipos()
    if ipos:
        print("IPO mendatang: " + " | ".join(
            (f"{(i.get('codes') or ['?'])[0]}" for i in ipos[:8])))

    # ---------- ENGINE STATE ----------
    hr("KONDISI ENGINE (paper trading + akurasi)")
    pf = repo.latest_portfolio(); stp = repo.prediction_stats()
    print(f"Portfolio: total Rp{pf['total']:,.0f} | P/L {pf['pnl_pct']:+.2f}% (Rp{pf['pnl']:,.0f}) | kas Rp{pf['cash']:,.0f}")
    print(f"Akurasi: {stp['resolved']} resolved | win {stp['wins']} / loss {stp['losses']} | win-rate {stp['win_rate']}%")
    pos = repo.get_positions()
    print(f"Posisi terbuka ({len(pos)}): " + (", ".join(p['ticker'] for p in pos) or "-"))
    preds = repo.recent_predictions(25)
    up = sum(1 for p in preds if p["direction"] == "UP")
    dn = sum(1 for p in preds if p["direction"] == "DOWN")
    fl = sum(1 for p in preds if p["direction"] == "FLAT")
    print(f"Bias 25 prediksi terbaru: UP {up} / DOWN {dn} / FLAT {fl}")


if __name__ == "__main__":
    main()
