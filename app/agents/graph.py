"""Orkestrasi MAS TERPUSAT sebagai StateGraph LangGraph (jalur utama saat USE_LANGGRAPH).

DebateState = BLACKBOARD eksplisit: semua agen membaca/menulis state yang sama; edges =
protokol koordinasi terpusat (koordinator = graf, bukan `if` tersebar di orchestrator).
Node hanya MEMBUNGKUS fungsi agen yang sudah ada (analyst/sentiment/trader/council/risk)
→ lapisan LLM, rantai fallback, circuit-breaker, tool-loop TETAP di llm.py (tak diduplikasi).
Graf gagal → orchestrator jatuh ke jalur manual → heuristik (3 lapis; sistem simulasi
uang tak pernah buta).

Struktur:
    START → analyst → sentiment → initial_trade
        initial_trade → [ragu & CTO belum konsultasi] → council
                      → [belum sepakat & layak debat] → rebut ⟲
        semua jalur → risk_gate (APPROVE/RESIZE/VETO, rule-based 0 token) → END
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

import config
from app.agents import analyst, council, risk, sentiment, trader


class DebateState(TypedDict):
    ticker: str
    quote: dict
    view: str
    position: Optional[dict]
    cash: float
    horizon: Optional[int]
    note: Optional[str]
    sent_note: str
    notes: str
    decision: dict
    rnd: int
    transcript: list


def _dsum(d: dict) -> str:
    return (f"{d.get('direction','?')} {(d.get('probability') or 0):.0f}% "
            f"/{d.get('horizon_days','?')}h [{d.get('term','pendek')}] -> "
            f"{d.get('action','?')} (exp {d.get('expected_pct','?')}%)")


# --- nodes: tiap node MEMBUNGKUS fungsi agen nyata (llm fallback tetap jalan di dalamnya) ---
def _n_analyst(state: DebateState) -> dict:
    view = analyst.analyze_cached(state["ticker"], state["quote"],
                                  note=state.get("note"), horizon=state["horizon"])
    return {"view": view}


def _n_sentiment(state: DebateState) -> dict:
    if not config.SENTIMENT_AGENT:
        return {"sent_note": ""}
    n = sentiment.note(state["ticker"])
    tr = list(state["transcript"]) + ([f"[AGEN SENTIMEN] {n}"] if n else [])
    return {"sent_note": n, "transcript": tr}


def _n_initial_trade(state: DebateState) -> dict:
    d = trader.decide(state["ticker"], state["quote"], state["view"],
                      state["position"], state["cash"], council_notes="",
                      horizon=state["horizon"], sentiment_note=state["sent_note"])
    tr = list(state["transcript"]) + [
        f"[ANALYST #1]\n{state['view']}",
        f"[TRADER/CTO #1] {_dsum(d)}\n{d.get('critique','')}"]
    return {"decision": d, "notes": "", "rnd": 1, "transcript": tr}


def _n_council(state: DebateState) -> dict:
    notes = council.discuss(state["ticker"], state["view"])
    tr = list(state["transcript"])
    d = state["decision"]
    if notes:
        d = trader.decide(state["ticker"], state["quote"], state["view"],
                          state["position"], state["cash"], council_notes=notes,
                          horizon=state["horizon"], sentiment_note=state["sent_note"])
        tr = [f"[DEWAN]\n{notes}"] + tr + \
             [f"[TRADER/CTO pasca-dewan] {_dsum(d)}\n{d.get('critique','')}"]
    else:
        tr = ["[DEWAN] (kosong / nonaktif)"] + tr
    return {"decision": d, "notes": notes, "transcript": tr}


def _n_rebut(state: DebateState) -> dict:
    rnd = state["rnd"] + 1
    view = analyst.rebut(state["ticker"], state["view"], state["decision"].get("critique", ""))
    d = trader.decide(state["ticker"], state["quote"], view, state["position"],
                      state["cash"], council_notes=state["notes"], horizon=state["horizon"],
                      sentiment_note=state["sent_note"])
    tr = list(state["transcript"]) + [
        f"[ANALYST #{rnd}]\n{view}",
        f"[TRADER/CTO #{rnd}] {_dsum(d)}\n{d.get('critique','')}"]
    return {"view": view, "decision": d, "rnd": rnd, "transcript": tr}


def _n_risk_gate(state: DebateState) -> dict:
    """Risk Agent (rule-based, 0 token) menilai keputusan final SEBELUM commit.
    VETO → HOLD (prediksi tetap dicatat — sama dgn semantik veto lama yang hanya
    memblokir trade); RESIZE → size dipangkas. paper.act_on_decision tetap re-check."""
    d = dict(state["decision"])
    r = risk.assess(d)
    d["risk"] = r
    tr = list(state["transcript"])
    if r["verdict"] == "VETO":
        d["action"] = "HOLD"
        d.setdefault("key_factors", []).append(f"[RISK] VETO — {r['reason']}")
        tr.append(f"[RISK AGENT] VETO — {r['reason']}")
    elif r["verdict"] == "RESIZE" and r.get("size_cap") is not None:
        d["size_pct"] = min(d.get("size_pct") or 0, r["size_cap"])
        d.setdefault("key_factors", []).append(f"[RISK] RESIZE — {r['reason']}")
        tr.append(f"[RISK AGENT] RESIZE — {r['reason']}")
    else:
        tr.append(f"[RISK AGENT] APPROVE — {r['reason']}")
    return {"decision": d, "transcript": tr}


# --- routers (kondisional = keputusan alur; kebijakan debat di trader.should_debate) ---
def _route_initial(state: DebateState) -> str:
    d = state["decision"]
    ragu = (not d.get("agree", True) or 55 <= (d.get("probability") or 50) <= 68
            or d.get("action") == "BUY")
    cto_consulted = (d.get("_agent_tools") or {}).get("council_calls", 0) > 0
    if ragu and not cto_consulted:
        return "council"
    return "rebut" if trader.should_debate(d, state["rnd"]) else "risk"


def _route_debate(state: DebateState) -> str:
    return "rebut" if trader.should_debate(state["decision"], state["rnd"]) else "risk"


_APP: Any = None


def _app():
    global _APP
    if _APP is None:
        g = StateGraph(DebateState)
        g.add_node("analyst", _n_analyst)
        g.add_node("sentiment", _n_sentiment)
        g.add_node("initial_trade", _n_initial_trade)
        g.add_node("council", _n_council)
        g.add_node("rebut", _n_rebut)
        g.add_node("risk_gate", _n_risk_gate)
        g.add_edge(START, "analyst")
        g.add_edge("analyst", "sentiment")
        g.add_edge("sentiment", "initial_trade")
        g.add_conditional_edges("initial_trade", _route_initial,
                                {"council": "council", "rebut": "rebut", "risk": "risk_gate"})
        g.add_conditional_edges("council", _route_debate,
                                {"rebut": "rebut", "risk": "risk_gate"})
        g.add_conditional_edges("rebut", _route_debate,
                                {"rebut": "rebut", "risk": "risk_gate"})
        g.add_edge("risk_gate", END)
        _APP = g.compile()
    return _APP


def run_flow(ticker: str, quote: dict, position: Optional[dict], cash: float,
             horizon: Optional[int] = None, note: Optional[str] = None) -> dict:
    """Jalankan graf MAS penuh (analis → sentimen → debat → risk gate) → keputusan final
    dengan _analyst_view (transcript blackboard) & risk verdict terisi."""
    init: DebateState = {"ticker": ticker, "quote": quote, "view": "", "position": position,
                         "cash": cash, "horizon": horizon, "note": note, "sent_note": "",
                         "notes": "", "decision": {}, "rnd": 1, "transcript": []}
    final = _app().invoke(init, {"recursion_limit": 50})
    d = final["decision"]
    d["_analyst_view"] = "\n\n".join(final["transcript"])
    return d


def render_mermaid() -> str:
    """Diagram Mermaid graf (untuk tesis / dokumentasi)."""
    return _app().get_graph().draw_mermaid()


if __name__ == "__main__":
    print(render_mermaid())
