"""Agent RISK — gerbang risiko rule-based, 0 token (agen reaktif, bukan LLM).
Node graf yang menilai keputusan CTO SEBELUM commit: APPROVE / RESIZE / VETO.
Sumber aturan tunggal = paper.pre_trade_checks (ARA/ARB, anti-churn, falling knife,
valuasi ekstrem, tekanan asing, kapasitas portofolio). paper.act_on_decision tetap
re-check saat commit — defense in depth, node ini bukan penjaga satu-satunya."""
from __future__ import annotations

from app import repo
from app.trading import paper


def assess(decision: dict) -> dict:
    """Nilai keputusan → {"verdict", "reason", "size_cap"}. Tak pernah melempar:
    gerbang error → APPROVE (re-check paper saat commit tetap menjaga)."""
    ticker = decision.get("ticker", "?")
    try:
        verdict, reason, size_cap = paper.pre_trade_checks(decision)
    except Exception as e:  # noqa: BLE001
        repo.log("risk", "gate", f"{ticker}: gerbang risiko error ({e}) — "
                 f"diserahkan ke re-check commit", level="warn", ticker=ticker)
        return {"verdict": "APPROVE", "reason": f"gerbang error: {e}", "size_cap": None}
    level = "warn" if verdict != "APPROVE" else "info"
    repo.log("risk", "gate", f"{ticker}: {verdict} — {reason}", level=level, ticker=ticker)
    return {"verdict": verdict, "reason": reason, "size_cap": size_cap}
