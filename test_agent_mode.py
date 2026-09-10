"""Test menyeluruh MODE AGENT (tanpa jaringan, mock provider) — edge case yang menentukan
apakah sistem layak disebut multi-agent DAN selamat saat komponennya bermasalah:
tool tak dikenal / tool error / loop mentok budget / arg rusak / JSON rusak / fallback CTO /
fallback analis / argumen tool mengalir benar. ASCII-only print (console Windows cp1252)."""
import sys
import types

import pytest

sys.path.insert(0, ".")
import config
from app.agents import llm


class _NS(types.SimpleNamespace):
    pass


def _resp(content=None, tool_calls=None):
    return _NS(choices=[_NS(message=_NS(content=content, tool_calls=tool_calls))], usage=None)


def _tc(tid, name, args):
    return _NS(id=tid, type="function", function=_NS(name=name, arguments=args))


def _fake_provider(script):
    """Client palsu: tiap create() mengambil respons berikutnya dari script (list)."""
    state = {"i": 0, "kwargs": []}

    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    state["kwargs"].append(kw)
                    r = script[min(state["i"], len(script) - 1)]
                    state["i"] += 1
                    if isinstance(r, Exception):
                        raise r
                    return r
    return _Client(), state


def _with_fake(name, client):
    llm.config.LLM_MIN_INTERVAL = 0.0
    llm.config.PROVIDER_BASE[name] = "http://x"
    llm.config.PROVIDER_KEY[name] = "k"
    llm._clients.clear()
    llm._client = lambda base, key, _c=client: _c


_ORIG_CLIENT = llm._client


def _cleanup(*names):
    llm._client = _ORIG_CLIENT
    for n in names:
        llm.config.PROVIDER_KEY.pop(n, None)
        llm.config.PROVIDER_BASE.pop(n, None)
    llm._cb_until.clear()


SPEC = [{"type": "function", "function": {"name": "t1", "description": "x",
         "parameters": {"type": "object", "properties": {}, "required": []}}}]


def test_unknown_tool_and_error_tool():
    """Tool tak dikenal & tool melempar exception: hasil error DISUNTIK (bukan crash),
    loop lanjut sampai jawaban final."""
    client, st = _fake_provider([
        _resp(tool_calls=[_tc("a", "GHOST", "{}"), _tc("b", "t1", "{}")]),
        _resp(content="final ok"),
    ])
    _with_fake("_f1", client)
    try:
        def boom(**_):
            raise RuntimeError("meledak")
        out = llm.chat_chain_tools([("_f1", "m")], [{"role": "user", "content": "x"}],
                                   SPEC, {"t1": boom}, max_rounds=3)
    finally:
        _cleanup("_f1")
    assert out["content"] == "final ok"
    tool_msgs = [m for m in st["kwargs"][1]["messages"] if m["role"] == "tool"]
    assert "tak dikenal" in tool_msgs[0]["content"], tool_msgs[0]
    assert "error" in tool_msgs[1]["content"], tool_msgs[1]
    print("PASS unknown/error tool: loop selamat, error tersuntik sbg hasil tool")


def test_budget_exhausted_forces_answer():
    """Model rakus tool tiap putaran: putaran terakhir DIPAKSA jawab (tool_choice=none),
    tak ada loop tak berujung."""
    client, st = _fake_provider([
        _resp(tool_calls=[_tc("a", "t1", "{}")]),
        _resp(tool_calls=[_tc("b", "t1", "{}")]),
        _resp(content="dipaksa jawab"),
    ])
    _with_fake("_f2", client)
    try:
        out = llm.chat_chain_tools([("_f2", "m")], [{"role": "user", "content": "x"}],
                                   SPEC, {"t1": lambda **_: "data"}, max_rounds=2)
    finally:
        _cleanup("_f2")
    assert out["content"] == "dipaksa jawab"
    assert st["kwargs"][-1]["tool_choice"] == "none", st["kwargs"][-1]["tool_choice"]
    assert out["tool_calls"] == 2
    print("PASS budget: putaran akhir tool_choice=none, jawaban keluar")


def test_malformed_args_and_argument_flow():
    """Argumen JSON rusak -> args={} (tool tetap jalan); argumen valid MENGALIR ke impl."""
    client, _ = _fake_provider([
        _resp(tool_calls=[_tc("a", "t1", "{rusak!!"), _tc("b", "t1", '{"ticker":"BBCA"}')]),
        _resp(content="ok"),
    ])
    _with_fake("_f3", client)
    got = []
    try:
        out = llm.chat_chain_tools([("_f3", "m")], [{"role": "user", "content": "x"}],
                                   SPEC, {"t1": lambda ticker="", **_: got.append(ticker) or f"[{ticker}]"},
                                   max_rounds=3)
    finally:
        _cleanup("_f3")
    assert out["content"] == "ok"
    assert got == ["", "BBCA"], got   # rusak -> default; valid -> sampai ke impl
    print("PASS args: rusak jadi default, valid mengalir (", got, ")")


