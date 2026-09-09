"""Client LLM NVIDIA NIM (OpenAI-compatible). Dua agen: Analyst & Trader."""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from openai import OpenAI, RateLimitError, InternalServerError

import config
from app import repo

_clients: dict[str, OpenAI] = {}

# Provider yang AMAN dipaksa balas JSON object (OpenAI-compatible json_object).
# cerebras SENGAJA dikecualikan: gpt-oss (reasoning) sering balas content KOSONG saat
# json_object dipaksa → "JSON gagal parse". Tanpa paksaan, cerebras parse normal.
# nara MASUK: diuji 2026-08-10, nemotron-3-ultra & gpt-5.6-luna balas JSON valid dgn
# response_format json_object (luna JUSTRU lebih andal dipaksa: tanpa paksaan sempat balas
# "[Error] servers overloaded"). dahl BELUM diuji (endpoint 503/429) → sengaja di luar.
# nara_free/nara_smart = gateway nara yang sama dgn KUNCI berbeda (kuota+breaker terpisah) →
# ikut masuk. Tanpa ini deepseek-v4-flash-free menulis prosa dulu: 14,5-16,8s vs batas CTO 20s.
_JSON_OBJECT_OK = {"nvidia", "groq", "mistral", "gemini", "openrouter",
                   "nara", "nara_free", "nara_smart"}

# --- Rate limiter PER-PROVIDER (kuota tiap provider terpisah) ---
# Dulu 1 lock global menyerialkan SEMUA call → dewan "paralel" sebenarnya 1.8s×anggota.
# Kini tiap endpoint punya lock+jeda sendiri → provider beda jalan serempak, tiap provider
# tetap dijaga jaraknya (cegah 429). Skill sama persis, cuma berhenti menunggu sia-sia.
# 2026-09-02: kuncinya dulu base_url, sehingga nara/nara_free/nara_smart — TIGA KUNCI dengan
# TIGA kuota terpisah yang sengaja dibuat begitu — berbagi satu jalur antre. Akibatnya terukur
# di produksi: analis (nara_smart, ~42s) menahan jatah, kursi dewan (nara) ikut antre lalu
# habis waktu; 4 dari 7 ticker kehilangan kursi kontrarian. Kunci sekarang NAMA PROVIDER
# logis, sesuai maksud desain tiga-kunci itu.
_rl_guard = threading.Lock()
_rl_slots: dict[str, list] = {}  # key(nama provider logis) -> [lock, last_call_ts]


def _throttle(key: str) -> None:
    """Jaga jarak minimum antar panggilan ke PROVIDER yang SAMA (mencegah burst → 429)."""
    with _rl_guard:
        slot = _rl_slots.get(key)
        if slot is None:
            slot = [threading.Lock(), 0.0]
            _rl_slots[key] = slot
    with slot[0]:
        wait = config.LLM_MIN_INTERVAL - (time.monotonic() - slot[1])
        if wait > 0:
            time.sleep(wait)
        slot[1] = time.monotonic()


# --- Circuit breaker per-provider ---
# Provider yang baru saja kena rate-limit/timeout di-SKIP tanpa memanggil API selama cooldown.
# Ini menghentikan chain/dewan menghantam provider yang sudah ke-limit tiap siklus (biang utama
# ratusan error 429/hari). Provider pulih otomatis begitu cooldown lewat; sukses → reset langsung.
_cb_guard = threading.Lock()
_cb_until: dict[str, float] = {}   # provider -> monotonic time sampai kapan di-skip
_cb_strikes: dict[str, int] = {}   # provider -> trip beruntun tanpa sukses (backoff eksponensial)
_cb_timeouts: dict[str, int] = {}  # provider -> timeout beruntun (2 baru men-trip, lihat _trip)


class _Cooling(Exception):
    """Provider sedang cooldown → di-skip tanpa panggil API (bukan error nyata)."""


