"""Jalankan evaluator A/B arus-asing (item-3 NEXT_UPDATES) → tulis data/backtest_foreign_ab.json
kalau sampel cukup (>=AB_MIN prediksi resolved ber-foreign_pressure). Kalau belum cukup: lapor
saja, TIDAK menulis artefak (gate item-3 tetap HOLD). Jalankan berkala setelah data terkumpul.

Jalankan:  python tools/run_foreign_ab.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.backtest import backtest_foreign_ab

db.init_db()
print(json.dumps(backtest_foreign_ab(), indent=2, ensure_ascii=False))
