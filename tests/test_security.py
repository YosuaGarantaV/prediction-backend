"""Penjaga keamanan server: CSRF/cost-burn guard + bind localhost (python test_security.py).
Endpoint POST memicu kerja LLM mahal — situs jahat di browser TAK boleh menembaknya."""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from starlette.testclient import TestClient

import config
from app import auth
from app.server import app

c = TestClient(app, base_url="https://testserver")  # https: cookie Secure tersimpan
# Guard auth kini melindungi semua rute — login dulu supaya uji CSRF menguji CSRF, bukan auth.
auth._fails.clear()
c.post("/login", data={"username": config.AUTH_USER, "password": config.AUTH_PASSWORD})


def test_get_always_allowed():
    assert c.get("/api/overview").status_code == 200
    print("GET lolos ok")


def test_same_origin_post_allowed():
    r = c.post("/api/run-cycle", headers={"origin": "http://testserver", "host": "testserver"})
    assert r.status_code == 200, r.status_code   # dashboard sendiri → lolos
    print("POST same-origin lolos ok")


def test_cross_origin_post_blocked():
    r = c.post("/api/run-cycle", headers={"origin": "http://evil.com", "host": "testserver"})
    assert r.status_code == 403, r.status_code   # situs jahat → ditolak (cegah bakar kuota)
    print("POST cross-origin diblokir ok")


def test_no_origin_post_allowed():
    # curl / alat non-browser tak kirim Origin → tetap boleh (bukan ancaman CSRF)
    r = c.post("/api/run-cycle", headers={"host": "testserver"})
    assert r.status_code == 200, r.status_code
    print("POST tanpa-origin (curl) lolos ok")


def test_login_dikecualikan_csrf():
    """/login & /guest-login lolos meski Origin lintas/null (pintu auth, bukan aksi mahal) —
    ini yang dulu bikin tombol tamu error 'lintas-origin ditolak'."""
    gc = TestClient(app, base_url="https://testserver")
    r = gc.post("/guest-login", headers={"origin": "null"}, follow_redirects=False)
    assert r.status_code == 303, r.status_code      # tak lagi 403 lintas-origin
    r2 = c.post("/login", headers={"origin": "null"},
                data={"username": "x", "password": "y"}, follow_redirects=False)
    assert r2.status_code in (401, 429), r2.status_code  # sampai handler (bukan 403 CSRF)
    # Tapi endpoint MAHAL tetap terlindungi lintas-origin:
    r3 = c.post("/api/run-cycle", headers={"origin": "http://evil.com", "host": "testserver"})
    assert r3.status_code == 403, r3.status_code
    print("login exempt CSRF, endpoint mahal tetap terlindungi ok")


def test_bind_localhost_by_default():
    # Default HOST harus localhost — cegah paparan LAN tak sengaja di WiFi publik
    assert config.HOST == "127.0.0.1", config.HOST
    print("bind default localhost ok")


if __name__ == "__main__":
    test_get_always_allowed()
    test_same_origin_post_allowed()
    test_cross_origin_post_blocked()
    test_no_origin_post_allowed()
    test_bind_localhost_by_default()
    print("ALL OK")
