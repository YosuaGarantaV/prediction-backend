"""Uji role GUEST: login, isolasi akses (403 endpoint admin), IDOR per-ticker (403),
penyaringan list ke GUEST_TICKERS, kuota analis harian (429), dan admin tetap full akses.
Jalankan: .venv/Scripts/python.exe -m pytest test_guest.py -q"""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from starlette.testclient import TestClient

import config

# Kredensial uji deterministik (di-patch SEBELUM request; dibaca per-request).
config.AUTH_USER = "yosua"
config.AUTH_PASSWORD = "pw-uji-123"
config.GUEST_USER = "guest"
config.GUEST_PASSWORD = "pw-uji-guest-xyz"
config.GUEST_TICKERS = ["BBCA", "BBRI", "BMRI", "TLKM", "ASII"]
config.GUEST_DAILY_ANALYST = 5

from app import auth, db, guest  # noqa: E402
from app.agents import orchestrator  # noqa: E402
from app.server import app  # noqa: E402

db.init_db()
# Jangan jalankan analis LLM nyata saat uji POST /api/run: no-op (thread daemon di handler).
orchestrator.run_one = lambda *a, **k: None

GSET = set(config.GUEST_TICKERS)


def _client() -> TestClient:
    auth._fails.clear()
    import app.guest as _g
    _g._rl_hits.clear()   # reset rate-limit sliding-window antar test (IP testclient sama)
    return TestClient(app, base_url="https://testserver")  # https: cookie Secure tersimpan


def _login(c: TestClient, user: str, pw: str):
    return c.post("/login", data={"username": user, "password": pw}, follow_redirects=False)


def _guest_client() -> TestClient:
    c = _client()
    r = _login(c, "guest", "pw-uji-guest-xyz")
    assert r.status_code == 303 and r.headers["location"] == "/", r.status_code
    return c


def _admin_client() -> TestClient:
    c = _client()
    r = _login(c, "yosua", "pw-uji-123")
    assert r.status_code == 303, r.status_code
    return c


def _reset_quota():
    conn = db.get_conn()
    conn.execute("DELETE FROM guest_quota WHERE ip='testclient'")
    conn.commit()


def test_guest_login_works():
    c = _guest_client()
    assert c.get("/api/overview").status_code == 200      # endpoint whitelist → boleh


def test_guest_blocked_from_admin_endpoints():
    """Deny-by-default: internal/ops + SEMUA endpoint isi-REKENING tetap tertutup."""
    c = _guest_client()
    for path in ("/api/usage", "/api/logs", "/api/report", "/api/backup",
                 "/api/mirror", "/api/datafiles",
                 # isi rekening — bukan data pasar (permintaan Yosua 2026-07-18)
                 "/api/positions", "/api/trades", "/api/ledger"):
        assert c.get(path).status_code == 403, (path, c.get(path).status_code)


def test_guest_tak_pernah_lihat_saldo_rekening():
    """/api/overview lolos allowlist TAPI kunci uang (saldo/kurva ekuitas/modal) DICOPOT.
    Data pasar & performa model tetap ada supaya dashboard tamu tetap informatif."""
    c = _guest_client()
    d = c.get("/api/overview").json()
    for bocor in ("portfolio", "curve", "start_cash"):
        assert bocor not in d, f"{bocor} bocor ke guest"
    for tetap in ("stats", "market_phase", "macro", "flow"):
        assert tetap in d, f"{tetap} hilang — dashboard tamu jadi kosong"
    # Admin TETAP melihat saldo (tak ada regresi utk pemilik).
    a = _admin_client().get("/api/overview").json()
    assert "portfolio" in a and "curve" in a


def test_guest_blocked_from_admin_posts():
    c = _guest_client()
    assert c.post("/api/run-cycle").status_code == 403
    assert c.post("/api/run-backtest").status_code == 403
    assert c.post("/api/watch/BBCA").status_code == 403    # mutasi state → guest tak boleh
    assert c.post("/api/pin/BBCA").status_code == 403


def test_guest_blocked_from_api_schema_docs():
    c = _guest_client()
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert c.get(path).status_code == 403, (path, c.get(path).status_code)


