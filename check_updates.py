"""GERBANG BUKTI NEXT_UPDATES.md — KUNCI. Item dibangun HANYA kalau item-nya PASS di sini.
Opini (user/AI) TAK meng-override; hanya DATA yang memutuskan. Jalankan: python check_updates.py

Tiap item: kondisi OBJEKTIF yg dicek dari DB/artefak. PASS = boleh dibangun. HOLD = belum cukup bukti.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import eval as _eval  # noqa: E402 — vonis statistik terpusat (baseline + CI blok)

try:  # konsol Windows (cp1252) tak bisa cetak emoji → paksa UTF-8 (sama seperti run.py)
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DB = Path(__file__).resolve().parent / "data" / "prediction.db"
DATA = DB.parent


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def item2_calibration(c) -> tuple[bool, str]:
    """Kalibrasi lebih cepat — PASS kalau win-rate UP BEDA NYATA dari base rate pasar.

    Bar LAMA ("n>=40 & winrate<40%") tidak valid dan pernah meloloskan item dari data yang
    tak menyimpulkan apa-apa: (a) tanpa baseline — 40% terdengar buruk padahal base rate UP
    cuma 38,7%, jadi bar itu nyaris selalu terpenuhi; (b) n=40 BARIS bisa datang dari 2-3
    tanggal, dan prediksi dalam satu hari berkorelasi kuat (satu hari merah menyeret semua).
    Sekarang: unit = tanggal, vonis = CI blok vs base rate (app/eval.verdict_winrate)."""
    rows = c.execute(
        "SELECT substr(ts,1,10) d, horizon_days h, outcome "
        "FROM predictions WHERE status='resolved' AND direction='UP' AND outcome IS NOT NULL"
    ).fetchall()
    if not rows:
        return False, "belum ada prediksi UP resolved"
    # Baseline BEDA per horizon (UP: h1 32,9% / h3 37,2% / h5 38,7% — rentang 5,8 pp), jadi
    # satu baseline tak boleh dipukul-rata ke DB campur. Dievaluasi per horizon DOMINAN;
    # horizon lain dilaporkan tapi tak ikut memutuskan (sampelnya selalu lebih tipis).
    by_h: dict[int, list] = {}
    for r in rows:
        by_h.setdefault(int(r["h"] or 3), []).append(
            {"ts": r["d"], "win": r["outcome"] == "win"})
    dom = max(by_h, key=lambda k: len(by_h[k]))
    # side="below": item ini membuktikan arah UP LEMAH. Win-rate yang justru lebih BAIK
    # dari baseline tak boleh meloloskan gerbang "kalibrasi bermasalah".
    v = _eval.verdict_winrate(by_h[dom], _eval.baseline_for("UP", dom), side="below")
    lain = ", ".join(f"h{h}:n={len(x)}" for h, x in sorted(by_h.items()) if h != dom)
    return v["pass"], f"[horizon dominan h{dom}] {v['detail']}" + (f" | lain: {lain}" if lain else "")


def item3_foreign_scoring(c) -> tuple[bool, str]:
    """Arus asing ke SCORING — PASS kalau A/B berpasangan per-tanggal membuktikan lebih baik.

    Bar LAMA ("oos_with > oos_without + 1pt") membandingkan dua persentase tanpa CI; selisih
    1pt pada n=40 sepenuhnya di dalam derau. Sekarang selisih diukur DI TANGGAL YANG SAMA
    (kontrol rezim) lalu di-bootstrap blok — lolos hanya kalau CI selisih > 0."""
    # HANYA UP/DOWN: prediksi FLAT punya semantik "menang" yang berbeda (menang = harga
    # diam), dan `agree` tak terdefinisi untuknya — memasukkannya ke grup B membuat A-B
    # mengukur "searah-asing vs (berlawanan + FLAT)", bukan dengan-vs-tanpa konfirmasi asing.
    rows = c.execute(
        "SELECT substr(ts,1,10) d, direction, outcome, factors_json "
        "FROM predictions WHERE status='resolved' AND outcome IS NOT NULL "
        "AND factors_json IS NOT NULL AND direction IN ('UP','DOWN') ORDER BY id"
    ).fetchall()
    recs = []
    for r in rows:
        try:
            fp = (json.loads(r["factors_json"]) or {}).get("foreign_pressure")
        except Exception:  # noqa: BLE001
            fp = None
        if fp is None:
            continue
        agree = (r["direction"] == "UP" and fp > 0) or (r["direction"] == "DOWN" and fp < 0)
        # A = sinyal SEARAH arus asing; B = sisanya. Dibandingkan di tanggal yang sama.
        recs.append({"ts": r["d"], "win": r["outcome"] == "win",
                     "group": "a" if agree else "b"})
    if not recs:
        return False, "belum ada prediksi resolved ber-foreign_pressure (kolektor jalan di save_prediction)"
    v = _eval.verdict_paired(recs)
    return v["pass"], v["detail"]


def item1_policy_sector(c) -> tuple[bool, str]:
    """Policy->sector mapping — PASS kalau ada >=10 kasus TERKONFIRMASI manual.
    Bar: data/policy_cases.json = list kasus {berita, sektor, arah_terjadi:true}; butuh >=10 confirmed."""
    f = DATA / "policy_cases.json"
    cases = []
    if f.exists():
        try:
            cases = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cases = []
    confirmed = [x for x in cases if x.get("arah_terjadi") is True]
    return (len(confirmed) >= 10), f"{len(confirmed)}/10 kasus terkonfirmasi (data/policy_cases.json)"


ITEMS = [
    ("1. Policy->sector mapping (MBG dll)", item1_policy_sector),
    ("2. Kalibrasi lebih cepat", item2_calibration),
    ("3. Arus asing ke scoring heuristik", item3_foreign_scoring),
]


def main() -> None:
    c = _conn()
    print("=" * 64)
    print("  GERBANG BUKTI NEXT_UPDATES (data yang memutuskan, bukan opini)")
    print("=" * 64)
    any_pass = False
    for name, fn in ITEMS:
        ok, detail = fn(c)
        any_pass = any_pass or ok
        print(f"  [{'PASS ✅ BUILD' if ok else 'HOLD ⛔'}] {name}")
        print(f"       {detail}")
    print("-" * 64)
    print("  -> Bangun HANYA item ber-PASS. HOLD = tunggu data, JANGAN bangun.")
    if not any_pass:
        print("  -> Semua HOLD: tak ada yang boleh dibangun sekarang.")


if __name__ == "__main__":
    main()