# --- Peredam log kegagalan provider ---
# Audit 2026-09-02: 11.518 dari 27.925 baris agent_logs 7 hari (41%) adalah baris yang SAMA
# persis — "provider/model gagal: 401/402/403" — diulang tiap ticker tiap siklus. Panel log
# jadi tak terbaca: suara analis/CTO/dewan tenggelam, yang persis dikeluhkan sbg "log kosong".
# Kegagalan pertama tetap dicatat UTUH (diagnosis tak hilang); pengulangan identik diringkas
# jadi satu baris per LOG_DEDUPE_SEC berisi jumlah kejadian. Bukan menyembunyikan error —
# menghitungnya.
_dedupe_guard = threading.Lock()
_dedupe: dict[str, list] = {}   # key -> [kapan boleh log lagi, jumlah tertahan]


def _log_fail(key: str, message: str) -> None:
    """Catat kegagalan provider, tapi redam pengulangan identik dalam jendela waktu."""
    now = time.monotonic()
    with _dedupe_guard:
        slot = _dedupe.get(key)
        if slot is None or now >= slot[0]:
            held = slot[1] if slot else 0
            _dedupe[key] = [now + config.LOG_DEDUPE_SEC, 0]
        else:
            slot[1] += 1
            return
    if held:
        message = f"{message} (+{held} kejadian identik diredam {config.LOG_DEDUPE_SEC}s terakhir)"
    repo.log("llm", "error", message, level="warn")


def _cooling(provider: str) -> bool:
    with _cb_guard:
        return time.monotonic() < _cb_until.get(provider, 0.0)


def _trip(provider: str, err: Exception) -> None:
    """Nyalakan cooldown sesuai jenis error. Kuota harian habis: istirahat panjang. Rate-limit
    per-menit: pendek dengan backoff eksponensial. Timeout: datar dan pendek, perlu dua kali
    beruntun. Error lain (JSON rusak, dll) tidak men-trip karena bukan soal kapasitas."""
    s = str(err).lower()
    is_limit = (isinstance(err, RateLimitError) or "too many" in s or "429" in s or "rate" in s)
    # Timeout dipisah dari rate-limit. Provider yang lambat menjawab prompt besar bukan
    # provider yang menolak, dan kalau disamakan, satu jawaban lambat menyalakan backoff
    # eksponensial yang menjatuhkan seluruh ticker sesudahnya. Timeout perlu dua kali
    # beruntun untuk men-trip, dengan cooldown datar tanpa eskalasi.
    if "per day" in s or "daily" in s or "per-day" in s:
        cd, strikes = config.CB_COOLDOWN_DAY, 0
    elif not is_limit and ("timed out" in s or "timeout" in s):
        with _cb_guard:
            n = _cb_timeouts[provider] = _cb_timeouts.get(provider, 0) + 1
        if n < 2:
            return                                   # sekali lambat = bukan provider tumbang
        cd, strikes = config.CB_TIMEOUT_COOLDOWN, 0
    elif is_limit:
        # BACKOFF EKSPONENSIAL (audit 2026-07-14: gemini trip 26x/hari — cooldown flat 90s
        # selalu pulih tepat sebelum siklus berikutnya → kuota yang memang sempit dihantam
        # ulang tiap siklus). Trip beruntun tanpa satu pun sukses = 90s → 180 → 360 → 720 →
        # cap CB_COOLDOWN_MAX. Cap dipisah dari CB_COOLDOWN_DAY (audit 2026-07-15: ini limit
        # PER-MENIT, pulih dalam menit — 2 jam terlalu lama, provider sehat ikut disidelinekan;
        # 15 mnt cukup menghentikan hammering tanpa membuang provider). Satu sukses → reset 90s.
        with _cb_guard:
            _cb_strikes[provider] = strikes = _cb_strikes.get(provider, 0) + 1
        cd = min(config.CB_COOLDOWN * 2 ** (strikes - 1), config.CB_COOLDOWN_MAX)
    else:
        return
    with _cb_guard:
        _cb_until[provider] = time.monotonic() + cd
    _log_fail(f"cb|{provider}|{strikes}",
              f"{provider}: circuit-breaker AKTIF {cd}s"
              f"{f' (trip beruntun ke-{strikes})' if strikes > 1 else ''} "
              f"(kena limit/timeout, di-skip sementara)")


def _reset(provider: str) -> None:
    with _cb_guard:
        _cb_until.pop(provider, None)
        _cb_strikes.pop(provider, None)
        _cb_timeouts.pop(provider, None)   # "beruntun" harus putus oleh satu jawaban sukses


