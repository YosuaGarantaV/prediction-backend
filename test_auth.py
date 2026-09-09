"""Uji auth login: guard sesi (401/redirect), login benar/salah, lockout brute-force.
Jalankan: .venv/Scripts/python.exe -m pytest test_auth.py -q"""
from starlette.testclient import TestClient

import config

# Kredensial uji deterministik — di-patch SEBELUM request (auth.py membacanya per-request).
config.AUTH_USER = "yosua"
config.AUTH_PASSWORD = "pw-uji-123"

from app import auth, db  # noqa: E402
from app.server import app  # noqa: E402

db.init_db()   # /api/overview butuh tabel (DB lokal bisa saja belum ada)


def _client() -> TestClient:
    auth._fails.clear()   # tiap test mulai dari counter bersih (IP testclient dipakai bersama)
    return TestClient(app, base_url="https://testserver")  # https: cookie Secure tersimpan


def _login(c: TestClient, password: str):
    return c.post("/login", data={"username": "yosua", "password": password},
                  follow_redirects=False)


def test_unauthenticated_api_gets_401():
    c = _client()
    for path in ("/api/overview", "/api/watchlist", "/api/usage"):
        r = c.get(path)
        assert r.status_code == 401, (path, r.status_code)


def test_unauthenticated_html_redirects_to_login():
    c = _client()
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_page_renders():
    c = _client()
    r = c.get("/login")
    assert r.status_code == 200 and "password" in r.text.lower()


def test_wrong_password_rejected_and_counted():
    c = _client()
    r = _login(c, "salah-total")
    assert r.status_code == 401
    assert c.get("/api/overview").status_code == 401          # tetap belum masuk
    assert len(auth._fails.get("testclient", [])) == 1        # kegagalan tercatat utk lockout


def test_correct_password_opens_protected_routes():
    c = _client()
    r = _login(c, "pw-uji-123")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/api/overview").status_code == 200
    assert c.get("/api/watchlist").status_code == 200


def test_lockout_after_max_fails_returns_429():
    c = _client()
    for _ in range(auth.MAX_FAILS):
        assert _login(c, "salah").status_code in (401, 429)
    r = _login(c, "pw-uji-123")            # password BENAR pun ditolak saat terkunci
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    assert c.get("/api/overview").status_code == 401
    auth._fails.clear()                    # jangan tinggalkan IP testclient terkunci


def test_logout_clears_session():
    c = _client()
    _login(c, "pw-uji-123")
    assert c.get("/api/overview").status_code == 200
    r = c.get("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/api/overview").status_code == 401


def test_login_and_static_paths_exempt_from_guard():
    c = _client()
    assert c.get("/login").status_code == 200
    assert c.get("/favicon.ico").status_code in (200, 404)     # exempt, bukan redirect/401
    r = c.get("/_next/tidak-ada.js", follow_redirects=False)
    assert r.status_code == 404                                # exempt → 404 statis, bukan 303
