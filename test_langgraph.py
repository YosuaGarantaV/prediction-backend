"""Self-check jalur LangGraph (tanpa jaringan — agen di-mock). Membuktikan graf MAS penuh:
analis+sentimen jalan dulu, sepakat→berhenti, ragu→dewan, tak sepakat→loop rebut sampai
DEBATE_ROUNDS (hanya bila layak — DEBATE_ONLY_ACTIONABLE), semua jalur berakhir di risk_gate,
dan graf bisa dirender jadi Mermaid (untuk tesis)."""
import sys

sys.path.insert(0, ".")
import config
from app.agents import graph, analyst as A, trader as T, council as C, sentiment as S, risk as R

_ORIG = (T.decide, A.rebut, C.discuss, A.analyze_cached, S.note, R.assess)


def _restore():
    T.decide, A.rebut, C.discuss, A.analyze_cached, S.note, R.assess = _ORIG


def _base():
    """Mock dasar tiap tes: analis termemo, sentimen lexicon, risk approve."""
    A.analyze_cached = lambda *a, **k: "tesis awal"
    S.note = lambda t: "AGEN SENTIMEN [lexicon]: FLAT (yakin 50%) — tes"
    R.assess = lambda d: {"verdict": "APPROVE", "reason": "tes", "size_cap": None}
    A.rebut = lambda *a, **k: "v2"
    C.discuss = lambda *a, **k: ""


def _mk(direction="DOWN", prob=62, agree=True, action="HOLD", council_calls=0):
    d = {"ticker": "KETR", "direction": direction, "probability": prob, "horizon_days": 5,
         "expected_pct": -2, "target_price": 98, "entry_price": 100, "action": action,
         "size_pct": 5, "key_factors": [], "critique": "kritik", "reasoning": "r",
         "term": "pendek", "agree": agree}
    if council_calls:
        d["_agent_tools"] = {"tool_calls": 1, "council_calls": council_calls}
    return d


def _run():
    return graph.run_flow("KETR", {"price": 100.0, "volume": 5e6}, None, 1e7)


def test_agree_stops_immediately():
    """Trader langsung sepakat & yakin (75%, HOLD) → tak ada dewan, tak ada rebut."""
    _base()
    calls = {"trader": 0, "council": 0, "rebut": 0}
    T.decide = lambda *a, **k: (calls.__setitem__("trader", calls["trader"] + 1) or _mk(prob=75))
    C.discuss = lambda *a, **k: (calls.__setitem__("council", calls["council"] + 1) or "x")
    A.rebut = lambda *a, **k: (calls.__setitem__("rebut", calls["rebut"] + 1) or "v2")
    try:
        d = _run()
    finally:
        _restore()
    assert calls == {"trader": 1, "council": 0, "rebut": 0}, calls
    assert "[ANALYST #1]" in d["_analyst_view"]
    assert "[AGEN SENTIMEN]" in d["_analyst_view"]        # sentimen selalu hadir di blackboard
    assert "[RISK AGENT] APPROVE" in d["_analyst_view"]   # semua jalur lewat risk gate
    print("PASS agree-stop:", calls)


def test_uncertain_triggers_council():
    """Ragu (62%) & CTO tak konsultasi → node council jalan; lalu sepakat → berhenti."""
    _base()
    calls = {"trader": 0, "council": 0}
    def trade(*a, council_notes="", **k):
        calls["trader"] += 1
        return _mk(prob=75, agree=True) if council_notes else _mk(prob=62, agree=True)
    T.decide = trade
    C.discuss = lambda *a, **k: (calls.__setitem__("council", calls["council"] + 1) or "PENDAPAT DEWAN: DOWN")
    try:
        d = _run()
    finally:
        _restore()
    assert calls["council"] == 1, calls
    assert calls["trader"] == 2, calls          # initial + pasca-dewan
    assert "[DEWAN]" in d["_analyst_view"]
    print("PASS uncertain->council:", calls)


def test_cto_self_consulted_skips_council_node():
    """CTO sudah konsultasi via tool (council_calls>0) → node council DILEWATI (anti dobel)."""
    _base()
    calls = {"council": 0}
    T.decide = lambda *a, **k: _mk(prob=62, agree=True, council_calls=1)
    C.discuss = lambda *a, **k: (calls.__setitem__("council", 99) or "x")
    try:
        _run()
    finally:
        _restore()
    assert calls["council"] == 0, calls
    print("PASS cto-self-consulted: node dewan dilewati")


