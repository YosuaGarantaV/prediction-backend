"""Isolasi test: banyak test menambal global modul secara PERMANEN tanpa teardown
(mis. `repo.get_conn = lambda`, `config.X = ...`, `premarket.global_brief = lambda`),
sehingga urutan-jalan mencemari test lain (30 gagal bersama, tapi tiap file LOLOS sendiri).

Fixture autouse ini memotret __dict__ SETIAP modul `app.*` (plus config) yang sudah terimpor
SEBELUM tiap test, lalu mengembalikannya SESUDAHNYA — jadi reassignment global apa pun ter-revert
tanpa daftar manual yang harus dirawat. Koneksi thread-local juga dibuang agar test berikut
membuka koneksi baru ke DB_PATH saat itu.
"""
import sys

import pytest

from app import db


def _target_modules():
    mods = [m for name, m in sys.modules.items()
            if m is not None and (name == "app" or name.startswith("app."))]
    if "config" in sys.modules:
        mods.append(sys.modules["config"])
    return mods


@pytest.fixture(autouse=True)
def _isolate_module_globals():
    saved = [(m, dict(m.__dict__)) for m in _target_modules()]
    yield
    for m, snap in saved:
        m.__dict__.clear()
        m.__dict__.update(snap)
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        db._local = type(db._local)()  # thread-local kosong → koneksi baru saat dipakai
