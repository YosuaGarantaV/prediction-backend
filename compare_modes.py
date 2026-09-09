"""PEMBANDING A/B minggu agent vs pipeline. Jalankan: python compare_modes.py

Membaca data/prediction.db: win-rate per mode (predictions.factors_json.engine_mode),
biaya token per mode (agent_logs usage payload.mode), perilaku agen (frekuensi tool-call,
fallback, konsultasi dewan). VERDICT dari DATA, bukan opini — dipakai minggu depan untuk
memutuskan hybrid (komponen pemenang dipermanenkan).

Limitasi (baca sebelum menyimpulkan): dua minggu = dua rezim pasar berbeda, BUKAN A/B sejati;
digest-belajar 14 hari hanya aktif di mode agent (variabel kedua yang disengaja user).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent / "data" / "prediction.db"
MIN_N = 30  # minimum prediksi resolved per mode sebelum verdict dianggap kuat


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def prediction_stats(c) -> dict[str, dict]:
    """Per mode: n dibuat, n resolved, win-rate total & per arah."""
    out: dict[str, dict] = {}
    for r in c.execute("SELECT direction, status, outcome, factors_json FROM predictions"):
        try:
            mode = (json.loads(r["factors_json"]) or {}).get("engine_mode") or "pipeline"
        except Exception:  # noqa: BLE001
            mode = "pipeline"  # prediksi lama (pra-tagging) = era pipeline
        m = out.setdefault(mode, {"made": 0, "resolved": 0, "wins": 0, "dir": {}})
        m["made"] += 1
        if r["status"] == "resolved":
            m["resolved"] += 1
            d = m["dir"].setdefault(r["direction"], {"n": 0, "w": 0})
            d["n"] += 1
            if r["outcome"] == "win":
                m["wins"] += 1
                d["w"] += 1
    return out


def token_stats(c) -> dict[str, dict]:
    """Per mode: total token & jumlah call LLM (usage payload sejak tagging aktif)."""
    out: dict[str, dict] = {}
    for r in c.execute("SELECT payload_json FROM agent_logs WHERE phase='usage'"):
        try:
            p = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except Exception:  # noqa: BLE001
            continue
        mode = p.get("mode")
        if not mode:
            continue  # log lama pra-tagging: tak bisa diatribusi, jangan dikarang
        m = out.setdefault(mode, {"tokens": 0, "calls": 0})
        m["tokens"] += p.get("total_tokens") or 0
        m["calls"] += 1
    return out


def agent_behavior(c) -> dict:
    """Frekuensi perilaku agen dari log (jalur tool, fallback, konsultasi dewan)."""
    q = ("SELECT "
         "SUM(message LIKE '%jalur tool,%') tool_ok, "
         "SUM(message LIKE '%jalur tool gagal%' OR message LIKE '%jalur tool CTO gagal%') tool_fail, "
         "SUM(message LIKE '%[agen:%dewan 0x]%') cto_no_council, "
         "SUM(message LIKE '%[agen:%dewan%' AND message NOT LIKE '%dewan 0x]%') cto_council, "
         "SUM(message LIKE '%dewan dikonsultasikan%') scripted_council "
         "FROM agent_logs")
    r = c.execute(q).fetchone()
    return {k: r[k] or 0 for k in r.keys()}


def main() -> None:
    if not DB.exists():
        print("DB belum ada:", DB)
        return
    c = _conn()
    preds = prediction_stats(c)
    toks = token_stats(c)
    beh = agent_behavior(c)

    print("=" * 64)
    print("A/B AGENT vs PIPELINE — dari data/prediction.db")
    print("=" * 64)
    wr = {}
    for mode in ("pipeline", "agent"):
        p = preds.get(mode, {"made": 0, "resolved": 0, "wins": 0, "dir": {}})
        t = toks.get(mode, {"tokens": 0, "calls": 0})
        wr[mode] = 100 * p["wins"] / p["resolved"] if p["resolved"] else None
        tpp = t["tokens"] / p["made"] if p["made"] and t["tokens"] else None
        print(f"\n[{mode.upper()}]")
        print(f"  prediksi dibuat={p['made']}  resolved={p['resolved']}  "
              f"win-rate={'%.0f%%' % wr[mode] if wr[mode] is not None else '-'}")
        for d, v in sorted(p["dir"].items()):
            print(f"    {d}: {v['w']}/{v['n']} ({100*v['w']/v['n']:.0f}%)")
        print(f"  token LLM tertag={t['tokens']:,}  call={t['calls']}  "
              f"token/prediksi={'%.0f' % tpp if tpp else '- (log pra-tagging tak dihitung)'}")

    print(f"\n[PERILAKU AGEN]")
    print(f"  analis jalur tool sukses={beh['tool_ok']}  fallback={beh['tool_fail']}")
    print(f"  CTO konsultasi dewan (tool)={beh['cto_council']}  "
          f"tanpa dewan={beh['cto_no_council']}  trigger skrip lama={beh['scripted_council']}")

    # ---- VERDICT (aturan ditulis SEBELUM data masuk, anti-bias) ----
    print("\n" + "=" * 64)
    pa, pp = preds.get("agent", {}), preds.get("pipeline", {})
    na, np_ = pa.get("resolved", 0), pp.get("resolved", 0)
    if na < MIN_N or np_ < MIN_N:
        print(f"VERDICT: BELUM KONKLUSIF — butuh >= {MIN_N} resolved per mode "
              f"(agent={na}, pipeline={np_}). Biarkan jalan.")
        return
    ta, tp = toks.get("agent", {}), toks.get("pipeline", {})
    tpp_a = ta.get("tokens", 0) / max(pa.get("made", 1), 1)
    tpp_p = tp.get("tokens", 0) / max(pp.get("made", 1), 1)
    acc_ok = wr["agent"] is not None and wr["pipeline"] is not None \
        and wr["agent"] >= wr["pipeline"] - 2.0
    tok_ok = tpp_a and tpp_p and tpp_a < tpp_p
    if not (tpp_a and tpp_p):
        # Satu sisi tanpa token tertag (log pra-tagging) → membandingkan token = mengarang.
        # Verdict jujur: akurasi saja; token agent dibandingkan ke baseline internalnya sendiri.
        side = "AGENT" if acc_ok else "PIPELINE"
        print(f"VERDICT (AKURASI SAJA — token tak sebanding, satu mode tanpa log tertag): "
              f"{side} unggul (agent {wr['agent']:.1f}% vs pipeline {wr['pipeline']:.1f}%).\n"
              f"Token agent {tpp_a:,.0f}/prediksi — pantau trennya sendiri; token pipeline "
              f"baru terukur bila AGENT_MODE=false dijalankan lagi beberapa hari.")
        return
    if acc_ok and tok_ok:
        print("VERDICT: AGENT MENANG (win-rate >= pipeline-2pt DAN token/prediksi lebih rendah)."
              "\nHybrid: pertahankan mode agent; pertimbangkan matikan komponen yang tak dipakai.")
    elif not acc_ok:
        print("VERDICT: PIPELINE MENANG di akurasi. Hybrid: kembalikan AGENT_MODE=false; "
              "pertahankan komponen agent yang netral-akurasi tapi hemat (cek angka di atas).")
    else:
        print("VERDICT: PIPELINE MENANG di token (akurasi setara). Hybrid: pakai pipeline "
              "inject-all; pertimbangkan pertahankan digest-belajar (murah).")
    print("\nCatatan: 2 minggu = 2 rezim pasar; verdict kuat kalau margin jelas. "
          "Digest-belajar hanya di mode agent = variabel kedua (disengaja).")


if __name__ == "__main__":
    main()
