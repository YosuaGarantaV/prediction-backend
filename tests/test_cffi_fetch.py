"""Cek isolasi subprocess curl_cffi (app.data.cffi_fetch): crash/timeout anak → induk selamat
(return None), bukan ikut mati. Lihat memory saham-idx (crash native exit 1073807364)."""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
import subprocess
from types import SimpleNamespace

from app.data import cffi_fetch


def test_success_returns_body():
    cffi_fetch.subprocess.run = lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"data":[1,2]}')
    assert cffi_fetch.safe_get("http://x", {}) == '{"data":[1,2]}'


def test_native_crash_returns_none():
    # segfault anak = returncode negatif (mis. -11) atau kode besar Windows → induk TIDAK mati
    cffi_fetch.subprocess.run = lambda *a, **k: SimpleNamespace(returncode=-11, stdout="")
    assert cffi_fetch.safe_get("http://x", {}) is None
    cffi_fetch.subprocess.run = lambda *a, **k: SimpleNamespace(returncode=1073807364, stdout="")
    assert cffi_fetch.safe_get("http://x", {}) is None


def test_non200_returns_none():
    cffi_fetch.subprocess.run = lambda *a, **k: SimpleNamespace(returncode=2, stdout="")
    assert cffi_fetch.safe_get("http://x", {}) is None


def test_timeout_returns_none():
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)
    cffi_fetch.subprocess.run = boom
    assert cffi_fetch.safe_get("http://x", {}) is None


def test_blocked_profile_falls_through_to_next():
    """Cloudflare mem-blok per FINGERPRINT: 13 Agt 2026 semua profil Chrome kena 403 dari VM
    tapi safari18_0/firefox133 tembus. Satu profil gagal TIDAK boleh mematikan sumbernya."""
    cffi_fetch._GOOD = None
    dicoba = []

    def fake_run(cmd, input=None, **k):
        import json
        imp = json.loads(input)["impersonate"]
        dicoba.append(imp)
        ok = imp == "safari18_0"                       # cuma Safari yang lolos
        return SimpleNamespace(returncode=0 if ok else 2, stdout="PAYLOAD" if ok else "")

    cffi_fetch.subprocess.run = fake_run
    assert cffi_fetch.safe_get("http://x", {}) == "PAYLOAD", dicoba
    assert dicoba == ["chrome", "safari18_0"], dicoba   # berhenti begitu tembus
    dicoba.clear()
    assert cffi_fetch.safe_get("http://x", {}) == "PAYLOAD"
    assert dicoba == ["safari18_0"], dicoba             # profil yang tembus diingat
    cffi_fetch._GOOD = None


def test_all_profiles_blocked_returns_none():
    cffi_fetch._GOOD = None
    cffi_fetch.subprocess.run = lambda *a, **k: SimpleNamespace(returncode=2, stdout="")
    assert cffi_fetch.safe_get("http://x", {}) is None


if __name__ == "__main__":
    for fn in [test_success_returns_body, test_native_crash_returns_none,
               test_non200_returns_none, test_timeout_returns_none,
               test_blocked_profile_falls_through_to_next,
               test_all_profiles_blocked_returns_none]:
        fn()
        print("OK", fn.__name__)
    print("OK test_cffi_fetch: crash/timeout/non-200 anak → induk selamat (None).")
