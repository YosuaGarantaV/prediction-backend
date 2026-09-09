"""Buat laporan harian (markdown) — inilah yang kamu kirim ke Claude tiap hari untuk di-tune."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import config
from app import backtest, events, repo
from app.agents import skill
from app.data import flow as flow_mod


def build_markdown() -> str:
    pf = repo.latest_portfolio()
    stats = repo.prediction_stats()
    positions = repo.get_positions()
    trades = repo.recent_trades(15)
    preds = repo.recent_predictions(20)
    lessons = repo.recent_lessons(10)
    now = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M")  # WIB eksplisit

    lines = [
        f"# Laporan Harian Saham IDX Prediction Engine",
        f"_Dibuat: {now} WIB | Mode: {'LLM' if config.USE_LLM else 'Heuristik'}_",
        "",
        "## 1. Portofolio (paper money)",
        f"- Modal awal: Rp{config.START_CASH:,.0f}",
        f"- Kas: Rp{pf['cash']:,.0f}",
        f"- Nilai posisi: Rp{pf['equity']:,.0f}",
        f"- **Total: Rp{pf['total']:,.0f}**",
        f"- **P/L: Rp{pf['pnl']:,.0f} ({pf['pnl_pct']:+.2f}%)**",
        "",
        "## 2. Akurasi prediksi",
        f"- Prediksi selesai dievaluasi: {stats['resolved']}",
        f"- Menang: {stats['wins']} | Kalah: {stats['losses']} | "
        f"**Win-rate: {stats['win_rate']}%**",
    ]
    # UJI ALPHA: win-rate mentah bisa palsu (ikut pasar); alpha = kalahkan pasar (skill nyata).
    alpha = repo.alpha_scoreboard()
    if alpha:
        lines += ["", "### 2a. Uji alpha (mentah vs relatif-IHSG)",
                  "| arah | n | mentah | alpha (vs pasar) | excess |",
                  "|---|--:|--:|--:|--:|"]
        for a in alpha:
            gap = a["raw_win_rate"] - a["alpha_win_rate"]
            note = " (beta/ikut-pasar)" if gap >= 10 else " (skill relatif)" if a["alpha_win_rate"] >= 53 else ""
            lines.append(f"| {a['direction']} | {a['n']} | {a['raw_win_rate']}% | "
                         f"{a['alpha_win_rate']}%{note} | {a['avg_excess']:+}% |")
    lines += ["", "## 3. Posisi terbuka"]
    if positions:
        for p in positions:
            q = repo.get_quote(p["ticker"])
            price = q["price"] if q else p["avg_price"]
            pl = (price / p["avg_price"] - 1) * 100
            lines.append(f"- {p['ticker']}: {p['qty']:.0f} lembar @ {p['avg_price']} "
                         f"(now {price}, {pl:+.1f}%)")
    else:
        lines.append("- (tidak ada posisi)")

    lines += ["", "## 4. Transaksi terakhir"]
    for t in trades:
        lines.append(f"- {t['ts'][:16]} {t['side']} {t['ticker']} {t['qty']:.0f} @ {t['price']}")
    if not trades:
        lines.append("- (belum ada transaksi)")

    # Penggerak hari ini + arah ASING per saham — inti spectate: naik/turun BERSAMA asing
    # (institusi) vs DILAWAN asing (pump ritel / distribusi). Kompak, 1 baris per saham.
    try:
        from app.data import idxflow
        quotes = [q for q in repo.all_quotes()
                  if (q.get("price") or 0) * (q.get("volume") or 0) > 5e9]  # likuid saja
        quotes.sort(key=lambda q: q.get("change_pct") or 0)
        movers = quotes[-5:][::-1] + quotes[:5]
        lines += ["", "## 4b. Penggerak hari ini (likuid) + arus asing"]
        for q in movers:
            fp = idxflow.foreign_pressure(q["ticker"])
            asing = ("asing BELI" if fp is not None and fp >= 0.15 else
                     "asing JUAL" if fp is not None and fp <= -0.15 else "asing netral")
            tag = ("didukung institusi" if ((q["change_pct"] or 0) > 0) == (fp is not None and fp > 0.15)
                   and fp is not None and abs(fp) >= 0.15 else
                   "DILAWAN asing (rawan)" if fp is not None and abs(fp) >= 0.15 else "")
            lines.append(f"- {q['ticker']}: {q['change_pct']:+.1f}% | {asing}"
                         f" ({fp:+.2f})" if fp is not None else
                         f"- {q['ticker']}: {q['change_pct']:+.1f}% | (tanpa data asing)")
            if tag:
                lines[-1] += f" → {tag}"
    except Exception:  # noqa: BLE001
        pass

    wib = timezone(timedelta(hours=7))

    def _dibuat(ts: str) -> str:
        try:
            d = datetime.fromisoformat(ts)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(wib).strftime("%d/%m %H:%M")
        except Exception:  # noqa: BLE001
            return (ts or "")[:16]

    lines += ["", "## 5. Prediksi terbaru"]
    for p in preds:
        st = p["outcome"] or p["status"]
        lines.append(f"- {p['ticker']}: {p['direction']} {p['probability']:.0f}% "
                     f"/{p['horizon_days']}h, target {p['target_price']} "
                     f"(dibuat {_dibuat(p['ts'])} WIB) [{st}]")
        # KENAPA (dari narasi deterministik _narrate): teknikal + berita pendukung —
        # pembaca laporan melihat alasannya, bukan cuma arah & angka.
        for ln in (p.get("reasoning") or "").split("\n"):
            if ln.startswith(("Teknikal:", "Berita:")):
                lines.append(f"  - {ln[:160]}")

    lines += ["", "## 6. Pelajaran yang dipetik agen"]
    for ls in lessons:
        lines.append(f"- [{ls['kind']}] {ls['lesson']}")
    if not lessons:
        lines.append("- (belum ada, butuh prediksi yang jatuh tempo dulu)")

    # LEDGER HARIAN — kurva ekuitas + akurasi per hari (evaluasi trading, deteksi hari buruk).
    led = repo.daily_ledger(14)
    lines += ["", "## 5b. Ledger harian (14 hari terakhir)",
              "| tanggal | ekuitas | PnL hari | trade | pred | resolved | win-rate |",
              "|---|--:|--:|--:|--:|--:|--:|"]
    for x in led:
        wr = f"{x['win_rate']}%" if x["win_rate"] is not None else "-"
        lines.append(f"| {x['date']} | {x['equity']:,} | {x['pnl_day']:+,} | "
                     f"{x['trades']} | {x['predictions']} | {x['resolved']} | {wr} |")

    # EVALUASI FAKTOR STRUKTURAL — win-rate tiap faktor bernama (mana yg bekerja vs noise).
    board = repo.factor_scoreboard()
    lines += ["", "## 6b. Evaluasi faktor struktural (fired → win-rate)"]
    if board:
        for f in board:
            flag = "✅" if f["win_rate"] >= 55 else ("⚠️" if f["win_rate"] >= 45 else "❌")
            lines.append(f"- {flag} `{f['factor']}`: {f['win']}/{f['n']} = {f['win_rate']}% "
                         f"(fired {f['n']}x)")
    else:
        lines.append("- (belum cukup data — faktor baru, terisi seiring prediksi jatuh tempo)")

    sk = skill.agent_skill()
    bt = backtest.load_result()
    lines += [
        "", "## 7. Skill agen & backtest (anti-overfit)",
        f"- Level: {sk['level']} — {sk['level_label']}",
        f"- Win-rate live (terkalibrasi): {sk['calibrated_win_rate']}% (n={sk['resolved']}) | "
        f"Brier {sk['brier']}",
        f"- Backtest out-of-sample: {sk['backtest_oos']}% | {bt.get('verdict','(belum)')}",
    ]
    if bt.get("calibration"):
        cal = " | ".join(f"{c['range']}%→{c['realized']}%(n{c['n']})" for c in bt["calibration"])
        lines.append(f"- Kalibrasi: {cal}")
    if bt.get("horizons"):
        hz = " | ".join(f"{h['horizon']}h:OOS{h['oos']}%" for h in bt["horizons"])
        lines.append(f"- Horizon (OOS): {hz} → terbaik {bt.get('horizon')}h")
    lines.append(f"- Arus asing (proxy): {flow_mod.flow_brief()}")

    lines += ["", "## 8. Katalis mendatang (forward-looking)"]
    for e in events.upcoming_events(21)[:8]:
        lines.append(f"- H-{e['days_ahead']} {e['name']} [{e['impact']}]")

    from app.data import news as news_mod
    ipos = news_mod.upcoming_ipos()
    lines += ["", "## 8b. IPO mendatang (dipanen dari berita)"]
    if ipos:
        for i in ipos[:8]:
            code = f" [{', '.join(i['codes'])}]" if i.get("codes") else ""
            lines.append(f"- {i['ts'][:10]}{code} {i['title']}")
    else:
        lines.append("- (belum ada IPO mendatang terdeteksi)")

    lines += ["", "## 9. Percakapan agen terbaru (Analyst <-> Trader)"]
    convs = repo.recent_conversations(6)
    if convs:
        for c in convs:
            lines.append(f"\n### {c['ticker']} — {c['summary']}")
            lines.append(f"**ANALYST:** {(c['analyst'] or '')[:700]}")
            lines.append(f"**TRADER:** {(c['trader'] or '')[:500]}")
    else:
        lines.append("- (belum ada percakapan)")

    lines += [
        "", "## 10. Untuk Claude (tuning besok)",
        "Tinjau win-rate live vs backtest OOS (waspada overfit), prediksi yang meleset, "
        "percakapan agen, dan sarankan perbaikan aturan di `app/agents/knowledge.py`.",
    ]
    return "\n".join(lines)


def save_to_file() -> str:
    md = build_markdown()
    fname = config.LOG_DIR / f"report_{datetime.now():%Y%m%d}.md"
    fname.write_text(md, encoding="utf-8")
    repo.log("engine", "resolve", f"laporan harian disimpan: {fname.name}")
    return str(fname)