def test_guest_idor_per_ticker_blocked():
    c = _guest_client()
    # Ticker DI LUAR 5 saham guest → 403, termasuk varian bypass (case/suffix/spasi).
    for tk in ("ANTM", "antm", "ANTM.JK", "antm.jk", "ANtM", " ANTM", "GOTO", "ANTM%20"):
        for base in ("/api/history/", "/api/conversation/", "/api/news-ticker/"):
            r = c.get(base + tk)
            assert r.status_code == 403, (base + tk, r.status_code)
    assert c.post("/api/run/ANTM").status_code == 403


def test_guest_per_ticker_allowed_for_guest_tickers():
    c = _guest_client()
    for tk in ("BBCA", "bbca"):
        assert c.get("/api/history/" + tk).status_code == 200, tk
    # Bentuk .JK utk ticker guest: lolos guard tapi ditolak validator format handler (400,
    # bukan 200/bocor) — .JK bukan format kode emiten yang sah.
    assert c.get("/api/history/BBCA.JK").status_code == 400


def test_guest_list_endpoints_filtered_to_guest_tickers():
    c = _guest_client()
    # /api/predictions SENGAJA dikecualikan: kini showcase lintas-emiten (uji terpisah di
    # test_guest_predictions_showcase_berarah_menang), bukan lagi disaring ke GUEST_TICKERS.
    for path in ("/api/watchlist", "/api/watched",
                 "/api/lessons", "/api/conversations", "/api/focus"):
        data = c.get(path).json()
        assert isinstance(data, list)
        tickers = {str(x.get("ticker", "")).upper() for x in data if isinstance(x, dict)}
        assert tickers <= GSET, (path, tickers - GSET)      # tak ada emiten di luar 5
        assert len(tickers) <= 5, (path, len(tickers))


def test_guest_predictions_showcase_berarah_menang():
    """Guest /api/predictions = SHOWCASE: hanya prediksi BERARAH (UP/DOWN) yang MENANG, LINTAS
    semua emiten (bukan cuma GUEST_TICKERS); FLAT/datar, kalah, & open dibuang; maks 50.
    Admin tetap melihat semuanya (menang/kalah/flat/open + emiten non-guest)."""
    conn = db.get_conn()
    conn.execute("DELETE FROM predictions WHERE reasoning='uji-showcase'")
    # (ticker, direction, status, outcome) — ZZTEST = emiten DI LUAR GUEST_TICKERS
    rows = [("BBCA", "UP", "resolved", "win"),      # berarah menang, emiten guest  → tampil
            ("ZZTEST", "DOWN", "resolved", "win"),  # berarah menang, NON-guest     → tetap tampil
            ("BBCA", "FLAT", "resolved", "win"),    # datar → dibuang walau menang
            ("BBCA", "UP", "resolved", "loss"),     # berarah kalah → dibuang
            ("BBCA", "UP", "open", None)]           # belum jatuh tempo → dibuang
    for tk, d, st, oc in rows:
        conn.execute(
            "INSERT INTO predictions(ts,ticker,direction,probability,horizon_days,status,"
            "outcome,reasoning) VALUES('2026-07-20T09:00:00',?,?,70,3,?,?,'uji-showcase')",
            (tk, d, st, oc))
    conn.commit()
    try:
        g = _guest_client().get("/api/predictions?limit=300").json()
        mine = [x for x in g if x.get("reasoning") == "uji-showcase"]
        seen = {(x["ticker"], x["direction"], x["outcome"]) for x in mine}
        assert ("BBCA", "UP", "win") in seen, seen
        assert ("ZZTEST", "DOWN", "win") in seen, "emiten non-guest tak tampil (showcase lintas emiten)"
        assert all(x["outcome"] == "win" and x["direction"] in ("UP", "DOWN") for x in mine), seen
        assert not any(x["direction"] == "FLAT" for x in mine), "FLAT/datar bocor ke showcase"
        assert len(g) <= 50, len(g)
        # Admin melihat SEMUA 5 baris uji (flat/loss/open + emiten non-guest), tanpa penyaringan.
        a = _admin_client().get("/api/predictions?limit=500").json()
        am = {(x["ticker"], x["direction"], x["status"], x.get("outcome"))
              for x in a if x.get("reasoning") == "uji-showcase"}
        assert len(am) == 5, am
    finally:
        conn.execute("DELETE FROM predictions WHERE reasoning='uji-showcase'")
        conn.commit()