def cooldowns() -> dict:
    """Provider yang sedang cooldown + sisa detik (untuk panel debug)."""
    now = time.monotonic()
    with _cb_guard:
        return {p: round(u - now, 1) for p, u in _cb_until.items() if u > now}


def _client(base_url: str, api_key: str) -> OpenAI:
    key = f"{base_url}|{api_key}"
    if key not in _clients:
        # max_retries=0: kita tangani retry/backoff sendiri (lihat _create).
        _clients[key] = OpenAI(base_url=base_url, api_key=api_key,
                               max_retries=0, timeout=90.0)
    return _clients[key]


def chat_provider(provider: str, model: str, messages: list[dict], *,
                  want_json: bool = False, temperature: float = 0.6,
                  max_tokens: int = 1200, timeout: float = 60.0,
                  retries: int | None = None) -> dict:
    """Panggil SATU provider (OpenAI-compatible, mana saja). Lempar kalau gagal.
    `retries`=0 → gagal cepat (dipakai saat ada fallback: dewan / chain multi-provider)."""
    base = config.PROVIDER_BASE.get(provider)
    key = config.PROVIDER_KEY.get(provider, "")
    if not base or not key:
        raise RuntimeError(f"provider {provider}: base_url/key kosong")
    if _cooling(provider):
        raise _Cooling(f"{provider}: cooldown (baru kena limit)")  # skip tanpa panggil API
    client = _client(base, key)
    # Model "thinking" memakan token budget → jawaban/JSON kepotong. Matikan utk tugas ini:
    # gemini pakai reasoning_effort, GLM (4.5/4.7 hybrid-reasoning, thinking default NYALA)
    # pakai thinking.type — tanpa ini tesis GLM sering terpotong di max_tokens (2026-07-01).
    extra = None
    if provider == "gemini":
        extra = {"reasoning_effort": "none"}
    elif provider == "glm":
        extra = {"thinking": {"type": "disabled"}}
    kw = dict(model=model, messages=messages, temperature=temperature, top_p=0.95,
              max_tokens=max_tokens, extra_body=extra, timeout=timeout)
    # Paksa JSON valid di provider yg aman → hilangkan "JSON gagal parse" + hemat token reasoning.
    if want_json and provider in _JSON_OBJECT_OK:
        kw["response_format"] = {"type": "json_object"}
    try:
        resp = _create(client, provider, retries=retries, **kw)
    except Exception as e:  # noqa: BLE001 — trip breaker lalu lempar ke caller (chain/dewan)
        _trip(provider, e)
        raise
    _reset(provider)  # sukses → provider sehat, hapus cooldown
    msg = resp.choices[0].message
    content = msg.content or ""
    # model reasoning kadang taruh jawaban di reasoning_content & content kosong → jangan kosong.
    if not content.strip():
        content = getattr(msg, "reasoning", None) or getattr(msg, "reasoning_content", None) or ""
    out = {"content": content, "provider": provider, "model": model}
    if want_json:
        out["json"] = extract_json(content)
    _log_usage(provider, model, resp, content)
    return out


def _log_usage(provider: str, model: str, resp: Any, content: str) -> None:
    """Debug: catat token & output TIAP request LLM ke agent_logs + data/logs/engine.log."""
    u = getattr(resp, "usage", None)
    p_tok = getattr(u, "prompt_tokens", None) if u else None
    c_tok = getattr(u, "completion_tokens", None) if u else None
    t_tok = getattr(u, "total_tokens", None) if u else None
    repo.log("llm", "usage",
              f"{provider}/{model} tokens: prompt={p_tok} completion={c_tok} total={t_tok}",
              payload={"provider": provider, "model": model, "prompt_tokens": p_tok,
                       "completion_tokens": c_tok, "total_tokens": t_tok,
                       "mode": config.ENGINE_MODE,  # A/B agent-week: token dipisah per mode
                       "output": content})


