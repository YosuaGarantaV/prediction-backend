"""Pagu token harian. Hari dihitung menurut WIB, bukan UTC atau zona server.

Tanpa pagu, satu hari buruk (provider lambat, loop tool berulang, horizon panjang) bisa
menghabiskan kuota sebulan tanpa ada yang menghentikannya. Pagu ini berhenti MEMANGGIL,
bukan memotong jawaban di tengah: panggilan yang sudah jalan dibiarkan selesai, panggilan
berikutnya ditolak sampai tengah malam WIB.

Sumber angka = `agent_logs` (phase='usage'), tabel yang sama yang sudah dipakai panel token,
jadi hitungannya selamat dari restart tanpa perlu berkas state sendiri.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import config
from app import repo

WIB = timezone(timedelta(hours=7))

_guard = threading.Lock()
_hari = ""          # tanggal WIB yang sedang dihitung
_terpakai = 0       # total token hari itu
_dilaporkan = False  # sudah menulis log "pagu habis" untuk hari ini


def hari_wib() -> str:
    return datetime.now(WIB).strftime("%Y-%m-%d")


def _batas_utc(hari: str) -> str:
    """Awal hari WIB itu, dalam ISO UTC — batas bawah query agent_logs."""
    awal = datetime.fromisoformat(hari).replace(tzinfo=WIB)
    return awal.astimezone(timezone.utc).isoformat()


def _muat(hari: str) -> int:
    """Jumlah token yang sudah tercatat hari ini. 0 kalau DB tak bisa dibaca."""
    try:
        rows = repo.get_conn().execute(
            "SELECT payload_json FROM agent_logs WHERE agent='llm' AND phase='usage' AND ts>=?",
            (_batas_utc(hari),)).fetchall()
    except Exception:  # noqa: BLE001 — pagu tak boleh menjatuhkan engine
        return 0
    total = 0
    for (pj,) in rows:
        try:
            total += int((json.loads(pj or "{}") or {}).get("total_tokens") or 0)
        except Exception:  # noqa: BLE001
            continue
    return total


def _sinkron() -> tuple[int, int]:
    """(terpakai, pagu) untuk hari WIB sekarang; reseed otomatis saat tanggal berganti."""
    global _hari, _terpakai, _dilaporkan
    h = hari_wib()
    if h != _hari:
        _hari, _terpakai, _dilaporkan = h, _muat(h), False
    return _terpakai, int(config.DAILY_TOKEN_BUDGET or 0)


def sisa() -> int:
    """Token tersisa hari ini. Pagu 0 atau negatif = tanpa batas."""
    with _guard:
        pakai, pagu = _sinkron()
    return 10 ** 12 if pagu <= 0 else max(0, pagu - pakai)


def habis() -> bool:
    """True kalau panggilan LLM berikutnya harus ditolak."""
    global _dilaporkan
    with _guard:
        pakai, pagu = _sinkron()
        if pagu <= 0 or pakai < pagu:
            return False
        lapor = not _dilaporkan
        _dilaporkan = True
    if lapor:
        repo.log("llm", "budget",
                 f"pagu token harian habis: {pakai:,} dari {pagu:,} (reset 00:00 WIB)",
                 level="warn")
    return True


def catat(total_tokens: int | None) -> None:
    """Tambahkan pemakaian satu panggilan. Dipanggil dari llm._log_usage."""
    if not total_tokens:
        return
    global _terpakai
    with _guard:
        _sinkron()
        _terpakai += int(total_tokens)


def status() -> dict:
    """Untuk panel /api/health dan laporan."""
    with _guard:
        pakai, pagu = _sinkron()
    return {"hari_wib": _hari, "terpakai": pakai, "pagu": pagu,
            "sisa": (max(0, pagu - pakai) if pagu > 0 else None),
            "persen": (round(pakai / pagu * 100, 1) if pagu > 0 else None)}


def _reset_untuk_test() -> None:
    global _hari, _terpakai, _dilaporkan
    with _guard:
        _hari, _terpakai, _dilaporkan = "", 0, False
