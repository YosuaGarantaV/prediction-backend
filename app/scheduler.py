"""Penjadwal 24 jam (APScheduler): refresh harga/berita, siklus agen, evaluasi, laporan."""
from __future__ import annotations

import threading

from apscheduler.schedulers.background import BackgroundScheduler

import config
from app import backtest, model, repo, report
from app.agents import knowledge, orchestrator
from app.data import anomaly, disclosure, flow, idxflow, ipo, news, stocks
from app.trading import paper

_scheduler: BackgroundScheduler | None = None
_bootstrapped = False

# --- HEMAT SCRAPING: harga IDX beku di luar jam bursa (malam/akhir pekan) → job data lokal
# tak perlu interval penuh. Pasar buka = jalan normal; tutup = paling sering tiap
# closed_every_min; None = skip total saat tutup. Makro/indeks global TIDAK di-gate
# (pasar dunia jalan 24 jam).
_gate_last: dict[str, float] = {}


def _hemat(job_id: str, fn, closed_every_min: int | None):
    import time

    def run():
        if stocks.market_phase() != "open":
            if closed_every_min is None:
                return
            if time.time() - _gate_last.get(job_id, 0) < closed_every_min * 60:
                return
        _gate_last[job_id] = time.time()
        fn()

    run.__name__ = f"hemat_{job_id}"
    return run


def _fresh(path, max_age_h: float) -> bool:
    """True kalau file output masih segar → lewati komputasi berat saat restart cepat.
    curl_cffi crash (native) → supervisor restart tiap ~1-2 jam; TANPA guard ini tiap restart
    ulang model.train + backtest (mahal) + re-fetch semua = boros API & picu rate-limit."""
    try:
        import time
        return (time.time() - path.stat().st_mtime) < max_age_h * 3600
    except Exception:  # noqa: BLE001
        return False


def _bootstrap():
    """Tarikan data awal saat start (di thread terpisah agar web cepat siap).
    Langkah BERAT (model.train / backtest) dilewati kalau output masih segar — restart pasca
    crash jadi murah & cepat, tak mengulang kerja berat yang barusan selesai."""
    global _bootstrapped
    if _bootstrapped:
        return
    _bootstrapped = True
    try:
        repo.log("engine", "fetch", "bootstrap: tarik harga, makro, indeks, berita awal...")
        stocks.refresh_all()
        flow.compute_flow()
        stocks.refresh_macro()
        stocks.fetch_indices()
        news.refresh_news()
        ipo.refresh_ipos()            # IPO resmi dari e-IPO (otoritatif)
        idxflow.refresh_foreign()     # arus asing RESMI per-saham (IDX Trading Summary)
        disclosure.refresh_disclosures()  # filing emiten IDX (primer, lebih cepat dari media)
        if _fresh(model.MODEL_FILE, 6):   # model.json < 6 jam → skip latih ulang (job terjadwal urus)
            repo.log("engine", "fetch", "bootstrap: model masih segar (<6j), lewati train")
        else:
            model.train()             # latih model statistik dari history (sblm cycle)
        orchestrator.run_cycle()      # prediksi cepat muncul dulu
        if _fresh(config.DATA_DIR / "backtest.json", 6):  # backtest < 6 jam → skip
            repo.log("engine", "fetch", "bootstrap: backtest masih segar (<6j), lewati")
        else:
            backtest.run_horizons()   # validasi aturan multi-horizon + kalibrasi
        _learning_catchup()           # cron 16:00 WIB terlewat saat engine mati → susul
    except Exception as e:  # noqa: BLE001
        repo.log("engine", "fetch", f"bootstrap error: {e}", level="error")