def chat_chain(chain: list[tuple[str, str]], messages: list[dict], *,
               want_json: bool = False, **kw) -> dict:
    """Coba rantai (provider, model) berurutan → sukses PERTAMA. Untuk JSON, 'sukses'
    = JSON ter-parse. Lempar gabungan error kalau SEMUA gagal (CTO fallback minimax→cerebras→…)."""
    errors = []
    kw.setdefault("retries", 0)  # ada fallback berikutnya → tiap provider gagal CEPAT (tanpa backoff)
    for provider, model in chain:
        if not config.PROVIDER_KEY.get(provider):
            continue  # tak ada key → lewati diam-diam
        try:
            out = chat_provider(provider, model, messages, want_json=want_json, **kw)
            if want_json and out.get("json") is None:
                errors.append(f"{provider}/{model}: JSON gagal parse")
                continue
            return out
        except _Cooling:
            errors.append(f"{provider}/{model}: cooldown")  # di-skip diam-diam (bukan spam log)
            continue
        except Exception as e:  # noqa: BLE001
            err_short = str(e)[:70]
            errors.append(f"{provider}/{model}: {err_short}")
            _log_fail(f"chat|{provider}|{model}|{type(e).__name__}",
                      f"{provider}/{model} gagal: {err_short}")
            continue
    raise RuntimeError("semua provider gagal — " + " | ".join(errors))


def chat_chain_tools(chain: list[tuple[str, str]], messages: list[dict],
                     tools: list[dict], tool_impls: dict, *, max_rounds: int = 3,
                     temperature: float = 0.7, max_tokens: int = 2000,
                     timeout: float = 45.0, want_json: bool = False) -> dict:
    """Loop tool-calling lintas rantai fallback. `tools`=skema OpenAI function; `tool_impls`=
    dict name->fn(**args)->str. Model memanggil tool sesuai KEBUTUHAN; hasil disuntik balik lalu
    model lanjut sampai jawaban final / batas putaran. `want_json`=jawaban final harus JSON
    ter-parse (utk CTO) — gagal parse = coba provider berikutnya. Lempar bila SEMUA provider
    gagal (caller fallback ke jalur non-tool)."""
    errors = []
    for provider, model in chain:
        if not config.PROVIDER_KEY.get(provider):
            continue
        try:
            out = _tool_loop(provider, model, list(messages), tools, tool_impls,
                             max_rounds, temperature, max_tokens, timeout)
            if want_json:
                out["json"] = extract_json(out["content"])
                if out["json"] is None:
                    errors.append(f"{provider}/{model}: JSON gagal parse")
                    continue
            return out
        except _Cooling:
            errors.append(f"{provider}/{model}: cooldown")
            continue
        except Exception as e:  # noqa: BLE001
            errors.append(f"{provider}/{model}: {str(e)[:70]}")
            _log_fail(f"tool|{provider}|{model}|{type(e).__name__}",
                      f"{provider}/{model} tool gagal: {str(e)[:70]}")
            continue
    raise RuntimeError("semua provider tool gagal — " + " | ".join(errors))


def _tool_loop(provider: str, model: str, messages: list[dict], tools: list[dict],
               tool_impls: dict, max_rounds: int, temperature: float,
               max_tokens: int, timeout: float) -> dict:
    """Satu provider: minta model, jalankan tool_calls, ulang. Putaran terakhir dipaksa
    `tool_choice=none` → model WAJIB menjawab teks (tak menggantung di tool)."""
    base = config.PROVIDER_BASE.get(provider)
    key = config.PROVIDER_KEY.get(provider, "")
    if not base or not key:
        raise RuntimeError(f"provider {provider}: base_url/key kosong")
    if _cooling(provider):
        raise _Cooling(f"{provider}: cooldown")
    client = _client(base, key)
    calls = 0
    # JEJAK TOOL: apa yang benar-benar dilakukan agen (tool + argumen + potongan hasil).
    # Sebelumnya yang tersimpan cuma ANGKA panggilan, jadi log tak pernah bisa menjawab
    # "agen ini sedang mencari apa" — persis keluhan 'log agen tak jelas arahnya'.
    trace: list[dict] = []
    try:
        for i in range(max_rounds + 1):
            force = (i == max_rounds)  # putaran terakhir → paksa jawaban, matikan tool
            kw = dict(model=model, messages=messages, temperature=temperature, top_p=0.95,
                      max_tokens=max_tokens, timeout=timeout, tools=tools,
                      tool_choice=("none" if force else "auto"))
            resp = _create(client, provider, retries=0, **kw)
            msg = resp.choices[0].message
            _log_usage(provider, model, resp, msg.content or "")
            tcs = getattr(msg, "tool_calls", None)
            if not tcs:
                _reset(provider)
                content = msg.content or ""
                if not content.strip():
                    content = (getattr(msg, "reasoning", None)
                               or getattr(msg, "reasoning_content", None) or "")
                return {"content": content, "provider": provider, "model": model,
                        "tool_calls": calls, "trace": trace}
            # sisipkan pesan asisten (berisi tool_calls) lalu hasil tiap tool
            messages.append({"role": "assistant", "content": msg.content or None,
                             "tool_calls": [{"id": tc.id, "type": "function",
                                             "function": {"name": tc.function.name,
                                                          "arguments": tc.function.arguments}}
                                            for tc in tcs]})
            for tc in tcs:
                calls += 1
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:  # noqa: BLE001
                    args = {}
                fn = tool_impls.get(name)
                try:
                    result = fn(**args) if fn else f"(tool {name} tak dikenal)"
                except Exception as e:  # noqa: BLE001
                    result = f"(tool {name} error: {e})"
                trace.append({"tool": name, "args": args,
                              "hasil": " ".join(str(result).split())[:220]})
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": str(result)[:4000]})
        _reset(provider)  # pragma: no cover — force di atas mestinya sudah return
        return {"content": "", "provider": provider, "model": model,
                "tool_calls": calls, "trace": trace}
    except Exception as e:  # noqa: BLE001
        _trip(provider, e)
        raise


