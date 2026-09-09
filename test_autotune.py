"""Cek agen EVALUATOR / auto_tune (python test_autotune.py) — DB in-memory, LLM di-mock.
Kontrak: batch harian menggantikan batch lama; guardrails cap 200 char & skip saat output
rusak / data kurang; aturan aktif terinject ke build_knowledge_prompt tanpa dobel."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import config
from app import repo
from app.agents import knowledge, llm

_MEM = sqlite3.connect(":memory:", check_same_thread=False)
_MEM.row_factory = sqlite3.Row
_MEM.execute("CREATE TABLE lessons(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, "
             "ticker TEXT, kind TEXT, lesson TEXT, prediction_id INTEGER)")
# horizon_days/reasoning/ts WAJIB ada: performance_digest kini memakai
# repo.resolved_predictions() (definisi resolusi sah tunggal), bukan query mentah.
_MEM.execute("CREATE TABLE predictions(id INTEGER PRIMARY KEY, status TEXT, ts TEXT, "
             "resolved_at TEXT, direction TEXT, probability REAL, outcome TEXT, "
             "actual_pct REAL, factors_json TEXT, horizon_days INTEGER, reasoning TEXT)")

_TS = [0]


def _next_iso():
    _TS[0] += 1
    return (datetime(2026, 7, 5, tzinfo=timezone.utc) + timedelta(seconds=_TS[0])).isoformat()


@contextmanager
def _tx():
    yield _MEM
    _MEM.commit()


def _setup(resolved_n=12, rules_out=None):
    repo.get_conn = lambda: _MEM
    repo.tx = _tx
    repo.now_iso = _next_iso
    repo.log = lambda *a, **k: None
    repo.factor_scoreboard = lambda min_n=3: [{"factor": "foreign-buy", "n": 8, "win": 6}]
    from app.agents import skill
    skill.calibration_guidance = lambda: "KALIBRASI: tes"
    _MEM.execute("DELETE FROM predictions")
    now = datetime.now(timezone.utc)
    for i in range(resolved_n):
        # jarak ts->resolved_at 7 hari: dijamin melewati sesi bursa apa pun hari test dijalankan
        # (has_new_session), sementara resolved_at tetap di dalam jendela 14 hari digest.
        _MEM.execute("INSERT INTO predictions(status, ts, resolved_at, direction, probability, "
                     "outcome, actual_pct, horizon_days, reasoning) "
                     "VALUES ('resolved',?,?,?,?,?,?,3,'taruhan nyata')",
                     ((now - timedelta(days=8)).isoformat(),
                      (now - timedelta(days=1)).isoformat(), "UP", 60, "win", 1.0))
    _MEM.commit()
    if rules_out is not None:
        llm.chat_chain = lambda *a, **k: {"json": {"rules": rules_out}, "provider": "mistral"}


def test_batch_replaces_old():
    _setup(rules_out=["Aturan lama satu untuk arah DOWN.", "Aturan lama dua soal volume."])
    assert knowledge.auto_tune() == 2
    assert len(repo.active_auto_rules()) == 2
    _setup(rules_out=["Aturan BARU tunggal: jangan BUY saat asing net-jual."])
    assert knowledge.auto_tune() == 1
    active = repo.active_auto_rules()
    assert len(active) == 1 and "BARU" in active[0], active
    print("batch baru menggantikan batch lama ok")


def test_cap_200_char_and_max_rules():
    long_rule = "Aturan sangat panjang " * 30           # >200 char
    _setup(rules_out=[long_rule] + [f"Aturan pendek nomor {i} yang valid." for i in range(15)])
    n = knowledge.auto_tune()
    assert n == config.AUTO_RULES_MAX, n
    active = repo.active_auto_rules()
    assert all(len(r) <= 200 for r in active), max(len(r) for r in active)
    print("cap 200 char + maks AUTO_RULES_MAX ok")


def test_invalid_output_keeps_old_batch():
    _setup(rules_out=["Aturan sah yang harus selamat dari batch rusak."])
    knowledge.auto_tune()
    _setup()                                             # LLM balas bukan-list
    llm.chat_chain = lambda *a, **k: {"json": {"rules": "bukan list"}, "provider": "x"}
    assert knowledge.auto_tune() == 0
    assert "selamat" in repo.active_auto_rules()[0]
    print("output rusak -> skip, batch lama tetap aktif ok")


def test_skip_when_few_resolved():
    _setup(resolved_n=5, rules_out=["Tak boleh sampai tertulis."])
    assert knowledge.auto_tune() == 0
    print("skip saat <10 resolved 7 hari ok")


def test_prompt_injection_no_double():
    _setup(rules_out=["Aturan injeksi: cek arus asing sebelum BUY."])
    knowledge.auto_tune()
    text = knowledge.build_knowledge_prompt(repo.recent_lessons(15))
    assert "== ATURAN DINAMIS" in text and "Aturan injeksi" in text
    # auto-rule TIDAK boleh dobel lewat blok PELAJARAN (recent_lessons mengecualikannya)
    assert text.count("Aturan injeksi") == 1
    assert all(ls["kind"] not in ("agent-note", "auto-rule") for ls in repo.recent_lessons(50))
    print("injeksi prompt tanpa dobel ok")


if __name__ == "__main__":
    test_batch_replaces_old()
    test_cap_200_char_and_max_rules()
    test_invalid_output_keeps_old_batch()
    test_skip_when_few_resolved()
    test_prompt_injection_no_double()
    print("ALL OK")
