"""Self-check jalur tool on-demand (tanpa jaringan): (1) message-threading lintas round-trip —
model minta tool → hasil disuntik → model menjawab final; (2) want_json — jawaban akhir CTO
harus JSON ter-parse; (3) CTO agentic memakai hasil consult_council."""
import sys
import types

sys.path.insert(0, ".")
from app.agents import llm


class _NS(types.SimpleNamespace):
    pass


def _resp(content=None, tool_calls=None):
    msg = _NS(content=content, tool_calls=tool_calls)
    return _NS(choices=[_NS(message=msg)], usage=None)


def _tc(tid, name, args):
    return _NS(id=tid, type="function", function=_NS(name=name, arguments=args))


def test_tool_loop():
    llm.config.LLM_MIN_INTERVAL = 0.0            # jangan tidur di test
    llm.config.PROVIDER_BASE["_fake"] = "http://x"
    llm.config.PROVIDER_KEY["_fake"] = "k"
    seen = {"n": 0, "tool_used": False, "roles": []}

    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    seen["n"] += 1
                    seen["roles"].append([m["role"] for m in kw["messages"]])
                    if seen["n"] == 1:                       # putaran-1: minta tool
                        return _resp(tool_calls=[_tc("c1", "get_x", "{}")])
                    return _resp(content="ARAH: UP — hasil tool dipakai")  # putaran-2: jawab

    llm._clients.clear()
    orig = llm._client
    llm._client = lambda base, key: _Client()
    try:
        def _get_x(**_):
            seen["tool_used"] = True
            return "DATA-FUNDAMENTAL-X"
        specs = [{"type": "function", "function": {
            "name": "get_x", "description": "x",
            "parameters": {"type": "object", "properties": {}, "required": []}}}]
        out = llm.chat_chain_tools([("_fake", "m")],
                                   [{"role": "user", "content": "analisa"}],
                                   specs, {"get_x": _get_x}, max_rounds=3)
    finally:
        llm._client = orig
        llm.config.PROVIDER_KEY.pop("_fake", None)
        llm.config.PROVIDER_BASE.pop("_fake", None)

    assert seen["tool_used"], "tool wajib dipanggil saat model memintanya"
    assert out["tool_calls"] == 1, f"harus 1 tool-call, dapat {out['tool_calls']}"
    assert "UP" in out["content"], f"jawaban final hilang: {out['content']!r}"
    # putaran-2 harus melihat pesan tool tersuntik (role 'tool' ada di histori)
    assert "tool" in seen["roles"][1], f"hasil tool tak disuntik balik: {seen['roles'][1]}"
    print("PASS test_tool_loop:", out["content"], "| tool_calls =", out["tool_calls"])


def test_want_json():
    """CTO agentic: consult_council dipanggil → hasilnya dipakai → jawaban akhir JSON valid."""
    llm.config.LLM_MIN_INTERVAL = 0.0
    llm.config.PROVIDER_BASE["_fake"] = "http://x"
    llm.config.PROVIDER_KEY["_fake"] = "k"
    seen = {"n": 0, "council": 0}

    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    seen["n"] += 1
                    if seen["n"] == 1:                       # CTO minta vote dewan
                        return _resp(tool_calls=[_tc("c1", "consult_council", "{}")])
                    # dewan bilang DOWN → CTO ikut, balas JSON final
                    assert any(m["role"] == "tool" and "DOWN" in m["content"]
                               for m in kw["messages"]), "hasil dewan tak sampai ke CTO"
                    return _resp(content='{"direction":"DOWN","probability":62,"agree":true}')

    llm._clients.clear()
    orig = llm._client
    llm._client = lambda base, key: _Client()
    try:
        def _council(**_):
            seen["council"] += 1
            return "PENDAPAT DEWAN: groq DOWN 70%, gemini DOWN 65%. Konsensus: DOWN:2"
        specs = [{"type": "function", "function": {
            "name": "consult_council", "description": "vote dewan",
            "parameters": {"type": "object", "properties": {}, "required": []}}}]
        out = llm.chat_chain_tools([("_fake", "m")],
                                   [{"role": "user", "content": "putuskan"}],
                                   specs, {"consult_council": _council},
                                   max_rounds=3, want_json=True)
    finally:
        llm._client = orig
        llm.config.PROVIDER_KEY.pop("_fake", None)
        llm.config.PROVIDER_BASE.pop("_fake", None)

    assert seen["council"] == 1, "consult_council harus terpanggil tepat 1x"
    assert out["json"] and out["json"]["direction"] == "DOWN", f"JSON salah: {out.get('json')}"
    print("PASS test_want_json:", out["json"])


def test_digest_empty_db():
    """performance_digest tak crash & '' saat tak ada data resolved di jendela."""
    from app.agents import skill
    d = skill.performance_digest(0)   # jendela 0 hari = pasti kosong
    assert d == "", f"digest jendela kosong harus '', dapat: {d[:80]!r}"
    d14 = skill.performance_digest(14)  # DB nyata: cukup tak melempar exception
    assert isinstance(d14, str)
    print("PASS test_digest_empty_db (digest 14d:", (d14[:60] + "...") if d14 else "kosong", ")")


def test_explore_tools():
    """Tool eksplorasi analis: validasi input LLM + hasil padat dari DB nyata (tanpa API)."""
    from app import db
    db.init_db()
    from app.agents import analyst
    # input LLM tak valid HARUS ditolak sopan (trust boundary), bukan crash/SQL aneh
    assert "tidak valid" in analyst._ticker_snapshot("../etc; DROP TABLE")
    assert "tidak valid" in analyst._ticker_snapshot("")
    ok = analyst._ticker_snapshot("BBCA")
    assert ok.startswith("BBCA:") or "tidak ada di DB" in ok, ok
    peers = analyst._peers_brief("BBCA")
    assert isinstance(peers, str) and peers, peers
    print("PASS test_explore_tools:", ok[:60], "|", peers.splitlines()[0][:60])


if __name__ == "__main__":
    test_tool_loop()
    test_want_json()
    test_digest_empty_db()
    test_explore_tools()
