"""Cek gate pencatatan _commit (python test_commit_gate.py), monkeypatch, tanpa DB.

Kontrak: FLAT tidak dicatat sebagai taruhan pada horizon mana pun, sementara trade tetap
dieksekusi dan transkrip tetap terarsip; UP/DOWN tetap dicatat; resolved_predictions
mengecualikan forecast tampilan 1-hari."""
from app.agents import orchestrator as orch

_ORIG_RESOLVED = orch.repo.resolved_predictions  # dipulihkan di test yg butuh fungsi asli


def _setup():
    saved, acted, convs = [], [], []
    orch.repo.save_prediction = lambda d: saved.append(d) or len(saved)
    orch.repo.open_prediction_id = lambda *a: None
    orch.repo.open_predictions_for_ticker = lambda t: []
    orch.repo.supersede_prediction = lambda pid: None   # default no-op (dites khusus di bawah)
    orch.repo.save_conversation = lambda *a, **k: convs.append(a)
    orch.repo.log = lambda *a, **k: None
    orch.repo.add_lesson = lambda *a, **k: None
    # Gate bukti + kalibrasi + narasi menyentuh data ini — stub supaya test tetap tanpa DB.
    orch.repo.get_quote = lambda t: None
    orch.repo.news_for_ticker = lambda *a, **k: []
    orch.repo.resolved_predictions = lambda *a, **k: []
    orch.model.predict_proba = lambda f: None
    orch.paper.act_on_decision = lambda d, pid: acted.append((d["ticker"], pid))
    orch._commit_1d = lambda t: None
    from app.data import flow, idxflow, premarket
    premarket.global_brief = lambda: {"score": 0.0, "lean": "netral"}
    idxflow.foreign_brief = lambda t: "(stub)"
    flow.local_reversal = lambda: (False, "")   # gate rezim lokal netral di unit test
    flow.ihsg_uptrend = lambda: (False, 0.0, 0.0)  # trend-gate INERT (last=0) — uji gate lain
    return saved, acted, convs


def _mk_open(id_, direction, horizon, entry=100.0, prob=55.0, ts=None, expires_at=None):
    """Baris prediksi 'open' palsu (spt yg dikembalikan repo.open_predictions_for_ticker).
    ts default = 3 HARI LALU. expires_at default = KEMARIN → taruhan sudah jatuh tempo, jadi
    supersede menilainya win/loss. Pass expires_at=<tanggal depan> untuk menguji taruhan yang
    BELUM jalan penuh (harus jadi 'superseded' netral, bukan kalah)."""
    from datetime import datetime, timezone, timedelta
    from app import market_calendar as cal
    if ts is None:
        ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    if expires_at is None:
        # SESI BURSA terakhir yang sudah tutup - BUKAN date.today()-1. Kalau dipaku ke tanggal
        # kalender, test ini lolos/gagal tergantung HARI test dijalankan: Senin -> kemarin =
        # Minggu -> tak ada sesi tutup sesudahnya -> prediction_is_due False -> supersede menilai
        # 'superseded' (netral) alih-alih win/loss, dan 4 test merah tanpa ada kode yang berubah
        # (terjadi 2026-08-24, Senin, sebelum closing). Kelas bug yang sama sudah diperbaiki utk
        # test_audit_populasi 2026-08-10; sumber tanggal harus kalender bursa, bukan asumsi.
        expires_at = cal.last_closed_session(datetime.now(timezone.utc)).isoformat()
    return {"id": id_, "ticker": "TEST", "direction": direction, "horizon_days": horizon,
            "entry_price": entry, "probability": prob, "reasoning": "old", "ts": ts,
            "expires_at": expires_at}


def _mk(direction, horizon=3, action="HOLD"):
    return {"ticker": "TEST", "direction": direction, "probability": 60.0,
            "horizon_days": horizon, "entry_price": 100.0, "target_price": 101.0,
            "expected_pct": 1.0, "reasoning": "r", "action": action, "size_pct": 0,
            "key_factors": [], "critique": "c", "term": "pendek", "agree": True,
            "_analyst_view": "[ANALYST #1] tesis"}


def test_flat_multiday_not_recorded():
    saved, acted, convs = _setup()
    out = orch._commit(_mk("FLAT"))
    assert out == 0 and not saved, saved            # tak dicatat sbg taruhan
    assert acted == [("TEST", None)], acted          # trade tetap dieksekusi
    assert convs, "transkrip debat harus tetap terarsip"
    print("FLAT multi-hari: tak dicatat, trade jalan, transkrip terarsip ok")


def test_directional_still_recorded():
    saved, acted, _ = _setup()
    pid = orch._commit(_mk("DOWN"))
    assert saved and pid == 1, (saved, pid)
    assert acted == [("TEST", 1)]
    print("DOWN tetap dicatat + dieksekusi ok")


