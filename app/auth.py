"""Login web + anti brute-force. Rute /login /logout di sini; guard sesi di server.py.

Kredensial dibaca dari env (config.AUTH_USER / AUTH_PASSWORD), dibandingkan constant-time
(hmac.compare_digest) supaya tak bisa dibedakan lewat timing. Kegagalan login dihitung
per-IP: >= MAX_FAILS dalam WINDOW_S -> 429 + Retry-After (memperlambat brute-force)."""
from __future__ import annotations

import hmac
import threading
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import config

router = APIRouter()

# --- Anti brute-force per-IP ---
# ponytail: counter di memori proses — hilang saat service restart; cukup untuk
# single-instance (systemd 1 proses). Persist ke DB baru perlu kalau multi-worker.
MAX_FAILS = 5
WINDOW_S = 15 * 60
_lock = threading.Lock()
_fails: dict[str, list[float]] = {}          # ip -> daftar epoch kegagalan dalam window


def client_ip(request: Request) -> str:
    """IP klien ASLI di belakang Caddy: entri TERAKHIR X-Forwarded-For = yang ditulis
    proxy kita sendiri (entri pertama bisa dispoof klien). Tanpa XFF -> koneksi langsung."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "?"


def lockout_s(ip: str, *, add_fail: bool = False) -> int:
    """Sisa detik lockout IP (0 = bebas). add_fail=True mencatat 1 kegagalan dulu.
    Thread-safe: uvicorn melayani request paralel."""
    now = time.time()
    with _lock:
        ts = _fails.setdefault(ip, [])
        ts[:] = [t for t in ts if now - t < WINDOW_S]   # buang yang di luar window
        if add_fail:
            ts.append(now)
        if len(ts) >= MAX_FAILS:
            return max(1, int(ts[0] + WINDOW_S - now) + 1)
        return 0


def clear_fails(ip: str) -> None:
    with _lock:
        _fails.pop(ip, None)


# Halaman login self-contained (CSS inline, token warna = tema gelap dashboard).
# Slot <!--ERR--> diganti kotak error saat gagal — hindari str.format (CSS penuh kurung).
_LOGIN_HTML = """<!doctype html>
<html lang="id"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Masuk - Saham IDX Prediction Engine</title>
<style>
:root{--bg:#0e1213;--panel:#161b1d;--elevated:#20272a;--line:#242c2f;--edge:#3b4649;
--txt:#eae7de;--muted:#97a6a4;--accent:#d9a441;--down:#e4685a}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);min-height:100vh;display:flex;align-items:center;
justify-content:center;font:15px/1.5 ui-sans-serif,system-ui,"Segoe UI",sans-serif}
form.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
padding:38px 34px 32px;width:min(400px,92vw);box-shadow:0 18px 50px rgba(0,0,0,.45)}
.mark{width:44px;height:44px;border-radius:10px;background:var(--elevated);
border:1px solid var(--edge);color:var(--accent);font-weight:700;font-size:15px;
display:flex;align-items:center;justify-content:center;letter-spacing:.5px;margin-bottom:18px}
h1{font-size:18px;font-weight:600;letter-spacing:.2px}
p.sub{color:var(--muted);font-size:13px;margin:4px 0 22px}
label{display:block;font-size:12px;color:var(--muted);letter-spacing:.4px;
text-transform:uppercase;margin:14px 0 6px}
input{width:100%;background:var(--bg);border:1px solid var(--edge);border-radius:8px;
color:var(--txt);padding:10px 12px;font-size:15px;outline:none}
input:focus{border-color:var(--accent)}
button{width:100%;margin-top:22px;background:var(--accent);color:#14100a;border:0;
border-radius:8px;padding:11px;font-size:15px;font-weight:600;cursor:pointer}
button:hover{filter:brightness(1.08)}
.err{background:rgba(228,104,90,.12);border:1px solid var(--down);color:var(--down);
border-radius:8px;padding:9px 12px;font-size:13px;margin-bottom:6px}
button.guest{margin-top:10px;background:transparent;color:var(--muted);
border:1px solid var(--edge);font-weight:500}
button.guest:hover{filter:none;border-color:var(--accent);color:var(--txt)}
.divider{display:flex;align-items:center;gap:10px;margin:18px 0 2px;color:var(--muted);
font-size:11px;letter-spacing:.5px;text-transform:uppercase}
.divider::before,.divider::after{content:"";flex:1;height:1px;background:var(--line)}
</style></head>
<body>
<form class="card" method="post" action="/login" autocomplete="on">
  <div class="mark">IDX</div>
  <h1>Saham IDX Prediction Engine</h1>
  <p class="sub">Masuk untuk membuka dashboard.</p>
  <!--ERR-->
  <label for="u">Username</label>
  <input id="u" name="username" autocomplete="username" autofocus required>
  <label for="p">Password</label>
  <input id="p" name="password" type="password" autocomplete="current-password"
         required>
  <button type="submit">Masuk</button>
  <!--GUEST-->
