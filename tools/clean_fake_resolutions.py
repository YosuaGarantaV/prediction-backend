"""Bersihkan label PALSU: prediksi yang di-'resolve' padahal BELUM ada sesi bursa yang tutup
sejak ia dibuat (akhir pekan / libur / dibuat sesudah closing).

Harga acuannya masih angka yang sama dengan entry -> actual_pct 0.0% -> FLAT otomatis 'benar',
UP/DOWN otomatis 'meleset'. Lihat market_calendar.has_new_session (penjaga barunya).

Dua nasib, sesuai kenyataan taruhannya:
  - `expires_at` masih di depan  -> BALIK ke 'open' (taruhan belum jatuh tempo, biarkan jalan)
  - sudah lewat jatuh tempo      -> 'superseded' (netral: tak dihitung menang/kalah — kita
                                    memang tak pernah punya observasi sah, bukan menang/kalah)
Lessons yang lahir dari label palsu itu DIHAPUS (kalau tidak, terus di-inject ke prompt).

DUA LAPIS, jangan berhenti di lapis pertama:
  1. lessons per-prediksi (`kind` win/loss/loss-highconf) yang menempel ke label palsu;
  2. `kind='auto-rule'` — aturan harian hasil sulingan `knowledge.auto_tune()` ATAS statistik
     yang tercemar. Ini yang lebih berbahaya: bentuknya perintah ("Prioritaskan FLAT...",
     "Hindari UP, arah UP hanya 22% akurat") dan batch terbaru diinject tiap siklus lewat
     `repo.active_auto_rules()`. Angka 22%/62% itu lahir dari FLAT-menang-gratis dan
     UP/DOWN-kalah-gratis. Membersihkan lapis 1 saja tak menghapus biasnya.
Seluruh batch auto-rule yang ada saat pembersihan dihapus; `auto_tune()` berikutnya menyuling
ulang dari data bersih (advisory-only, jadi kosong sementara tidak merusak apa pun).

Dry-run secara default. Menulis hanya dengan --apply, dan selalu backup DB lebih dulu.
    python tools/clean_fake_resolutions.py            # lihat rencana
    python tools/clean_fake_resolutions.py --apply    # kerjakan
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from app import market_calendar as cal


def find_fake(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, ts, resolved_at, expires_at, ticker, direction, actual_pct, outcome "
        "FROM predictions WHERE status='resolved' AND resolved_at IS NOT NULL"
    ).fetchall()
    out = []
    for r in rows:
        try:
            made, done = datetime.fromisoformat(r["ts"]), datetime.fromisoformat(r["resolved_at"])
        except (TypeError, ValueError):
            continue                       # ts rusak -> jangan sentuh, biar terlihat di audit
        if not cal.has_new_session(made, done):
            out.append(dict(r))
    return out


def plan(fake: list[dict], today) -> tuple[list[int], list[int]]:
    """Pisah: yang masih boleh jalan (reopen) vs yang sudah lewat jatuh tempo (superseded)."""
    reopen, dead = [], []
    for r in fake:
        exp = r.get("expires_at")
        try:
            still_running = bool(exp) and datetime.fromisoformat(exp).date() > today
        except ValueError:
            still_running = False
        (reopen if still_running else dead).append(r["id"])
    return reopen, dead


def main(apply: bool, db: str | None = None) -> int:
    db = db or str(config.DB_PATH)
    conn = sqlite3.connect(db)
    fake = find_fake(conn)
    today = cal.last_closed_session(datetime.now(timezone.utc))
    reopen, dead = plan(fake, today)
    ids = reopen + dead
    n_lessons = 0
    if ids:
        q = ",".join("?" * len(ids))
        n_lessons = conn.execute(
            f"SELECT COUNT(*) FROM lessons WHERE prediction_id IN ({q})", ids).fetchone()[0]

    print(f"DB           : {db}")
    print(f"sesi tutup   : {today}")
    print(f"label palsu  : {len(fake)} prediksi (dinilai tanpa sesi bursa baru)")
    print(f"  -> open    : {len(reopen)} (belum jatuh tempo, taruhan diteruskan)")
    print(f"  -> superseded: {len(dead)} (lewat tempo, netral — tak dihitung menang/kalah)")
    n_rules = conn.execute("SELECT COUNT(*) FROM lessons WHERE kind='auto-rule'").fetchone()[0]
    batches = conn.execute(
        "SELECT COUNT(DISTINCT ts) FROM lessons WHERE kind='auto-rule'").fetchone()[0]
    print(f"lessons hapus: {n_lessons}")
    print(f"auto-rule hapus: {n_rules} ({batches} batch — disuling dari statistik tercemar)")
    for r in fake[:10]:
        print(f"    #{r['id']:>4} {r['ticker']:<6} {r['direction']:<5} "
              f"{r['actual_pct']:+.2f}% {r['outcome']:<5} dibuat {r['ts'][:16]} "
              f"-> dinilai {r['resolved_at'][:16]}")
    if len(fake) > 10:
        print(f"    ... +{len(fake) - 10} lagi")

    if not apply:
        print("\nDRY-RUN. Tambahkan --apply untuk mengerjakan.")
        return 0
    if not ids and not n_rules:
        print("\nTidak ada yang perlu dibersihkan.")
        return 0

    bak = Path(db).parent / "backups" / f"pre-clean-{time.strftime('%Y%m%d-%H%M%S')}.db"
    bak.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db, bak)
    print(f"\nbackup       : {bak}")

    with conn:
        # Lapis 2: aturan harian hasil sulingan atas statistik tercemar (lihat docstring).
        conn.execute("DELETE FROM lessons WHERE kind='auto-rule'")
        if reopen:
            q = ",".join("?" * len(reopen))
            conn.execute(f"UPDATE predictions SET status='open', resolved_at=NULL, "
                         f"actual_pct=NULL, outcome=NULL WHERE id IN ({q})", reopen)
        if dead:
            q = ",".join("?" * len(dead))
            conn.execute(f"UPDATE predictions SET status='superseded', actual_pct=NULL, "
                         f"outcome=NULL WHERE id IN ({q})", dead)
        if ids:
            q = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM lessons WHERE prediction_id IN ({q})", ids)
    print(f"selesai      : {len(reopen)} open, {len(dead)} superseded, "
          f"{n_lessons} lesson + {n_rules} auto-rule dihapus")
    print("             : jalankan knowledge.auto_tune() untuk menyuling ulang dari data bersih")

    sisa = find_fake(conn)
    print(f"verifikasi   : sisa label palsu = {len(sisa)} (harus 0)")
    return 0 if not sisa else 1


if __name__ == "__main__":
    # --db=PATH: jalankan atas salinan (gladi resik sebelum menyentuh DB live).
    _db = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--db=")), None)
    raise SystemExit(main("--apply" in sys.argv, _db))
