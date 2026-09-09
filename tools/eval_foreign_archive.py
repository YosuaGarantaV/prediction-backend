"""Vonis item-3 NEXT_UPDATES atas arsip arus asing per-tanggal (data/foreign_flow/).

Standar penilaian = milik repo ini, bukan standar baru:
  menang    = |gerak| > 0,5% searah prediksi (sama dgn scoring live)
  universe  = turnover harian median >= config.MIN_TURNOVER, harga >= 200
  CI        = app.eval.block_bootstrap (blok tanggal >= horizon)
  lulus     = batas bawah CI > 0

Dua koreksi yang mengubah vonis dan sengaja dipasang di sini:
  1. BASE RATE DICOCOKKAN TANGGAL, dihitung dari sampel ini sendiri. Tabel statis
     `eval.BASELINE_WINRATE` diukur di universe+periode lain; memakainya melebihkan edge
     sampai 2,3 pp (UP h5) dan MENGURANGI edge DOWN 1,6 pp — penyebut yang salah.
  2. ENTRY DI OPEN t+1. IDX menerbitkan Trading Summary SESUDAH closing, jadi sinyal
     tanggal t tak bisa dieksekusi di close t. Entry-close melebihkan win-rate UP h1
     sebesar 4,0 pp (38,0% vs 34,0% base) — seluruhnya look-ahead.

Jalankan:  python tools/eval_foreign_archive.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config                                    # noqa: E402
from app.data.idxflow import FOREIGN_DIR         # noqa: E402
from app.eval import block_bootstrap             # noqa: E402

WIN = 0.005                                      # ambang menang, sama dgn scoring live
LOOK = 20                                        # jendela akumulasi
HOR = (1, 3, 5)
FEE = (config.FEE_BUY + config.FEE_SELL) * 100
OUT_FILE = config.DATA_DIR / "backtest_foreign_archive.json"


def load_panel() -> dict[str, list[tuple[str, dict]]]:
    """{ticker: [(tanggal, baris), ...]} urut tanggal, dari arsip per-tanggal."""
    panel: dict[str, list[tuple[str, dict]]] = {}
    for f in sorted(FOREIGN_DIR.glob("*.json")):
        for code, r in json.loads(f.read_text(encoding="utf-8")).items():
            panel.setdefault(code, []).append((f.stem, r))
    return panel


def build_rows(panel: dict) -> list[tuple]:
    """Fitur pada tanggal t + return dari OPEN t+1 ke CLOSE t+h."""
    rows = []
    for ser in panel.values():
        if len(ser) < 120:
            continue
        td = [d for d, _ in ser]
        by = dict(ser)
        for i in range(LOOK, len(td) - max(HOR) - 1):
            r = by[td[i]]
            entry = by[td[i + 1]].get("open") or 0.0   # EKSEKUSI paling awal yg mungkin
            if entry <= 0 or r["close"] < 200:
                continue
            w20 = [by[x] for x in td[i - LOOK + 1:i + 1]]
            if float(np.median([x["value"] for x in w20])) < config.MIN_TURNOVER:
                continue
            tot = r["fbuy"] + r["fsell"]
            queue = r["bidv"] + r["offerv"]
            v20 = sum(x["value"] for x in w20) or 1.0
            rows.append((
                td[i],
                (r["fbuy"] - r["fsell"]) / tot if tot > 0 else None,      # foreign_pressure
                (r["bidv"] - r["offerv"]) / queue if queue > 0 else None,  # book imbalance
                sum(x["net_val"] for x in w20) / v20,                      # akumulasi 20h
                queue * r["close"],                                        # nilai antrean tutup
                r["close"] / by[td[i - LOOK]]["close"] - 1,                # ret20
                {h: by[td[i + h]]["close"] / entry - 1 for h in HOR},
            ))
    return rows


def _hit(fwd: dict, direction: str, h: int) -> float:
    return 1.0 if (fwd[h] > WIN if direction == "UP" else fwd[h] < -WIN) else 0.0


def verdict(rows: list[tuple], base: dict, pick, direction: str, h: int, label: str) -> dict:
    sel = [r for r in rows if pick(r)]
    if len(sel) < 50:
        return {"label": label, "h": h, "n": len(sel), "status": "insufficient"}
    hits, rets = {}, {}
    for r in sel:
        hits.setdefault(r[0], []).append(_hit(r[6], direction, h))
        rets.setdefault(r[0], []).append(r[6][h] * 100 * (1 if direction == "UP" else -1))
    dates = sorted(hits)
    diff = np.array([np.mean(hits[d]) * 100 - base[(direction, h)][d] for d in dates])
    lo, hi = block_bootstrap(diff, block=max(5, h), seed=0)
    wr = float(np.mean([v for d in dates for v in hits[d]])) * 100
    ret = float(np.mean([v for d in dates for v in rets[d]]))
    return {"label": label, "h": h, "direction": direction, "n": len(sel),
            "win_rate": round(wr, 1), "base": round(wr - float(np.mean(diff)), 1),
            "edge_pp": round(float(np.mean(diff)), 1), "ci": [round(lo, 1), round(hi, 1)],
            "pass": bool(lo > 0), "ret_net_pct": round(ret - FEE, 2)}


SIGNALS = [
    (lambda r: r[1] is not None and r[1] >= 0.4, "UP", "foreign_pressure >= +0,40"),
    (lambda r: r[1] is not None and r[1] <= -0.4, "DOWN", "foreign_pressure <= -0,40"),
    (lambda r: r[2] is not None and r[2] >= 0.5, "UP", "buku order bid >> offer"),
    (lambda r: r[2] is not None and r[2] <= -0.5, "DOWN", "buku order offer >> bid"),
    (lambda r: r[3] >= 0.05, "UP", "akumulasi asing 20h >= +5% turnover"),
    (lambda r: r[3] >= 0.05 and r[5] <= -0.05, "UP", "akumulasi 20h & harga TURUN >5%"),
    (lambda r: r[3] >= 0.05 and abs(r[5]) <= 0.05, "UP", "akumulasi 20h & harga DATAR"),
    # PREDIKAT PRODUKSI PERSIS (factors.extra_signals): ambang 0,6 + antrean >= Rp200jt
    (lambda r: r[2] is not None and r[2] >= 0.6 and r[1] is not None and r[1] >= 0.4
     and r[4] >= 2e8, "UP", "GABUNGAN prod: book>=+0,6 & fp>=+0,40"),
    (lambda r: r[2] is not None and r[2] <= -0.6 and r[1] is not None and r[1] <= -0.4
     and r[4] >= 2e8, "DOWN", "GABUNGAN prod: book<=-0,6 & fp<=-0,40"),
    (lambda r: r[2] is not None and r[2] >= 0.6 and r[4] >= 2e8, "UP", "book>=+0,6 saja (prod)"),
    (lambda r: r[1] is not None and r[1] >= 0.4, "UP", "fp>=+0,40 saja"),
]


def main() -> int:
    panel = load_panel()
    if not panel:
        print(f"arsip kosong: {FOREIGN_DIR} (isi dulu lewat tools/import_idx_archive.py)")
        return 1
    rows = build_rows(panel)
    base = {}
    for h in HOR:
        for d in ("UP", "DOWN"):
            acc: dict[str, list[float]] = {}
            for r in rows:
                acc.setdefault(r[0], []).append(_hit(r[6], d, h))
            base[(d, h)] = {k: float(np.mean(v)) * 100 for k, v in acc.items()}
    dates = sorted({r[0] for r in rows})
    print(f"arsip {len(list(FOREIGN_DIR.glob('*.json')))} hari bursa | sampel {len(rows):,} saham-hari "
          f"| {len(dates)} tanggal ({dates[0]} .. {dates[-1]}) | entry OPEN t+1 | fee {FEE:.2f}%\n")
    results = []
    for h in HOR:
        print(f"=== HORIZON {h} HARI ===")
        for pick, direction, label in SIGNALS:
            v = verdict(rows, base, pick, direction, h, label)
            results.append(v)
            if v.get("status") == "insufficient":
                print(f"  {label:38} {direction:4} n={v['n']} (sampel kurang)")
                continue
            print(f"  {label:38} {direction:4} n={v['n']:6} wr {v['win_rate']:5.1f}% "
                  f"edge {v['edge_pp']:+5.1f} pp CI [{v['ci'][0]:+5.1f},{v['ci'][1]:+5.1f}] "
                  f"{'LULUS' if v['pass'] else 'gagal'}  net {v['ret_net_pct']:+5.2f}%")
        print()
    OUT_FILE.write_text(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(), "archive_days": len(dates),
        "samples": len(rows), "entry": "open_t+1", "win_threshold_pct": WIN * 100,
        "fee_roundtrip_pct": FEE, "results": results}, indent=2), encoding="utf-8")
    print(f"artefak -> {OUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
