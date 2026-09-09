"""Audit kesehatan provider LLM memakai payload produksi.

Pin provider mati diam-diam: kunci dicabut, kredit habis, model ditarik. Gejalanya bukan
crash melainkan prediksi yang pelan-pelan jadi hasil fallback. Skrip ini menembak tiap
pasangan (provider, model) yang dipakai chain produksi dalam tiga lapis: katalog /models,
satu panggilan JSON, dan tool-call untuk chain yang memang memakai tool.

    python tools/provider_audit.py
    python tools/provider_audit.py --selftest

Exit code 1 kalau ada chain produksi tanpa satu pun anggota sehat.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import requests

import config
from app.agents import llm

CHAINS = {n: getattr(config, n) for n in dir(config) if n.endswith("CHAIN")}
CHAINS["COUNCIL_MEMBERS"] = [(p, m) for _role, p, m in config.COUNCIL_MEMBERS]

# Payload berbentuk keputusan CTO: JSON ketat, angka, bukan obrolan.
JSON_PROBE = [
    {"role": "system", "content": "Jawab HANYA JSON valid."},
    {"role": "user", "content": '{"ticker":"BBCA","close":9250,"chg_pct":-1.2,'
     '"vol_ratio":1.8}\nPutuskan. Balas {"direction":"UP|DOWN|FLAT",'
     '"probability":0-100,"reason":"<=15 kata"}'},
]
TOOL_SCHEMA = [{"type": "function", "function": {
    "name": "get_price", "description": "Harga terakhir satu saham.",
    "parameters": {"type": "object", "properties": {
        "ticker": {"type": "string"}}, "required": ["ticker"]}}}]


def _models_listed(provider: str, model: str) -> str:
    """'ya' / 'tidak' / '?' — model masih ada di katalog provider."""
    base, key = config.PROVIDER_BASE.get(provider), config.PROVIDER_KEY.get(provider)
    try:
        r = requests.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"},
                         timeout=15)
        ids = {m.get("id", "") for m in (r.json().get("data") or [])}
    except Exception:  # noqa: BLE001 — katalog opsional; jangan gagalkan audit
        return "?"
    if not ids:
        return "?"
    return "ya" if model in ids else "tidak"


def probe(provider: str, model: str, want_tool: bool) -> dict:
    """Satu pasangan produksi -> {listed, json, tool, ms, err}."""
    out = {"provider": provider, "model": model, "listed": "-", "json": "-",
           "tool": "-", "ms": 0, "err": ""}
    if not config.PROVIDER_KEY.get(provider):
        out["err"] = "KUNCI KOSONG"
        return out
    llm._reset(provider)                       # audit menembak langsung, abaikan breaker
    out["listed"] = _models_listed(provider, model)
    t0 = time.time()
    try:
        r = llm.chat_provider(provider, model, JSON_PROBE, want_json=True,
                              max_tokens=200, timeout=45, retries=0)
        out["json"] = "ok" if isinstance(r.get("json"), dict) else "rusak"
    except Exception as e:  # noqa: BLE001
        out["err"] = str(e)[:90]
        out["json"] = "gagal"
    out["ms"] = int((time.time() - t0) * 1000)
    if want_tool and out["json"] == "ok":
        called = []
        try:
            llm._reset(provider)
            llm.chat_chain_tools([(provider, model)],
                                 [{"role": "user", "content": "Harga BBCA sekarang berapa?"}],
                                 TOOL_SCHEMA,
                                 {"get_price": lambda ticker: called.append(ticker) or "9250"},
                                 max_rounds=2, max_tokens=200, timeout=45)
            out["tool"] = "ok" if called else "tak dipanggil"
        except Exception as e:  # noqa: BLE001
            out["tool"] = "gagal"
            out["err"] = out["err"] or str(e)[:90]
    return out


def healthy(row: dict) -> bool:
    """Sehat = JSON produksi ter-parse DAN (kalau chain tool) tool benar terpanggil."""
    return row["json"] == "ok" and row["tool"] in ("-", "ok")


def main() -> int:
    tool_pairs = {p for n, c in CHAINS.items() if n.endswith("TOOL_CHAIN") for p in c}
    pairs = sorted({p for c in CHAINS.values() for p in c})
    rows = {}
    print(f"{'provider/model':45s} {'katalog':8s} {'json':7s} {'tool':13s} {'ms':>6s}  error")
    for pair in pairs:
        r = probe(*pair, want_tool=pair in tool_pairs)
        rows[pair] = r
        print(f"{r['provider']+'/'+r['model']:45s} {r['listed']:8s} {r['json']:7s} "
              f"{r['tool']:13s} {r['ms']:6d}  {r['err']}")

    print("\nchain produksi:")
    broken = []
    for name, chain in sorted(CHAINS.items()):
        ok = [f"{p}/{m}" for p, m in chain if healthy(rows[(p, m)])]
        print(f"  {name:20s} {len(ok)}/{len(chain)} sehat" +
              (f" -> primary hidup: {ok[0]}" if ok else "  <<< KOSONG, chain ini MATI"))
        if not ok:
            broken.append(name)
    if broken:
        print("\nMATI: " + ", ".join(broken) + " — engine memakai jalur fallback di sini.")
    return 1 if broken else 0


def selftest() -> None:
    assert healthy({"json": "ok", "tool": "-"})
    assert healthy({"json": "ok", "tool": "ok"})
    assert not healthy({"json": "ok", "tool": "tak dipanggil"})   # lulus JSON, tool bohong
    assert not healthy({"json": "rusak", "tool": "-"})
    assert not healthy({"json": "gagal", "tool": "-"})
    assert CHAINS["TRADER_CTO_CHAIN"], "chain produksi tak terbaca dari config"
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        sys.exit(main())