def test_disagree_loops_until_cap():
    """Tak pernah sepakat (dan layak debat: prob 75) → loop rebut sampai DEBATE_ROUNDS."""
    _base()
    config.DEBATE_ROUNDS = 3
    calls = {"trader": 0, "rebut": 0}
    T.decide = lambda *a, **k: (calls.__setitem__("trader", calls["trader"] + 1)
                                or _mk(prob=75, agree=False))   # yakin (no council) tapi tak sepakat
    A.rebut = lambda *a, **k: (calls.__setitem__("rebut", calls["rebut"] + 1) or f"v{calls['rebut']}")
    try:
        d = _run()
    finally:
        _restore()
        config.DEBATE_ROUNDS = 2
    assert calls["rebut"] == 2, calls
    assert calls["trader"] == 3, calls           # initial + 2 rebut-trade
    assert "[TRADER/CTO #3]" in d["_analyst_view"], d["_analyst_view"][-200:]
    print("PASS disagree-loop:", calls)


def test_low_stakes_disagreement_skips_rebut():
    """DEBATE_ONLY_ACTIONABLE: tak sepakat TAPI low-stakes (HOLD, prob 55) → rebut DILEWATI
    (hemat ~3.8k token/putaran)."""
    _base()
    config.DEBATE_ROUNDS = 2
    calls = {"rebut": 0}
    T.decide = lambda *a, **k: _mk(prob=55, agree=False, action="HOLD")
    A.rebut = lambda *a, **k: (calls.__setitem__("rebut", calls["rebut"] + 1) or "v2")
    try:
        d = _run()
    finally:
        _restore()
    assert calls["rebut"] == 0, calls
    assert "[RISK AGENT]" in d["_analyst_view"]
    print("PASS low-stakes: rebut dilewati (hemat token)")


def test_risk_veto_downgrades_to_hold():
    """Risk Agent VETO → action turun ke HOLD, alasan tercatat (prediksi tetap ada)."""
    _base()
    T.decide = lambda *a, **k: _mk(prob=75, agree=True, action="BUY", council_calls=1)
    R.assess = lambda d: {"verdict": "VETO", "reason": "falling knife (-15%/3hr)", "size_cap": None}
    try:
        d = _run()
    finally:
        _restore()
    assert d["action"] == "HOLD", d["action"]
    assert d["risk"]["verdict"] == "VETO"
    assert any("[RISK] VETO" in str(f) for f in d["key_factors"]), d["key_factors"]
    assert "[RISK AGENT] VETO" in d["_analyst_view"]
    print("PASS risk-veto: BUY diturunkan ke HOLD + alasan tercatat")


def test_risk_resize_clamps_size():
    """Risk Agent RESIZE → size_pct dipangkas ke cap."""
    _base()
    T.decide = lambda *a, **k: _mk(prob=75, agree=True, action="BUY", council_calls=1)
    R.assess = lambda d: {"verdict": "RESIZE", "reason": "ruang 3%", "size_cap": 3.0}
    try:
        d = _run()
    finally:
        _restore()
    assert d["action"] == "BUY" and d["size_pct"] == 3.0, (d["action"], d["size_pct"])
    print("PASS risk-resize: size dipangkas ke cap")


def test_mermaid_renders():
    """Graf bisa dirender Mermaid (nilai tesis) & memuat SEMUA node agen."""
    m = graph.render_mermaid()
    for node in ("analyst", "sentiment", "initial_trade", "council", "rebut", "risk_gate"):
        assert node in m, (node, m[:300])
    print("PASS mermaid: graf ter-render (", len(m), "char )")


if __name__ == "__main__":
    test_agree_stops_immediately()
    test_uncertain_triggers_council()
    test_cto_self_consulted_skips_council_node()
    test_disagree_loops_until_cap()
    test_low_stakes_disagreement_skips_rebut()
    test_risk_veto_downgrades_to_hold()
    test_risk_resize_clamps_size()
    test_mermaid_renders()
    print("ALL OK - graf MAS penuh: analis+sentimen+debat+risk gate + render diagram.")