def _learning_catchup() -> None:
    """LOOP BELAJAR tak boleh putus: cron report 16:00 + evaluator 16:15 WIB terlewat bila
    engine mati sore (nyata 8-9 Jul 2026: engine hidup hanya pagi → 2 hari tanpa laporan &
    tanpa aturan dinamis baru). Saat start: laporan terakhir >20 jam → jalankan report +
    auto_tune sekali (nama file = hari ini; isi kumulatif — yang penting evaluator menyuling
    rekam jejak terbaru, auto_tune punya guard sendiri <10 resolved → skip)."""
    import glob
    import os
    import time
    from datetime import datetime
    try:
        # Laporan hari ini sudah ada -> cron harian 16:00 sudah/akan jalan; catch-up akan
        # MENIMPA-nya + membakar 1 panggilan LLM auto_tune percuma. Nyata 2026-07-28:
        # catch-up 13:10 menghasilkan 10 aturan, lalu cron 16:15 menghasilkan 10 aturan lagi
        # yang mengganti batch pertama. Ambang 20 jam vs cron 24 jam memang saling tumpang
        # tindih: restart apa pun di jendela 20-24 jam memicunya.
        # Jam HARUS sama dengan report.save_to_file() (datetime.now() lokal) — kalau tidak,
        # penjaga ini mencari nama file yang tak pernah ditulis.
        if (config.LOG_DIR / f"report_{datetime.now():%Y%m%d}.md").exists():
            return
        files = glob.glob(str(config.LOG_DIR / "report_*.md"))
        last = max((os.path.getmtime(f) for f in files), default=0)
        if time.time() - last < 20 * 3600:
            return
        repo.log("engine", "resolve", "catch-up: laporan/evaluator harian terlewat (engine "
                 "mati saat cron 16:00 WIB) — jalankan sekarang")
        report.save_to_file()
        knowledge.auto_tune()
    except Exception as e:  # noqa: BLE001
        repo.log("engine", "resolve", f"catch-up laporan gagal: {e}", level="warn")


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler:
        return _scheduler

    sched = BackgroundScheduler(timezone="UTC", daemon=True)
    sched.add_job(_hemat("prices", stocks.refresh_all, 60), "interval",
                  minutes=config.PRICE_REFRESH_MIN,
                  id="prices", max_instances=1, coalesce=True)
    sched.add_job(stocks.refresh_hot, "interval", minutes=config.HOT_REFRESH_MIN,
                  id="hot", max_instances=1, coalesce=True)  # live cepat hot-set (self-gated)
    sched.add_job(_hemat("flow", flow.compute_flow, 60), "interval",
                  minutes=config.PRICE_REFRESH_MIN,
                  id="flow", max_instances=1, coalesce=True)
    sched.add_job(stocks.refresh_macro, "interval", minutes=30,
                  id="macro", max_instances=1, coalesce=True)  # global 24 jam, tanpa gate
    sched.add_job(stocks.fetch_indices, "interval", minutes=30,
                  id="indices", max_instances=1, coalesce=True)
    # Berita tetap 24 jam (bahan outlook pra-buka) tapi saat tutup cukup tiap 30 menit.
    sched.add_job(_hemat("news", news.refresh_news, 30), "interval",
                  minutes=config.NEWS_REFRESH_MIN,
                  id="news", max_instances=1, coalesce=True)
    sched.add_job(_hemat("ipo", ipo.refresh_ipos, 240), "interval", minutes=60,
                  id="ipo", max_instances=1, coalesce=True)
    sched.add_job(_hemat("foreign", idxflow.refresh_foreign, 240), "interval", minutes=120,
                  id="foreign", max_instances=1, coalesce=True)  # arus asing resmi (end-of-day)
    sched.add_job(_hemat("disclosure", disclosure.refresh_disclosures, 60), "interval",
                  minutes=config.DISCLOSURE_REFRESH_MIN,
                  id="disclosure", max_instances=1, coalesce=True)  # filing emiten (intraday, cepat)
    # INFORMASI TERCEPAT: harga bergerak SEBELUM berita — anomali harga/volume dari quotes
    # yang sudah live (0 API baru), disuntik sbg 'berita'. Harga beku saat tutup → skip total.
    sched.add_job(_hemat("anomaly", anomaly.scan, None), "interval",
                  minutes=config.PRICE_REFRESH_MIN,
                  id="anomaly", max_instances=1, coalesce=True)
    sched.add_job(orchestrator.run_cycle, "interval", minutes=config.AGENT_CYCLE_MIN,
                  id="agents", max_instances=1, coalesce=True)
    sched.add_job(orchestrator.resolve_due, "interval", minutes=60,
                  id="resolve", max_instances=1, coalesce=True)
    sched.add_job(backtest.run_horizons, "interval", minutes=config.BACKTEST_REFRESH_MIN,
                  id="backtest", max_instances=1, coalesce=True)
    sched.add_job(model.train, "interval", minutes=config.BACKTEST_REFRESH_MIN,
                  id="model", max_instances=1, coalesce=True)
    # Snapshot ekuitas BERKALA (bukan cuma di akhir run_cycle, yg dilewati saat pasar tutup).
    # Saat tutup mark-to-market beku → cukup 1×/jam (kurva tetap hidup tanpa titik datar spam).
    sched.add_job(_hemat("equity", paper.snapshot, 60), "interval",
                  minutes=config.PRICE_REFRESH_MIN,
                  id="equity", max_instances=1, coalesce=True)
    # EVALUASI OTONOM harian: laporan tersimpan otomatis 16:00 WIB (09:00 UTC, pasca-tutup),
    # lalu 16:15 agen EVALUATOR menyuling rekam jejak → aturan dinamis utk prompt besok.
    sched.add_job(report.save_to_file, "cron", hour=9, minute=0,
                  id="report", max_instances=1, coalesce=True)
    sched.add_job(knowledge.auto_tune, "cron", hour=9, minute=15,
                  id="autotune", max_instances=1, coalesce=True)
    sched.start()
    _scheduler = sched
    repo.log("engine", "fetch", "scheduler 24 jam aktif")

    # Price-guard: refresh harga cepat (~PRICE_REFRESH_SEC) seluruh universe ber-quote,
    # di thread daemon TERPISAH dari APScheduler → sweep lambat / throttle Yahoo tidak
    # pernah menahan job LLM, dan crash apa pun di dalamnya tak menjatuhkan engine.
    from app.data import price_guard
    price_guard.start_background()

    threading.Thread(target=_bootstrap, daemon=True).start()
    return sched