def test_flat_1d_tidak_lagi_dicatat():
    """Companion 1-hari FLAT tidak lagi dicatat. Diuji terhadap dasar pasar yang dicocokkan
    per tanggal (tools/gate_trial.py): win-rate FLAT ada di bawah dasarnya sendiri, jadi
    mencatatnya cuma menambah baris yang kalah dari tebakan acak. Trade tetap dieksekusi."""
    saved, acted, _ = _setup()
    orch._commit(_mk("FLAT", horizon=1))
    assert not saved, "FLAT tak boleh lagi masuk tabel prediksi"
    assert acted, "trade (mis. exit posisi) TETAP harus dievaluasi"
    print("FLAT 1-hari tak dicatat, trade tetap jalan ok")


def test_count_predictions_menghitung_baris_bukan_panggilan():
    """Angka "prediksi dicatat" harus bisa dihitung ulang dari tabel. Menghitung panggilan
    `_commit` melebihkan karena duplikat dan keputusan yang digerbang ikut terhitung."""
    import sqlite3
    from app import repo
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE predictions(id INTEGER PRIMARY KEY, ticker TEXT)")
    conn.commit()
    repo.get_conn = lambda: conn
    before = repo.count_predictions()
    conn.execute("INSERT INTO predictions(ticker) VALUES ('A')")
    conn.execute("INSERT INTO predictions(ticker) VALUES ('B')")
    conn.commit()
    assert before == 0 and repo.count_predictions() - before == 2
    print("count_predictions menghitung baris ok")


def test_resolved_predictions_excludes_display():
    import sqlite3
    from app import repo
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # ts/resolved_at WAJIB ada: resolved_predictions memverifikasi ada sesi bursa yang tutup
    # di antaranya (market_calendar.has_new_session). Kamis 2026-07-16 10:00 WIB -> Jumat 17
    # 17:00 WIB = satu closing terlewati = observasi sah.
    dibuat, dinilai = "2026-07-16T03:00:00+00:00", "2026-07-17T10:00:00+00:00"
    conn.execute("CREATE TABLE predictions(id INTEGER PRIMARY KEY, status TEXT, direction TEXT, "
                 "probability REAL, actual_pct REAL, outcome TEXT, horizon_days INTEGER, "
                 "reasoning TEXT, ts TEXT, resolved_at TEXT)")
    conn.execute("INSERT INTO predictions(status,direction,probability,outcome,horizon_days,"
                 "reasoning,ts,resolved_at) VALUES ('resolved','UP',60,'win',3,'analisis LLM',?,?)",
                 (dibuat, dinilai))
    conn.execute("INSERT INTO predictions(status,direction,probability,outcome,horizon_days,"
                 "reasoning,ts,resolved_at) VALUES ('resolved','UP',60,'loss',1,"
                 "'prediksi 1-hari (besok) — forecast.',?,?)", (dibuat, dinilai))
    # Taruhan nyata TAPI dinilai akhir pekan (Sabtu) tanpa sesi baru -> harus ikut dibuang.
    conn.execute("INSERT INTO predictions(status,direction,probability,outcome,horizon_days,"
                 "reasoning,ts,resolved_at) VALUES ('resolved','DOWN',60,'loss',3,'analisis LLM',?,?)",
                 ("2026-07-17T11:26:00+00:00", "2026-07-18T00:00:00+00:00"))
    conn.commit()
    repo.get_conn = lambda: conn
    repo.resolved_predictions = _ORIG_RESOLVED   # test lain menstub fungsi ini
    rows = repo.resolved_predictions()
    assert len(rows) == 1 and rows[0]["outcome"] == "win", rows
    print("resolved_predictions mengecualikan forecast display 1-hari ok")


def test_up_naked_flattened():
    """UP teknikal-murni (tanpa model/struktural/berita) → gate bukti mem-FLAT-kan →
    multi-hari tak dicatat (UP telanjang historis 23% benar, n=447)."""
    saved, acted, _ = _setup()
    out = orch._commit(_mk("UP", action="BUY"))
    assert out == 0 and not saved, saved
    assert acted and acted[0][0] == "TEST"   # trade path tetap jalan (HOLD)
    print("UP tanpa bukti: di-FLAT-kan & tak dicatat ok")


def test_up_with_pillar_recorded():
    """UP + faktor struktural (mis. asing_beli) → lolos gate & tetap dicatat."""
    saved, _, _ = _setup()
    d = _mk("UP", action="BUY")
    d["signals"] = [{"name": "asing_beli", "dir": 1}]
    pid = orch._commit(d)
    assert pid == 1 and saved and saved[0]["direction"] == "UP", saved
    assert any("[bukti UP]" in str(f) for f in saved[0]["factors"]["key_factors"])
    print("UP dgn pilar struktural: tetap dicatat ok")