def test_guest_run_quota_hits_429_after_limit():
    _reset_quota()
    c = _guest_client()
    for i in range(config.GUEST_DAILY_ANALYST):
        assert c.post("/api/run/BBCA").status_code == 200, i
    r = c.post("/api/run/BBRI")                              # ke-6 → habis
    assert r.status_code == 429, r.status_code
    assert int(r.headers["retry-after"]) > 0
    # Re-login TIDAK me-reset kuota (persisten di DB, bukan cookie).
    c2 = _guest_client()
    assert c2.post("/api/run/BMRI").status_code == 429
    _reset_quota()


def test_admin_keeps_full_access():
    c = _admin_client()
    assert c.get("/api/usage").status_code == 200
    assert c.get("/api/logs").status_code == 200
    assert c.post("/api/run-cycle").status_code == 200
    # Admin watchlist tidak disaring (boleh > 5 emiten bila DB berisi banyak).
    assert c.get("/api/watchlist").status_code == 200


def test_login_page_tak_bocorkan_username_admin():
    """Halaman /login publik tak boleh menampilkan username admin (setengah kredensial)."""
    c = _client()
    body = c.get("/login").text
    assert config.AUTH_USER not in body, "username admin bocor di HTML login"
    assert 'value="yosua"' not in body


def test_security_headers_ada():
    """Tiap respons bawa header anti-clickjacking/MIME-sniff/HSTS."""
    c = _client()
    h = c.get("/login").headers
    assert h.get("x-frame-options") == "DENY"
    assert h.get("x-content-type-options") == "nosniff"
    assert "max-age" in (h.get("strict-transport-security") or "")
    assert "frame-ancestors 'none'" in (h.get("content-security-policy") or "")


def test_guest_rate_limit_menahan_flood():
    """1 IP guest yang membanjiri > GUEST_RATE_MAX/window → 429 (anti-DoS satu-sumber)."""
    import app.guest as _g
    orig_max, orig_win = config.GUEST_RATE_MAX, config.GUEST_RATE_WINDOW_S
    config.GUEST_RATE_MAX, config.GUEST_RATE_WINDOW_S = 5, 60
    try:
        c = _guest_client()
        _g._rl_hits.clear()
        codes = [c.get("/api/overview").status_code for _ in range(12)]
        assert 429 in codes, codes            # flood akhirnya kena rem
        assert codes.count(200) <= 6, codes   # tak lebih dari ambang yang lolos
    finally:
        config.GUEST_RATE_MAX, config.GUEST_RATE_WINDOW_S = orig_max, orig_win
        _g._rl_hits.clear()


def test_tombol_tamu_muncul_di_login_saat_aktif():
    """Halaman /login menampilkan tombol 'Masuk sebagai Tamu' bila GUEST_PASSWORD terisi."""
    c = _client()
    body = c.get("/login").text
    assert "/guest-login" in body
    assert "Masuk sebagai Tamu" in body


def test_guest_login_beri_sesi_tamu_tanpa_password():
    """POST /guest-login memberi sesi role=guest tanpa kredensial, lalu akses guest berlaku."""
    c = _client()
    r = c.post("/guest-login", follow_redirects=False)
    assert r.status_code == 303, r.status_code           # redirect ke dashboard
    # Sesi tamu berlaku: endpoint guest-allowlist 200, admin-only tetap 403.
    assert c.get("/api/overview").status_code == 200
    assert c.get("/api/logs").status_code == 403
    assert c.post("/api/run-cycle").status_code == 403


def test_guest_login_mati_saat_password_kosong():
    """GUEST_PASSWORD kosong = mode tamu OFF (fail-closed): tombol hilang & endpoint 403."""
    orig = config.GUEST_PASSWORD
    config.GUEST_PASSWORD = ""
    try:
        c = _client()
        assert "/guest-login" not in c.get("/login").text
        assert c.post("/guest-login", follow_redirects=False).status_code == 403
    finally:
        config.GUEST_PASSWORD = orig


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(name, "ok")
    print("ALL OK")
