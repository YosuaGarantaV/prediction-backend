"""Fetch URL ber-Cloudflare via curl_cffi di SUBPROCESS TERISOLASI.

curl_cffi (impersonate Chrome) sesekali CRASH native (segfault/abort, exit 1073807364 di Windows)
yang TAK bisa ditangkap try/except → dulu mematikan SELURUH engine (supervisor restart ~1x/2jam,
tiap restart ulang bootstrap berat). Dengan menjalankan panggilan itu di anak-proses, crash cuma
membunuh anak → induk dapat returncode != 0 dan LANJUT (data lama dipertahankan). Engine utama
tak pernah mati karena Cloudflare-fetch lagi.

ponytail: stdlib subprocess, tanpa dep baru; anak = modul kecil (init paket kosong → impor ringan).
"""
from __future__ import annotations

import json
import subprocess
import sys


# Cloudflare memblokir per FINGERPRINT, bukan cuma per IP — dan blokirnya bergerak. Diukur
# 2026-08-13 dari VM Oracle: SEMUA profil Chrome (chrome/124/131/136/android) balas 403 "Just a
# moment", sementara safari18_0 & firefox133 balas 200 dgn payload penuh 644 KB; dari IP rumah
# chrome masih 200. Satu profil tetap = satu titik gagal yang MATI DIAM-DIAM (arus asing resmi
# basi 16 jam, disclosure 183 warn/minggu). Dicoba berurutan, yang terakhir sukses didahulukan.
_PROFILES = ("chrome", "safari18_0", "firefox133")
_GOOD: str | None = None      # profil terakhir yang tembus (proses ini)


def _fetch_once(url: str, headers: dict | None, timeout: float,
                impersonate: str, proxy: str | None) -> str | None:
    payload = json.dumps({"url": url, "headers": headers or {}, "timeout": timeout,
                          "impersonate": impersonate, "proxy": proxy})
    try:
        p = subprocess.run(
            [sys.executable, "-m", "app.data.cffi_fetch"],
            input=payload, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout + 15,  # ruang > timeout HTTP internal anak
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if p.returncode != 0:      # non-200(2), error(1), atau CRASH native (negatif/besar) → induk selamat
        return None
    return p.stdout


def safe_get(url: str, headers: dict | None = None, timeout: float = 25.0,
             impersonate: str | None = None, proxy: str | None = None) -> str | None:
    """GET via curl_cffi di subprocess terisolasi. Return body text, atau None kalau semua
    profil gagal / non-200 / TIMEOUT / CRASH native anak. Induk TIDAK ikut mati apa pun yang
    terjadi. impersonate=None → sapu `_PROFILES` sampai ada yang tembus; isi eksplisit → pakai
    itu saja. proxy (opsional): URL residential-proxy / scraping-API → egress lewat IP itu
    supaya lolos WAF yg blokir IP datacenter.
    ponytail: plafonnya len(_PROFILES) subprocess saat SEMUA diblok (job latar, sudah tahan
    gagal); kalau itu jadi mahal, simpan hasil sapuan gagal + backoff seperti disclosure."""
    global _GOOD
    if impersonate:
        return _fetch_once(url, headers, timeout, impersonate, proxy)
    order = ([_GOOD] + [p for p in _PROFILES if p != _GOOD]) if _GOOD else list(_PROFILES)
    for imp in order:
        body = _fetch_once(url, headers, timeout, imp, proxy)
        if body is not None:
            _GOOD = imp
            return body
    return None


def _main() -> int:
    try:
        req = json.loads(sys.stdin.read())
    except Exception:  # noqa: BLE001
        return 1
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # body bisa non-ASCII (judul emiten Indonesia)
    except Exception:  # noqa: BLE001
        pass
    try:
        from curl_cffi import requests as creq
        kw = {"headers": req.get("headers") or {},
              "impersonate": req.get("impersonate", "chrome"),
              "timeout": req.get("timeout", 25)}
        if req.get("proxy"):          # egress lewat proxy → IP residensial/scraping-API
            kw["proxies"] = {"http": req["proxy"], "https": req["proxy"]}
        r = creq.get(req["url"], **kw)
        if r.status_code != 200:
            return 2
        sys.stdout.write(r.text)
        return 0
    except Exception:  # noqa: BLE001
        return 1


if __name__ == "__main__":
    sys.exit(_main())
