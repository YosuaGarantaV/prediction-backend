"""Engine paper trading: beli/jual uang palsu, lot 100 lembar, fee khas IDX."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import config
from app import repo

LOT = 100  # 1 lot IDX = 100 lembar


def mark_to_market() -> float:
    """Total nilai posisi berdasarkan harga terbaru."""
    equity = 0.0
    for pos in repo.get_positions():
        q = repo.get_quote(pos["ticker"])
        price = q["price"] if q else pos["avg_price"]
        equity += pos["qty"] * price
    return equity


def snapshot() -> dict:
    return repo.snapshot_portfolio(repo.get_cash(), mark_to_market())


def buy(ticker: str, price: float, size_pct: float, *, reason: str = "",
        prediction_id: int | None = None) -> dict | None:
    if price <= 0:
        return None
    cash = repo.get_cash()
    total = cash + mark_to_market()

    positions = repo.get_positions()
    holding = repo.get_position(ticker)
    if not holding and len(positions) >= config.MAX_POSITIONS:
        repo.log("trader", "trade", f"{ticker}: skip BUY, posisi penuh "
                 f"({config.MAX_POSITIONS})", level="warn", ticker=ticker)
        return None

    alloc = total * min(size_pct, config.MAX_ALLOC_PER_TRADE * 100) / 100
    alloc = min(alloc, cash)
    # CAP KUMULATIF PER-TICKER (Aturan 26: maks ~15%/posisi). Bug nyata 2026-07-03: 6x BUY ANTM
    # masing-masing lolos cap per-trade → menumpuk jadi 60% portofolio. Averaging boleh, tapi
    # total nilai posisi ticker tak boleh melewati MAX_ALLOC_PER_TRADE dari total ekuitas.
    if holding and holding["qty"] > 0:
        # ANTI-AVERAGING-DOWN (forensik 2026-07-17): posisi MERAH dilarang ditambah. Prediksi
        # open yang sama memicu act_on_decision TIAP siklus 12 mnt → JELI dibeli 6x @995→930
        # (falling knife) sampai cap 15% penuh = -Rp1.09jt (92% drawdown akun). Cap membatasi
        # EKSPOSUR, bukan pendarahan. Nambah hanya boleh saat posisi hijau (pyramiding winner).
        if price < holding["avg_price"]:
            repo.log("trader", "trade", f"{ticker}: skip BUY, posisi merah "
                     f"(harga {price} < avg {holding['avg_price']}) — anti-averaging-down",
                     level="warn", ticker=ticker)
            return None
        held_val = holding["qty"] * price
        room = total * config.MAX_ALLOC_PER_TRADE - held_val
        if room <= 0:
            repo.log("trader", "trade", f"{ticker}: skip BUY, posisi sudah "
                     f"{held_val / total * 100:.0f}% portofolio (cap "
                     f"{config.MAX_ALLOC_PER_TRADE * 100:.0f}%)", level="warn", ticker=ticker)
            return None
        alloc = min(alloc, room)
    # Anti-receh: saat kas nyaris habis (commit beruntun menguras kas dalam 1 siklus), sisa
    # receh tetap kebeli jadi 1 lot sampah + bayar fee 2×. Lewati kalau alokasi < 1% modal.
    if alloc < total * 0.01:
        repo.log("trader", "trade", f"{ticker}: skip BUY, alokasi receh "
                 f"(Rp{alloc:,.0f} < 1% modal)", level="warn", ticker=ticker)
        return None
    qty = math.floor(alloc / price / LOT) * LOT
    if qty < LOT:
        repo.log("trader", "trade", f"{ticker}: skip BUY, alokasi < 1 lot",
                 level="warn", ticker=ticker)
        return None

    gross = qty * price
    fee = gross * config.FEE_BUY
    net = gross + fee
    if net > cash:
        repo.log("trader", "trade", f"{ticker}: skip BUY, kas kurang "
                 f"(butuh Rp{net:,.0f}, kas Rp{cash:,.0f})", level="warn", ticker=ticker)
        return None

    # avg_price = BASIS BIAYA termasuk fee beli (net, bukan gross). Fix 2026-07-17: dulu pakai
    # gross → realized (net_jual - qty*avg) melebihkan untung sebesar fee beli (~0.15%/round-trip)
    # & ambang stop/profit sedikit optimis. net/qty = basis biaya per lembar yang benar.
    if holding:
        new_qty = holding["qty"] + qty
        new_avg = (holding["qty"] * holding["avg_price"] + net) / new_qty
        opened = holding["opened_at"]
    else:
        new_qty, new_avg, opened = qty, net / qty, repo.now_iso()

    repo.upsert_position(ticker, new_qty, round(new_avg, 2), opened)
    trade = {"ticker": ticker, "side": "BUY", "qty": qty, "price": price,
             "gross": round(gross, 2), "fee": round(fee, 2), "net": round(net, 2),
             "reason": reason, "prediction_id": prediction_id}
    repo.record_trade(trade)
    repo.snapshot_portfolio(cash - net, mark_to_market())
    repo.log("trader", "trade", f"BELI {ticker} {qty} lembar @ {price} "
             f"(Rp{net:,.0f})", ticker=ticker, payload=trade)
    return trade


def sell(ticker: str, price: float, *, reason: str = "",
         prediction_id: int | None = None, force: bool = False) -> dict | None:
    holding = repo.get_position(ticker)
    if not holding or price <= 0:
        return None
    # ANTI-CHURN MIN-HOLD (guardrail biaya): jual DISKRESIONER (agen memutuskan SELL) < MIN_HOLD_H
    # jam setelah beli, saat P/L di zona NOISE = bayar fee round-trip 0.4% untuk gerakan tak
    # berarti. FORENSIK 2026-07-13: 85% posisi dijual <24 jam → fee total Rp4.7jt di akun -Rp1.5jt
    # (tanpa churn akun UNTUNG ~Rp3.2jt). Bencana terparah 24-26 Jun: 127 trade/3hari = Rp2.5jt fee.
    # force=True (exit pengaman check_exits: stop-loss/take-profit/kelamaan) SELALU lolos — ini
    # cuma menahan flip-jual noise diskresioner. Gerak BESAR (<=-6% tesis rusak / >=+10% profit
    # nyata) tetap boleh keluar walau dalam min-hold.
    try:
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(holding["opened_at"])).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        age_h = 999.0
    plp = (price / holding["avg_price"] - 1) * 100 if holding.get("avg_price") else 0.0
    if not force and age_h < config.MIN_HOLD_H and -6.0 < plp < 10.0:
        repo.log("trader", "trade", f"{ticker}: skip SELL, baru dipegang {age_h:.1f} jam "
                 f"(P/L {plp:+.1f}% zona noise) — min-hold {config.MIN_HOLD_H} jam anti-churn",
                 level="warn", ticker=ticker)
        return None
    qty = holding["qty"]
    gross = qty * price
    fee = gross * config.FEE_SELL
    net = gross - fee
    realized = net - qty * holding["avg_price"]

    repo.upsert_position(ticker, 0, 0, holding["opened_at"])
    cash = repo.get_cash()
    trade = {"ticker": ticker, "side": "SELL", "qty": qty, "price": price,
             "gross": round(gross, 2), "fee": round(fee, 2), "net": round(net, 2),
             "reason": f"{reason} | realized P/L Rp{realized:,.0f}",
             "prediction_id": prediction_id}
    repo.record_trade(trade)
    repo.snapshot_portfolio(cash + net, mark_to_market())
    repo.log("trader", "trade", f"JUAL {ticker} {qty} lembar @ {price} "
             f"(realized Rp{realized:,.0f})", ticker=ticker, payload=trade)
    return trade


def check_exits() -> list[str]:
    """Jaring pengaman: auto-jual posisi yang kena stop-loss / take-profit / kelamaan.
    Agen tetap bisa jual lebih awal lewat keputusannya; ini cuma backstop anti-nyangkut."""
    sold = []
    for p in repo.get_positions():
        q = repo.get_quote(p["ticker"])
        if not q or not p["avg_price"]:
            continue
        pl = (q["price"] / p["avg_price"] - 1) * 100
        try:
            days = (datetime.now(timezone.utc) - datetime.fromisoformat(p["opened_at"])).days
        except Exception:  # noqa: BLE001
            days = 0
        reason = None
        if pl <= config.STOP_LOSS_PCT:
            reason = f"stop-loss ({pl:.1f}%)"
        elif pl >= config.TAKE_PROFIT_PCT:
            reason = f"take-profit (+{pl:.1f}%)"
        elif days >= config.MAX_HOLD_DAYS:
            reason = f"dipegang {days} hari (terlalu lama)"
        if reason:
            # force=True: exit pengaman WAJIB jalan (lewati min-hold anti-churn) — mencegah
            # ruin lebih penting dari menghemat 1 fee.
            sell(p["ticker"], q["price"], reason="auto-exit: " + reason, force=True)
            sold.append(p["ticker"])
    return sold


def pre_trade_checks(decision: dict) -> tuple[str, str, float | None]:
    """Gerbang risiko pra-trade — SUMBER TUNGGAL semua veto/cap (murni baca, tanpa eksekusi).
    Return (verdict, reason, size_cap): APPROVE | RESIZE (size_cap %) | VETO.
    Dipakai Risk Agent (node graf, sebelum commit) DAN act_on_decision (re-check saat commit,
    defense in depth). Urutan cek = urutan act_on_decision lama (log tetap sebanding)."""
    ticker = decision["ticker"]
    action = decision.get("action", "HOLD")
    if action == "HOLD":
        return "APPROVE", "HOLD — tanpa eksekusi", None

    # Aturan IDX: jangan beli saham yang sudah ~ARA (antrian beli, tak realistis terisi),
    # jangan jual ke ~ARB (antrian jual menumpuk).
    q = repo.get_quote(ticker)
    chg = (q.get("change_pct") or 0.0) if q else 0.0
    if action == "SELL":
        if chg <= -20:
            return "VETO", f"skip SELL, sudah {chg}% (kena ARB)", None
        # Jual karena "sinyal hilang" adalah biang fee (audit 2026-09-08, DB live 8 Sep):
        # 44 dari 54 SELL dipicu re-prediksi FLAT ber-keyakinan 33-45%, sementara median
        # round-trip bergerak 0,00% dan fee round-trip 0,40% — 76 round-trip menghasilkan
        # gerak harga -Rp1,92 jt tapi fee -Rp2,54 jt (57% dari total rugi). Harga sesudah
        # exit itu justru +2,67% di hari bursa ke-10 (n=46), jadi exit-nya merusak nilai.
        # FLAT = tak ada sinyal, bukan tesis rusak. DOWN tetap boleh jual, dan jaring
        # pengaman check_exits (stop-loss / take-profit / MAX_HOLD_DAYS 15 hari) memanggil
        # sell(force=True) TANPA melewati gerbang ini, jadi posisi tetap punya jalan keluar.
        # `_arm_closed_from` = FLAT hasil tulis-ulang _proven_loss_gate atas tesis berarah;
        # exit-nya tetap sah (lihat catatan gerbang itu: "SELL tak diblokir").
        if ((decision.get("direction") or "").upper() == "FLAT"
                and not decision.get("_arm_closed_from")):
            return "VETO", ("skip SELL, arah FLAT = tak ada sinyal (anti-churn fee; exit "
                            "tetap lewat stop-loss/take-profit/MAX_HOLD_DAYS)"), None
        return "APPROVE", "SELL lolos gerbang risiko", None

    # --- BUY ---
    if chg >= 20:
        return "VETO", f"skip BUY, sudah +{chg}% (dekat ARA)", None

    # Edge tipis vs fee (forensik 2026-07-07): BUY ber-ekspektasi <MIN_EDGE_PCT kalah oleh
    # fee round-trip ~0.4% (exp<2%: 21% win, -Rp1.36jt; exp>=2%: 39% win, +Rp1.24jt).
    exp = decision.get("expected_pct")
    if exp is not None and float(exp) < config.MIN_EDGE_PCT:
        return "VETO", (f"VETO BUY — edge tipis (exp {float(exp):.1f}% < "
                        f"{config.MIN_EDGE_PCT:.1f}%; fee round-trip memakan edge)"), None

    # Anti-churn: jangan BELI ticker yang BARU saja dijual (whipsaw bayar fee 2× & kunci rugi).
    # Hanya berlaku untuk POSISI BARU — menambah (averaging) posisi yang dipegang tetap boleh.
    holding = repo.get_position(ticker)
    if not holding:
        last = repo.last_sell_ts(ticker)
        if last:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
            if age_h < config.REENTRY_COOLDOWN_HOURS:
                return "VETO", (f"skip BUY, baru dijual {age_h:.1f} jam lalu "
                                f"(cooldown {config.REENTRY_COOLDOWN_HOURS}j, anti-churn)"), None
    else:
        # Satu sinyal = satu entri per hari. Audit 2026-09-08: 17 entri ekstra dari 12 pasangan
        # (tanggal, ticker) — COIN 3x dalam 85 menit di harga 845-850, JELI 6x pada 17 Jul
        # (ticker itu sendiri -Rp1,68 jt = 38% seluruh kerugian). Siklus engine mengulang tesis
        # yang sama tiap putaran; tiap pengulangan membayar fee beli 0,15% tanpa informasi baru.
        # Averaging LINTAS HARI tetap boleh (situasi & harga sudah berubah).
        last_buy = repo.last_buy_ts(ticker)
        if last_buy:
            try:
                same_day = (datetime.fromisoformat(last_buy).astimezone(repo._WIB).date()
                            == datetime.now(repo._WIB).date())
            except (TypeError, ValueError):
                same_day = False
            if same_day:
                return "VETO", ("skip BUY, sudah beli ticker ini hari ini "
                                "(1 sinyal = 1 entri/hari, anti-churn fee)"), None

    # Veto BELI deterministik (knowledge rule 29 falling-knife + valuasi ekstrem) — tak
    # bisa diabaikan LLM. Inilah yg bikin TCPI (PER 297, -17%/3hr) keliru dibeli.
    feats = json.loads(q.get("features_json") or "{}") if q else {}
    f = repo.get_fundamentals_row(ticker) or {}
    cum3, macd = feats.get("cum_change_3d", 0), feats.get("macd_hist", 0)
    per, pbv = f.get("per"), f.get("pbv")
    if cum3 <= -12 and macd < 0:
        return "VETO", f"VETO BUY — falling knife ({cum3}%/3hr, MACD<0)", None
    if (per and per > 100) or (pbv and pbv > 12):
        return "VETO", f"VETO BUY — kemahalan ekstrem (PER {per}, PBV {pbv})", None
    # VETO arus asing DICABUT 2026-08-31 — dasarnya tak bertahan saat diuji ulang.
    # Klaim lama: "win-rate UP cuma 18% di rezim asing-jualan" → VETO BUY saat fp <= -0,4.
    # Diukur atas 974 prediksi resolved yang menyimpan foreign_pressure (collector di
    # repo.save_prediction): korelasi fp dgn actual_pct = -0,0105 (nol); prediksi yang arus
    # asingnya SEARAH menang 47,0% vs yang berlawanan 47,7% — searah malah sedikit lebih
    # buruk; verdict_paired per-tanggal -4,8 pp dgn CI [-20,2, +7,9] = tak terbukti. Arah
    # besarannya pun terbalik: fp>+0,2 rata-rata +0,578%, fp<-0,2 rata-rata +0,655%.
    # Veto satu-arah (hanya memblokir BELI) di atas fitur ber-korelasi nol = rem yang menahan
    # mesin tanpa alasan terukur. Fitur ini TIDAK dibuang: app/agents/factors.py tetap
    # membobotinya lewat muted_factors (dinilai per-faktor dari papan skor), dan idxflow kini
    # mengarsipkan snapshot per tanggal supaya suatu saat bisa divonis, bukan diasumsikan.

    # Kapasitas portofolio (cek buy() diangkat ke sini agar Risk Agent melihatnya SEBELUM
    # commit; buy() tetap mengecek ulang sendiri — defense in depth, bukan duplikasi bug).
    try:
        if not holding and len(repo.get_positions()) >= config.MAX_POSITIONS:
            return "VETO", f"skip BUY, posisi penuh ({config.MAX_POSITIONS})", None
        size = float(decision.get("size_pct") or 0.0)
        cap = config.MAX_ALLOC_PER_TRADE * 100
        price = decision.get("entry_price") or (q.get("price") if q else 0.0) or 0.0
        if holding and holding["qty"] > 0 and price > 0:
            total = repo.get_cash() + mark_to_market()
            if total > 0:
                held_pct = holding["qty"] * price / total * 100
                room = cap - held_pct
                if room <= 0:
                    return "VETO", (f"skip BUY, posisi sudah {held_pct:.0f}% portofolio "
                                    f"(cap {cap:.0f}%)"), None
                cap = min(cap, room)
        if size > cap:
            return "RESIZE", f"size {size:.0f}% > ruang {cap:.1f}% — dipangkas", round(cap, 1)
    except Exception:  # noqa: BLE001 — cek kapasitas gagal (DB) → serahkan ke re-check buy()
        pass
    return "APPROVE", "BUY lolos semua gerbang risiko", None


def act_on_decision(decision: dict, prediction_id: int | None) -> None:
    """Eksekusi keputusan agen dengan disiplin risiko + aturan IDX (ARA/ARB).
    Semua gerbang di pre_trade_checks (sumber tunggal); di sini tinggal terapkan."""
    ticker = decision["ticker"]
    price = decision.get("entry_price") or 0.0
    action = decision.get("action", "HOLD")
    reason = (decision.get("reasoning") or "")[:200]

    verdict, why, size_cap = pre_trade_checks(decision)
    if verdict == "VETO":
        repo.log("trader", "trade", f"{ticker}: {why}", level="warn", ticker=ticker)
        return
    size = decision.get("size_pct", 0)
    if verdict == "RESIZE" and size_cap is not None:
        repo.log("trader", "trade", f"{ticker}: {why}", level="warn", ticker=ticker)
        size = min(size, size_cap)

    if action == "BUY" and size > 0:
        buy(ticker, price, size, reason=reason, prediction_id=prediction_id)
    elif action == "BUY":
        # Audit 2026-08-10: dua-satunya jalur uang yang gagal TANPA jejak. CTO adalah LLM, jadi
        # action=BUY dgn size_pct 0/None mungkin saja terjadi — dulu keputusan itu lenyap begitu
        # saja walau gerbang risiko sudah menuliskan "APPROVE — BUY lolos". Sekarang berbunyi.
        repo.log("trader", "trade", f"{ticker}: BUY BATAL — size_pct={size!r} "
                 f"(gerbang lolos tapi CTO tak memberi ukuran posisi)", level="warn", ticker=ticker)
    elif action == "SELL":
        sell(ticker, price, reason=reason, prediction_id=prediction_id)
    # HOLD -> tidak ada aksi