def test_llm_probability_calibrated_and_narrated():
    """Klaim LLM 75% + realisasi historis DOWN buruk → probability turun deterministik;
    reasoning berisi narasi per-pilar (Berita:/Rezim:), bukan cuma teks CTO."""
    saved, _, _ = _setup()
    orch.repo.resolved_predictions = lambda *a, **k: (
        [{"direction": "DOWN", "outcome": "win", "probability": 70, "horizon_days": 3}] * 22
        + [{"direction": "DOWN", "outcome": "loss", "probability": 70, "horizon_days": 3}] * 18)
    d = _mk("DOWN")
    d["probability"] = 75.0
    orch._commit(d)
    assert saved[0]["probability"] < 60, saved[0]["probability"]
    assert "Berita:" in saved[0]["reasoning"], saved[0]["reasoning"]
    print("kalibrasi LLM + narasi kaya ok")


def test_heuristic_not_double_calibrated():
    """Keputusan heuristik (flag _calibrated) TIDAK di-shrink lagi di _commit."""
    saved, _, _ = _setup()
    orch.repo.resolved_predictions = lambda *a, **k: (
        [{"direction": "DOWN", "outcome": "loss", "probability": 70, "horizon_days": 3}] * 40)
    d = _mk("DOWN")
    d["probability"], d["_calibrated"] = 64.0, True
    orch._commit(d)
    assert saved[0]["probability"] == 64.0, saved[0]["probability"]
    print("heuristik tak dikalibrasi dua kali ok")


def test_supersede_closes_conflicting_old_main():
    """Bug 'duplicate prediction' (2026-07-13): ticker sama, keputusan baru horizon/arah
    BEDA dari prediksi open lama → yang lama harus DITUTUP (menang/kalah nyata), bukan
    dibiarkan menumpuk. Companion 1-hari tidak boleh ikut disentuh oleh supersede main."""
    saved, _, _ = _setup()
    resolved = []
    orch.repo.get_quote = lambda t: {"price": 110.0}   # +10% dari entry lama (100)
    orch.repo.resolve_prediction = lambda pid, pct, outcome: resolved.append((pid, pct, outcome))
    old_main = _mk_open(1, "UP", horizon=5)             # beda horizon dgn keputusan baru (3)
    companion = _mk_open(2, "FLAT", horizon=1)          # horizon==1 -> tak boleh disentuh
    orch.repo.open_predictions_for_ticker = lambda t: [old_main, companion]

    orch._commit(_mk("DOWN", horizon=3))                # keputusan baru: DOWN/3h, beda total

    assert resolved == [(1, 10.0, "win")], resolved      # UP lama +10% = win, ditutup jujur
    assert saved and saved[0]["direction"] == "DOWN", saved  # keputusan baru tetap tercatat
    print("supersede: main lama beda arah/horizon ditutup, companion tak disentuh ok")


def test_supersede_leaves_exact_match_alone():
    """Prediksi open lama yg PERSIS SAMA (arah+horizon) dgn keputusan baru dibiarkan —
    jalur dedup existing (open_prediction_id) yang menanganinya, bukan supersede."""
    saved, _, _ = _setup()
    resolved = []
    orch.repo.get_quote = lambda t: {"price": 110.0}
    orch.repo.resolve_prediction = lambda pid, pct, outcome: resolved.append((pid, pct, outcome))
    same = _mk_open(3, "DOWN", horizon=3)                # persis sama dgn keputusan baru
    orch.repo.open_predictions_for_ticker = lambda t: [same]
    orch.repo.open_prediction_id = lambda *a: 3          # dedup existing bilang "sudah ada"

    pid = orch._commit(_mk("DOWN", horizon=3))

    assert resolved == [], resolved                      # TIDAK ditutup oleh supersede
    assert pid == 3 and not saved, (pid, saved)           # ditangani jalur dedup lama
    print("supersede: match persis dibiarkan, jalur dedup lama tetap yang urus ok")


def test_supersede_flat_closes_all_mains():
    """Keputusan baru FLAT multi-hari (tak ada taruhan baru) → SEMUA main lama ticker ini
    ditutup, apa pun arahnya — tak ada yang 'dipertahankan'."""
    saved, acted, _ = _setup()
    resolved = []
    orch.repo.get_quote = lambda t: {"price": 90.0}      # -10% dari entry (100)
    orch.repo.resolve_prediction = lambda pid, pct, outcome: resolved.append((pid, pct, outcome))
    up_old = _mk_open(4, "UP", horizon=5)
    down_old = _mk_open(5, "DOWN", horizon=7)
    orch.repo.open_predictions_for_ticker = lambda t: [up_old, down_old]

    out = orch._commit(_mk("FLAT", horizon=3))

    ids_closed = {r[0] for r in resolved}
    assert ids_closed == {4, 5}, resolved                 # DUA-duanya ditutup
    assert out == 0 and not saved                          # FLAT multi-hari tetap tak dicatat
    print("supersede: FLAT baru menutup SEMUA main lama ok")