def _create(client: OpenAI, throttle_key: str, *, retries: int | None = None, **kw):
    """Panggil API dengan throttle per-provider + retry/backoff pada 429.
    `retries`=0 → gagal CEPAT (dipakai dewan & dalam chain: ada fallback, jangan tunggu backoff)."""
    n = config.LLM_MAX_RETRIES if retries is None else retries
    last_err = None
    for attempt in range(n + 1):
        _throttle(throttle_key)
        try:
            return client.chat.completions.create(**kw)
        except RateLimitError as e:  # 429 → tunggu lalu coba lagi
            last_err = e
            if attempt < n:
                time.sleep(config.LLM_RETRY_BACKOFF * (attempt + 1))
            else:
                raise
        except InternalServerError:
            # 5xx (gemini 503 high-demand, minimax DEGRADED): JANGAN retry di sini — semua
            # pemakai punya fallback chain / dewan toleran. Gagal CEPAT → pindah provider, bukan
            # tunggu backoff (dulu bikin 1 anggota 503 menyeret dewan jadi ~38s).
            raise
    raise last_err  # pragma: no cover


def extract_json(text: str) -> dict | None:
    """Tarik objek JSON dari teks (bisa diselubungi ```json ... ```)."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if not candidate:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        candidate = m.group(0) if m else None
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # buang trailing comma sederhana lalu coba lagi
        cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


# --- shortcut per-agen ---
def ask_analyst(messages: list[dict], **kw) -> dict:
    """Analis: tesis teks via RANTAI FALLBACK (GLM primary → Cerebras → Groq).
    GLM 429/limit → provider lain ambil alih, analis tak mati (dulu langsung jatuh ke heuristik)."""
    return chat_chain(config.ANALYST_CHAIN, messages, want_json=False,
                      temperature=0.7, max_tokens=2000, timeout=45.0)


def ask_trader_json(messages: list[dict], **kw) -> dict:
    """CTO: keputusan akhir JSON lewat RANTAI FALLBACK (minimax primary → cerebras → groq →
    openrouter). minimax-m3 sering DEGRADED → provider andal berikutnya ambil alih, bukan heuristik."""
    # timeout 30s (naik dari 15s, 11 Agt) — sama dgn jalur tool di trader.py biar A/B
    # agent-vs-pipeline tidak beda gara-gara batas waktu. PRIMARY kini deepseek-v4-flash-free:
    # diukur di VM live n=8 → median 13,7s, maks 19,7s, hanya 6/8 selesai di bawah 15s, jadi
    # batas lama membuang ~25% jawaban yang valid. minimax (urutan-3) yg >30s tetap ke-cut.
    return chat_chain(config.TRADER_CTO_CHAIN, messages, want_json=True,
                      temperature=0.6, max_tokens=1200, timeout=30.0)