def test_want_json_fallback_provider():
    """Provider-1 balas teks non-JSON -> provider-2 dicoba -> JSON valid dipakai."""
    c1, _ = _fake_provider([_resp(content="bukan json")])
    c2, _ = _fake_provider([_resp(content='{"direction":"UP"}')])
    llm.config.LLM_MIN_INTERVAL = 0.0
    for n in ("_fa", "_fb"):
        llm.config.PROVIDER_BASE[n] = "http://x"
        llm.config.PROVIDER_KEY[n] = "k"
    llm._clients.clear()
    llm._client = lambda base, key: c1 if getattr(llm._client, "flip", True) else c2
    # trick sederhana: bedakan lewat urutan panggil — pakai counter
    calls = {"n": 0}
    def pick(base, key):
        calls["n"] += 1
        return c1 if calls["n"] == 1 else c2
    llm._client = pick
    try:
        out = llm.chat_chain_tools([("_fa", "m"), ("_fb", "m")],
                                   [{"role": "user", "content": "x"}],
                                   SPEC, {}, max_rounds=1, want_json=True)
    finally:
        _cleanup("_fa", "_fb")
    assert out["json"] == {"direction": "UP"} and out["provider"] == "_fb", out
    print("PASS want_json fallback: JSON rusak -> provider berikutnya")


def test_trader_fallback_to_plain_json():
    """Jalur tool CTO gagal total -> decide() fallback ke ask_trader_json (keputusan tetap ada)."""
    from app import repo
    from app.agents import trader
    config.TRADER_TOOLS = True
    orig_tools, orig_ask, orig_log = llm.chat_chain_tools, llm.ask_trader_json, repo.log
    llm.chat_chain_tools = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("semua mati"))
    llm.ask_trader_json = lambda m: {"provider": "backup", "json": {
        "direction": "DOWN", "probability": 60, "horizon_days": 3, "expected_pct": -2,
        "action": "HOLD", "critique": "c", "reasoning": "r", "agree": True}}
    repo.log = lambda *a, **k: None
    try:
        d = trader.decide("KETR", {"price": 100.0, "volume": 5e6}, "tesis", None, 1e7)
    finally:
        llm.chat_chain_tools, llm.ask_trader_json, repo.log = orig_tools, orig_ask, orig_log
        config.TRADER_TOOLS = config.AGENT_MODE
    assert d["direction"] == "DOWN" and "_agent_tools" not in d, d
    print("PASS CTO fallback: tool mati -> JSON biasa, keputusan tetap keluar")


def test_analyst_fallback_to_inject_all():
    """Jalur tool analis gagal total -> analyze() fallback inject-all (tesis tetap ada)."""
    from app import repo
    from app.agents import analyst, prompts
    config.ANALYST_TOOLS = True
    orig = (llm.chat_chain_tools, llm.ask_analyst, repo.log,
            prompts.system_prompt, prompts.analyst_user_prompt)
    llm.chat_chain_tools = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mati"))
    llm.ask_analyst = lambda m: {"content": "TESIS CADANGAN", "provider": "x", "model": "y"}
    repo.log = lambda *a, **k: None
    prompts.system_prompt = lambda: "SYS"
    prompts.analyst_user_prompt = lambda *a, **k: "USR"
    try:
        v = analyst.analyze("KETR", {"price": 100.0, "features_json": "{}"})
    finally:
        (llm.chat_chain_tools, llm.ask_analyst, repo.log,
         prompts.system_prompt, prompts.analyst_user_prompt) = orig
        config.ANALYST_TOOLS = config.AGENT_MODE
    assert v == "TESIS CADANGAN", v
    print("PASS analis fallback: tool mati -> inject-all, tesis tetap keluar")


def test_agent_memory():
    """Tool remember: catatan tersimpan, validasi input LLM, dan MUNCUL di prompt berikutnya."""
    from app import db, repo
    db.init_db()
    from app.agents import analyst, prompts
    tkr = "ZZTST"
    conn = repo.get_conn()
    conn.execute("DELETE FROM lessons WHERE ticker=?", (tkr,)); conn.commit()
    # validasi: sampah pendek ditolak
    assert "tidak disimpan" in analyst._remember(tkr, "  x ")
    assert "dicatat" in analyst._remember(tkr, "volume sering palsu jelang ARA, cek broker summary")
    notes = repo.agent_notes(tkr)
    assert len(notes) == 1 and "volume" in notes[0], notes
    # injeksi: prompt lean saham ini memuat catatan milik agen
    p = prompts.analyst_user_prompt(tkr, {"price": 100.0, "features_json": "{}"}, lean=True)
    assert "CATATANMU SENDIRI" in p and "volume sering palsu" in p
    # lesson global TIDAK tercemar agent-note
    assert all(l["kind"] != "agent-note" for l in repo.recent_lessons(50))
    conn.execute("DELETE FROM lessons WHERE ticker=?", (tkr,)); conn.commit()
    print("PASS agent memory: remember -> tersimpan -> terinject, lesson global bersih")