def test_supersede_same_day_not_judged():
    """Permintaan user 2026-07-13: status jangan salah sebelum hari berakhir. Prediksi lama
    yang DIBUAT HARI INI lalu digantikan analisis baru → 'superseded' (netral, TAK dinilai
    menang/kalah), BUKAN resolve loss/win. Prediksi hari SEBELUMNYA tetap dinilai jujur."""
    from datetime import datetime, time, timezone
    from app import market_calendar as cal
    saved, _, _ = _setup()
    resolved, superseded = [], []
    orch.repo.get_quote = lambda t: {"price": 90.0}       # -10% (kalau dinilai = loss utk UP)
    orch.repo.resolve_prediction = lambda pid, pct, o: resolved.append((pid, o))
    orch.repo.supersede_prediction = lambda pid: superseded.append(pid)
    today = _mk_open(7, "UP", horizon=5, ts=datetime.now(timezone.utc).isoformat())     # HARI INI
    # Sesi bursa terakhir yang SUDAH tutup, jam 10:00 WIB (intraday) — bukan "now - 2 hari":
    # 2 hari kalender bisa mendarat di akhir pekan, harga belum bergerak, jadi belum layak
    # dinilai. Lihat market_calendar.has_new_session.
    lalu = datetime.combine(cal.last_closed_session(datetime.now(timezone.utc)),
                            time(10, 0), tzinfo=cal.WIB)
    # expires_at default = kemarin → taruhannya sudah jatuh tempo, jadi dinilai jujur.
    yesterday = _mk_open(8, "UP", horizon=5, ts=lalu.isoformat())                       # LALU
    orch.repo.open_predictions_for_ticker = lambda t: [today, yesterday]

    orch._commit(_mk("DOWN", horizon=3))                   # ganti keduanya

    assert superseded == [7], superseded                   # hari ini → superseded (tak dinilai)
    assert resolved == [(8, "loss")], resolved             # sudah jatuh tempo → dinilai jujur
    print("supersede: prediksi hari-ini superseded (bukan salah), yang jatuh tempo dinilai ok")


def test_supersede_multiday_not_judged_before_horizon():
    """Audit 2026-08-13: taruhan 3-5 hari yang digantikan analisis baru dulu DIVONIS pada
    penutupan hari pertama (34 dari 90 prediksi headline, umur rata-rata 0,61 hari, menang
    17,6% vs 39,3% yang jalan penuh). Sekarang: belum jatuh tempo → netral, bukan kalah."""
    from datetime import datetime, timedelta, timezone
    saved, _, _ = _setup()
    resolved, superseded = [], []
    orch.repo.get_quote = lambda t: {"price": 90.0}        # -10% → kalau dinilai = loss utk UP
    orch.repo.resolve_prediction = lambda pid, pct, o: resolved.append((pid, o))
    orch.repo.supersede_prediction = lambda pid: superseded.append(pid)
    now = datetime.now(timezone.utc)
    besok = (now + timedelta(days=4)).date().isoformat()
    belum = _mk_open(11, "UP", horizon=5, ts=(now - timedelta(days=1)).isoformat(),
                     expires_at=besok)                     # baru hari ke-1 dari 5
    tuntas = _mk_open(12, "UP", horizon=5, ts=(now - timedelta(days=14)).isoformat())
    orch.repo.open_predictions_for_ticker = lambda t: [belum, tuntas]

    orch._commit(_mk("DOWN", horizon=3))

    assert superseded == [11], superseded                  # 1 hari dari horizon 5 → netral
    assert resolved == [(12, "loss")], resolved            # 14 hari → jalan penuh, dinilai
    print("supersede: bet multi-hari belum jatuh tempo TIDAK divonis, yang tuntas dinilai ok")


if __name__ == "__main__":
    test_flat_multiday_not_recorded()
    test_directional_still_recorded()
    test_flat_1d_tidak_lagi_dicatat()
    test_count_predictions_menghitung_baris_bukan_panggilan()
    test_resolved_predictions_excludes_display()
    test_up_naked_flattened()
    test_up_with_pillar_recorded()
    test_llm_probability_calibrated_and_narrated()
    test_heuristic_not_double_calibrated()
    test_supersede_closes_conflicting_old_main()
    test_supersede_leaves_exact_match_alone()
    test_supersede_flat_closes_all_mains()
    test_supersede_same_day_not_judged()
    test_supersede_multiday_not_judged_before_horizon()
    print("ALL OK")
