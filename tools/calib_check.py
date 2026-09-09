"""STATIS: cek kalibrasi (klaim % vs realisasi) + miss + posisi terbuka. Read-only.
Jalankan: .venv\\Scripts\\python.exe tools\\calib_check.py
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass
from app import db, repo
db.init_db()

res = repo.resolved_predictions(1000)
n = len(res)
wins = sum(1 for r in res if r["outcome"] == "win")
print(f"=== KALIBRASI (klaim % vs realisasi) — {n} prediksi resolved ===")
print(f"{'bucket':10} {'n':>4} {'klaim~':>7} {'realisasi':>9} {'gap':>6}  cocok?")
for lo, hi in [(50,60),(60,70),(70,80),(80,101)]:
    b = [r for r in res if lo <= (r["probability"] or 0) < hi]
    if not b: continue
    w = sum(1 for r in b if r["outcome"] == "win")
    claim = sum(r["probability"] for r in b)/len(b)
    real = w/len(b)*100
    gap = claim - real
    ok = "OK" if abs(gap) <= 7 else ("TERLALU PD" if gap > 0 else "terlalu rendah")
    print(f"{lo}-{hi-1 if hi<101 else 100:<6} {len(b):>4} {claim:>6.0f}% {real:>8.0f}% {gap:>+5.0f}  {ok}")
allgap = (sum(r['probability'] for r in res)/n) - (wins/n*100) if n else 0
print(f"\nMISS: {n-wins}/{n} ({(n-wins)/n*100:.0f}%) | win {wins} | rata2 over-confidence {allgap:+.0f} poin")
hc = [r for r in res if (r["probability"] or 0) >= 65]
hcm = [r for r in hc if r["outcome"] == "loss"]
side = sum(1 for r in hcm if abs(r["actual_pct"] or 0) < 1.0)
print(f"HIGH-CONF (>=65%): {len(hc)} prediksi, {len(hcm)} meleset "
      f"({side} sideways / {len(hcm)-side} lawan-arah)")

print("\n=== POSISI TERBUKA ===")
pos = repo.get_positions()
if not pos:
    print("(tidak ada posisi)")
for p in pos:
    q = repo.get_quote(p["ticker"])
    price = q["price"] if q else p["avg_price"]
    pl = (price/p["avg_price"] - 1) * 100 if p["avg_price"] else 0
    print(f"  {p['ticker']:5} {p['qty']:>8.0f} @ {p['avg_price']:>8.1f}  now {price:>8.1f}  P/L {pl:+5.1f}%")
pf = repo.latest_portfolio()
print(f"\nPortfolio total Rp{pf['total']:,.0f} | P/L {pf['pnl_pct']:+.2f}% | kas Rp{pf['cash']:,.0f}")