def test_agent_attention_queue():
    """Tool suggest_ticker: validasi, dedup, cap antrean, drain mengosongkan."""
    from app.agents import analyst
    from app import repo
    # `_suggest` memvalidasi lewat repo.get_quote, jadi prasyaratnya tabel quotes TERISI —
    # bukan daftar universe. Klon baru belum pernah menarik harga, dan itu bukan kode rusak.
    if not repo.get_quote("BBCA"):
        pytest.skip("tabel quotes kosong — jalankan engine sekali dulu (python run.py)")
    analyst.drain_suggestions()  # bersihkan sisa
    assert "tidak valid" in analyst._suggest("KETR", "../x", "r")
    assert "tidak valid" in analyst._suggest("KETR", "KETR", "diri sendiri")     # self-suggest ditolak
    assert "tidak ada di universe" in analyst._suggest("KETR", "ZZZZZ", "r")     # tak dikenal DB
    assert "antrean" in analyst._suggest("KETR", "BBCA", "peer bank lebih menarik")
    assert "sudah dalam antrean" in analyst._suggest("KETR", "BBCA", "dobel")    # dedup
    q = analyst.drain_suggestions()
    assert q == [("BBCA", "peer bank lebih menarik")], q
    assert analyst.drain_suggestions() == []                                     # kosong setelah drain
    print("PASS agent attention: valid/dedup/drain bekerja")


def test_critique_text_readable():
    """Kritik/reasoning berbentuk dict/list dari model di-unwrap jadi kalimat terbaca
    (dulu json.dumps mentah — 11% kritik tampil '{"setuju": ...}', audit 2026-07-07)."""
    from app.agents.trader import _text
    assert _text({"setuju": "tren bearish", "tolak": ["rebound", "volume"]}) == \
        "setuju: tren bearish; tolak: rebound; volume"
    assert _text(["a", "b"]) == "a; b"
    assert _text(None) == "" and _text("abc") == "abc" and _text(42) == "42"
    print("PASS critique dict/list -> kalimat terbaca (bukan JSON mentah)")


if __name__ == "__main__":
    test_critique_text_readable()
    test_unknown_tool_and_error_tool()
    test_budget_exhausted_forces_answer()
    test_malformed_args_and_argument_flow()
    test_want_json_fallback_provider()
    test_trader_fallback_to_plain_json()
    test_analyst_fallback_to_inject_all()
    test_agent_memory()
    test_agent_attention_queue()
    print("ALL OK - mode agent tahan: tool hantu, tool meledak, budget habis, arg rusak,")
    print("JSON rusak lintas provider, CTO & analis fallback mulus.")

def test_tool_loop_ganti_system_ramping_setelah_ronde1():
    """Riwayat dikirim ULANG tiap ronde tool, jadi system prompt penuh dibayar berkali-kali.
    Ronde 2+ harus memakai system ramping; rulebook cukup sekali di ronde 1."""
    from app.agents import llm
    dilihat = []

    class _Fn:
        def __init__(self, n, a): self.name, self.arguments = n, a

    class _TC:
        def __init__(self): self.id, self.type, self.function = "c1", "function", _Fn("get_x", "{}")

    class _Msg:
        def __init__(self, tcs, content=""): self.tool_calls, self.content = tcs, content

    class _Resp:
        def __init__(self, msg): self.choices = [type("C", (), {"message": msg})()]; self.usage = None

    ronde = {"n": 0}

    def fake_create(client, key, **kw):
        dilihat.append(kw["messages"][0]["content"])
        ronde["n"] += 1
        return _Resp(_Msg([_TC()]) if ronde["n"] == 1 else _Msg(None, "tesis final"))

    llm._create = fake_create
    llm._cooling = lambda p: False
    llm._client = lambda b, k: object()
    import config
    config.PROVIDER_BASE["_t"] = "http://x"
    config.PROVIDER_KEY["_t"] = "k"
    msgs = [{"role": "system", "content": "RULEBOOK PANJANG " * 200},
            {"role": "user", "content": "analisis BBCA"}]
    out = llm._tool_loop("_t", "m", list(msgs), [], {"get_x": lambda: "9"},
                         2, 0.7, 100, 30, slim_system="RAMPING")
    assert out["content"] == "tesis final"
    assert len(dilihat) == 2, dilihat
    assert dilihat[0].startswith("RULEBOOK"), "ronde 1 wajib bawa rulebook penuh"
    assert dilihat[1] == "RAMPING", "ronde 2 wajib sudah ramping"
    assert len(dilihat[1]) < len(dilihat[0]) / 10
    print("tool loop: system ramping dipakai sejak ronde 2 ok")