</form>
</body></html>"""

# Tombol tamu: submit KEDUA di form yang sama, tapi formaction menembak /guest-login &
# formnovalidate melewati required username/password (tamu tak perlu kredensial). Hanya
# dirender bila mode tamu AKTIF (GUEST_PASSWORD terisi = fail-closed switch on/off).
_GUEST_BTN = (
    '<div class="divider">atau</div>'
    '<button type="submit" formaction="/guest-login" formmethod="post" formnovalidate '
    'class="guest">Masuk sebagai Tamu (akses terbatas)</button>')


def _page(error: str | None = None, status: int = 200,
          retry_after: int | None = None) -> HTMLResponse:
    html = _LOGIN_HTML.replace(
        "<!--ERR-->", f'<div class="err">{error}</div>' if error else "")
    html = html.replace("<!--GUEST-->", _GUEST_BTN if config.GUEST_PASSWORD else "")
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return HTMLResponse(html, status_code=status, headers=headers)


@router.get("/login", response_class=HTMLResponse)
def login_form():
    return _page()


@router.post("/login")
async def login_submit(request: Request):
    ip = client_ip(request)
    wait = lockout_s(ip)
    if wait:                                  # terkunci: tolak SEBELUM verifikasi apa pun
        return _page(f"Terlalu banyak percobaan gagal. Coba lagi dalam {wait} detik.",
                     status=429, retry_after=wait)
    # Form login selalu application/x-www-form-urlencoded → parse stdlib, tanpa
    # dependency python-multipart (request.form() Starlette baru mewajibkannya).
    body = (await request.body()).decode("utf-8", "replace")
    form = parse_qs(body, keep_blank_values=True)
    user = (form.get("username") or [""])[0]
    pw = (form.get("password") or [""])[0]
    # compare_digest = constant-time; KEEMPAT compare DIHITUNG dulu tanpa short-circuit
    # (jangan bocorkan lewat timing user/role mana yang cocok). Password kosong = role itu
    # DIMATIKAN (fail-closed): admin & guest sama-sama wajib password terisi di env.
    ok_admin_user = hmac.compare_digest(user.encode(), config.AUTH_USER.encode())
    ok_admin_pw = hmac.compare_digest(pw.encode(), config.AUTH_PASSWORD.encode())
    ok_guest_user = hmac.compare_digest(user.encode(), config.GUEST_USER.encode())
    ok_guest_pw = hmac.compare_digest(pw.encode(), config.GUEST_PASSWORD.encode())
    is_admin = ok_admin_user and ok_admin_pw and bool(config.AUTH_PASSWORD)
    is_guest = ok_guest_user and ok_guest_pw and bool(config.GUEST_PASSWORD)
    if is_admin or is_guest:
        role = "admin" if is_admin else "guest"
        clear_fails(ip)
        request.session.clear()
        request.session["auth"] = True
        request.session["role"] = role
        request.session["user"] = config.AUTH_USER if is_admin else config.GUEST_USER
        return RedirectResponse("/", status_code=303)
    wait = lockout_s(ip, add_fail=True)
    if wait:
        return _page(f"Terlalu banyak percobaan gagal. Coba lagi dalam {wait} detik.",
                     status=429, retry_after=wait)
    return _page("Username atau password salah.", status=401)


@router.post("/guest-login")
async def guest_login(request: Request):
    """Akses TAMU tanpa kredensial (mode gratis) — hanya bila mode tamu aktif (GUEST_PASSWORD
    terisi). Bot-guard: rate-limit per-IP (jalur yang sama dengan penjaga request tamu) supaya
    tak bisa dibanjiri membuat sesi. Kuota SCAN & filter data emiten tetap ditegakkan server-side
    (app/guest.py) — tombol ini cuma pintu masuk, bukan kelonggaran otorisasi."""
    if not config.GUEST_PASSWORD:                     # mode tamu dimatikan (fail-closed)
        return _page("Mode tamu tidak tersedia.", status=403)
    from app import guest                             # lazy: hindari import melingkar (guest→auth)
    ip = client_ip(request)
    wait = guest._rate_limited(ip)                    # anti-flood pembuatan sesi tamu
    if wait:
        return _page(f"Terlalu banyak permintaan. Coba lagi dalam {wait} detik.",
                     status=429, retry_after=wait)
    request.session.clear()
    request.session["auth"] = True
    request.session["role"] = "guest"
    request.session["user"] = config.GUEST_USER
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
