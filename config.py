"""Konfigurasi global untuk Saham IDX Prediction Engine."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
# DATA_DIR bisa dialihkan via env (real process env, sebelum load_dotenv) → INSTANCE TERISOLASI:
# sandbox $100k jalan di DB/dir sendiri, engine produksi di data/ tak tersentuh. Rollback =
# hapus foldernya. Default = data/ (produksi).
DATA_DIR = Path(os.getenv("DATA_DIR", "").strip() or (BASE_DIR / "data"))
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "prediction.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(_env(key, str(default))))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    return _env(key, str(default)).lower() in {"1", "true", "yes", "y", "on"}


# --- LLM / NVIDIA NIM ---
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_API_KEY_ANALYST = _env("NVIDIA_API_KEY_ANALYST")
NVIDIA_API_KEY_TRADER = _env("NVIDIA_API_KEY_TRADER")
# Z.ai / GLM (free tier ~1000 req/hari; model gratis: glm-4.5-flash / glm-4.7-flash)
GLM_BASE_URL = _env("GLM_BASE_URL", "https://api.z.ai/api/paas/v4")
GLM_API_KEY = _env("GLM_API_KEY")
# Google Gemini (endpoint OpenAI-compatible). Flash = cepat + pintar.
GEMINI_BASE_URL = _env("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-flash-latest")
# Mistral AI (endpoint OpenAI-compatible). Best model = mistral-large-latest.
MISTRAL_BASE_URL = _env("MISTRAL_BASE_URL", "https://api.mistral.ai/v1")
MISTRAL_API_KEY = _env("MISTRAL_API_KEY")
MISTRAL_MODEL = _env("MISTRAL_MODEL", "mistral-large-latest")
# GitHub Models DIHAPUS (audit 2026-08-10). Layanannya PENSIUN TOTAL 30 Juli 2026 — playground,
# katalog, inference API, BYOK semuanya mati untuk semua pelanggan (changelog GitHub 30 Jul).
# Buktinya di sistem ini: tiap panggilan balas 410 "github_models_retirement_brownout" dan
# GET /catalog/models mengembalikan 0 model, jadi TAK ADA id model pengganti yang menolong.
# Biayanya sebelum dicabut: 2.277 warn dalam 7 hari (40% dari SELURUH warn) + ~1 detik hangus
# tiap keputusan CTO, karena ia PRIMARY chain CTO dan ada di 6 slot. JANGAN ditambahkan lagi.
# GITHUB_MODELS_TOKEN di .env kini tak terpakai — hapus/revoke (lihat item terbuka 31 Jul).
# Model lokal di Colab (vLLM, GPU L4) — endpoint OpenAI-compatible lewat tunnel cloudflared.
# Kosong = provider ini dilewati diam-diam (chain jatuh ke cloud seperti biasa; lihat colab_local_model.ipynb).
COLAB_BASE_URL = _env("COLAB_BASE_URL")
COLAB_API_KEY = _env("COLAB_API_KEY")
COLAB_MODEL = _env("COLAB_MODEL", "Qwen/Qwen2.5-7B-Instruct")
# Default: kedua agen pakai MiniMax-M3 (cepat & andal). nemotron-3-ultra
# Analis = deepseek-v4-flash (cepat + reasoning). Trader = minimax-m3.
ANALYST_MODEL = _env("ANALYST_MODEL", "deepseek-ai/deepseek-v4-flash")
TRADER_MODEL = _env("TRADER_MODEL", "minimaxai/minimax-m3")
USE_LLM = _env_bool("USE_LLM", True)

# Anthropic Claude via endpoint OpenAI-compatible (base_url .../v1). Dipakai sebagai PRIMARY
# di semua chain keputusan saat USE_FABLE5 aktif.
# USE_FABLE5 default ON → fable-5 jadi PRIMARY di semua chain agen — TAPI hanya AKTIF kalau
# ANTHROPIC_API_KEY diisi. Tanpa key, chat_chain melewati provider ini diam-diam → perilaku
# persis seperti sekarang (chain free-tier lama). Jadi aman dinyalakan tanpa memutus apa pun.
# CATATAN JUJUR: fable-5 = LLM penalaran/sentimen; ia TIDAK memperbaiki kalibrasi 43% (itu dari
# skorer numerik model.py) dan menambah biaya API. Ortogonal terhadap win-rate.
ANTHROPIC_BASE_URL = _env("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
FABLE_MODEL = _env("FABLE_MODEL", "claude-fable-5")
USE_FABLE5 = _env_bool("USE_FABLE5", True)

# --- NaraRouter (gateway OpenAI-compatible, katalog 53 model per 11 Agt) ---
# Diukur DARI VM LIVE 2026-08-11, payload CTO produksi (temp 0,6 / top_p 0,95 / max_tokens 1200
# / response_format json_object):
#   nemotron-3-ultra       0/4 — MATI. Timeout di 20s DAN 30s (VM) & 40s (lokal), dgn 3 kunci
#     berbeda → bukan soal kunci/kuota, modelnya yang menggantung. Ia PRIMARY CTO sejak 10 Agt,
#     jadi tiap keputusan CTO hangus 20s sebelum jatuh ke fallback. Sebab penggantian ini.
#   deepseek-v4-flash      5/5 JSON valid, median 10,7s (8,4-11,8) — kunci berbayar
#   deepseek-v4-flash-free 5/5 JSON valid, median 10,7s (8,6-13,8) — kunci gratis, setara
#   gpt-5.6-luna           1/3 (2x "servers overloaded" balik HTTP 200 berisi teks error, bukan
#     JSON → chain menghitungnya gagal parse lalu lanjut). Cepat saat sehat (8,6s). Ekor evaluator.
#   minimax-m3 (via nara)  0/3 — membocorkan blok <think>, JSON tak pernah bersih. Jangan dipakai.
# Tool-calling (jalur AGENT_MODE): kedua deepseek-flash OK — tool_call argumen benar + jawaban
# final wajar, ~12-14s per ronde (2 ronde ~26s, di bawah timeout 45s). luna gagal (overload).
# TIGA KUNCI TERPISAH (user, 11 Agt) = tiga kuota + tiga breaker independen: satu kena 429 tidak
# menjatuhkan yang lain. Kunci saling bisa dipakai lintas model (dites), tapi dipasangkan sesuai
# label user. Gratis ditaruh DULU (biaya 0, latensi setara) → kunci berbayar jadi cadangan.
NARA_BASE_URL = _env("NARA_BASE_URL", "https://router.bynara.id/v1")
NARA_API_KEY = _env("NARA_API_KEY")
NARA_KEY_FREE = _env("NARA_KEY_FREE", NARA_API_KEY)    # kosong → pakai kunci utama (perilaku lama)
NARA_KEY_SMART = _env("NARA_KEY_SMART", NARA_API_KEY)
NARA_FAST = _env("NARA_FAST", "deepseek-v4-flash")
# 2026-08-24: id `deepseek-v4-flash-free` DICABUT dari katalog NaraRouter -> 404 "model does
# not exist" (516 warn/7 hari) padahal ia PRIMARY CTO. KUNCI gratisnya masih sah dan melayani
# model berbayar `deepseek-v4-flash` (diuji 4/4 JSON valid median 2,5s + tool-call OK), jadi
# yang diganti cuma id model. Cek katalog: GET {NARA_BASE_URL}/models.
NARA_FREE = _env("NARA_FREE", "deepseek-v4-flash")
NARA_SMART = _env("NARA_SMART", "gpt-5.6-luna")
# --- Dahl Inference (jaringan GPU terdesentralisasi Gonka) ---
# KETERSEDIAANNYA NAIK-TURUN. 2026-08-10 11:0x UTC: 6/6 gagal (M2.7 503 "devshard temporarily
# unavailable", K2.6 429). ~30 menit kemudian di jam yang sama: M2.7 PULIH dan jadi yang paling
# andal — 8/8 JSON valid dengan 8 prompt BERBEDA, median 9,53s (min 3,42 / maks 26,55).
# Median 9,5s itu sebabnya ia ditaruh di chain ANALIS (timeout 45s), BUKAN CTO (timeout 15s):
# dgn batas 15s hanya ~5/8 yang sempat selesai. Di ekor: praktis cuma dipakai kalau 6 provider
# di atasnya tumbang — yaitu memang perannya, asuransi. Catatan: hasil 0,19-0,80s pada prompt
# yang diulang itu CACHE sisi vendor, bukan kecepatan sebenarnya — jangan dipakai menilai.
DAHL_BASE_URL = _env("DAHL_BASE_URL", "https://inference.dahl.global/v1")
DAHL_API_KEY = _env("DAHL_API_KEY")
DAHL_MODEL = _env("DAHL_MODEL", "MiniMaxAI/MiniMax-M2.7")

# Backtest hanya di universe LIKUID (bisa ditradingkan), menyamakan populasi dgn tempat model
# dilatih & engine bertindak (audit 2026-07-21: overlap train-vs-backtest cuma 33%). Efek terukur:
# win 43,1%->45,4%, oos 42,7%->45,2% (h5) — LEBIH JUJUR (buang saham beku yg bukan sinyal), verdict
# tetap LEMAH (bukan klaim akurasi naik). Reversibel: BACKTEST_LIQUID_ONLY=false → perilaku lama.
BACKTEST_LIQUID_ONLY = _env_bool("BACKTEST_LIQUID_ONLY", True)

# --- DEWAN (council) multi-provider: minimax CTO + Cerebras/Groq/GLM ikut diskusi + fallback ---
# Semua OpenAI-compatible. Mengatasi minimax-m3 yang sering DEGRADED: kalau primary gagal,
# CTO turun ke provider andal berikutnya (tak langsung jatuh ke heuristik).
PROVIDER_BASE = {
    "nvidia": NVIDIA_BASE_URL,
    "glm": GLM_BASE_URL,
    "gemini": GEMINI_BASE_URL,
    "mistral": MISTRAL_BASE_URL,
    "groq": _env("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
    "cerebras": _env("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1"),
    "openrouter": _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    "colab": COLAB_BASE_URL,
    "anthropic": ANTHROPIC_BASE_URL,
    "nara": NARA_BASE_URL,
    "nara_free": NARA_BASE_URL,    # gateway sama, KUNCI beda → kuota & breaker terpisah
    "nara_smart": NARA_BASE_URL,
    "dahl": DAHL_BASE_URL,
}
PROVIDER_KEY = {
    "nvidia": NVIDIA_API_KEY_TRADER,
    "glm": GLM_API_KEY,
    "gemini": GEMINI_API_KEY,
    "mistral": MISTRAL_API_KEY,
    "groq": _env("GROQ_API_KEY"),
    "cerebras": _env("CEREBRAS_API_KEY"),
    "openrouter": _env("OPENROUTER_API_KEY"),
    "colab": COLAB_API_KEY,
    "anthropic": ANTHROPIC_API_KEY,
    "nara": NARA_API_KEY,
    "nara_free": NARA_KEY_FREE,
    "nara_smart": NARA_KEY_SMART,
    "dahl": DAHL_API_KEY,
}
# --- PETA PERAN -> PIN PRIMARY (anti role-collapse; bahan bab arsitektur tesis) ---
# DIUKUR ULANG 2026-08-24, 4 prompt BERBEDA per model (bukan satu prompt diulang - itu kena
# cache vendor dan angkanya bohong), payload CTO produksi + response_format=json_object:
#
#   provider/model                 JSON valid   median   tool-call
#   groq/openai/gpt-oss-120b          4/4        1,0s      OK 1,0s
#   groq/openai/gpt-oss-20b           4/4        0,8s      OK 0,6s
#   mistral/mistral-medium-latest     4/4        1,0s      OK 0,7s
#   mistral/mistral-large-latest      4/4        2,3s      OK 6,2s
#   nara_free/deepseek-v4-flash       4/4        2,5s      OK 3,3s
#   nara/deepseek-v4-flash            4/4        3,1s      OK 2,8s
#   openrouter/openai/gpt-oss-120b    4/4        4,4s      OK 3,9s
#   nara_smart/gpt-5.6-luna           4/4        5,0s      OK 5,7s
#   nvidia/minimaxai/minimax-m3       1/4       18,3s      -        -> dicabut dari chain
#   gemini/gemini-flash-latest        0/4       timeout    -        -> dicabut dari chain
#   dahl/MiniMaxAI/MiniMax-M2.7       bocor blok think, JSON tak pernah bersih -> dicabut
#
# EMPAT SLOT MATI DITEMUKAN 24 Agt (7.658 dari 10.464 warn 7 hari = 73% SELURUH warn):
#  1. groq/llama-3.3-70b-versatile -> 404, model PENSIUN. Katalog groq kini cuma gpt-oss-120b/
#     20b, qwen3.6-27b, compound. Ia menempati 6 slot di 5 chain + pin dewan teknikal ->
#     3.661 warn/7 hari. PENGGANTI: openai/gpt-oss-120b (model yang SAMA dengan yang dulu
#     disajikan cerebras, jadi prompt analis tak perlu disetel ulang).
#  2. cerebras/gpt-oss-120b -> 402 "Payment required to access this resource" pada KEDUA model
#     di katalognya (gpt-oss-120b & gemma-4-31b) -> kunci ini tak lagi punya akses gratis.
#     3.228 warn/7 hari, dan ia PIN PRIMARY ANALIS + primary jalur tool analis.
#  3. glm/glm-4.5-flash -> tak ada lagi di katalog Z.ai (kini glm-4.5/4.6/4.7/5/5.x); panggilan
#     menggantung sampai timeout 41s lalu balas KOSONG (201 warn "Request timed out"), dan
#     model yang ADA di katalog balas 429 "Insufficient balance or no resource package".
#     Ia PIN PRIMARY SENTIMEN + pin dewan sentimen.
#  4. nara_free/deepseek-v4-flash-free -> 404 "model does not exist" (516 warn). Model -free
#     dicabut dari katalog NaraRouter; KUNCI-nya masih sah dan melayani `deepseek-v4-flash`
#     (diuji: 4/4 JSON + tool-call OK) -> cukup ganti id model, JANGAN cabut providernya.
#     Ia PIN PRIMARY CTO + primary jalur tool CTO.
#  Tambahan: openrouter/openai/gpt-oss-20b:free -> 404 "unavailable for free" (80 warn).
#
# Akibat gabungannya bukan kosmetik: 24 Agt tercatat 626 baris "semua provider gagal" dan 278
# "jalur tool gagal" dalam SEHARI -> dewan tinggal 1 dari 3 anggota hidup dan mayoritas siklus
# jatuh ke heuristik tanpa LLM. Itu bukan arsitektur multi-agen, itu satu skorer numerik.
#
#   Analis            groq / openai/gpt-oss-120b       (dulu cerebras - 402)
#   Trader/CTO        nara_free / deepseek-v4-flash    (dulu ...-free - 404, hanya id yg ganti)
#   Dewan teknikal    openrouter / openai/gpt-oss-120b (dulu groq - 404)
#   Dewan kontrarian  nara / deepseek-v4-flash         (dulu gemini - 0/4)
#   Agen Sentimen     nara_smart / gpt-5.6-luna        (dulu glm - saldo habis)
#   Evaluator         mistral / mistral-large-latest   (tetap, 4/4 sehat)
#   Risk Agent        - (rule-based, 0 token)
#
# === GELOMBANG PEMBUSUKAN KE-4, DIUKUR DARI VM LIVE 2026-09-02 ===
# Empat dari enam pin peran MATI lagi; 11.518 dari 27.925 log 7 hari (41%) cuma warn provider,
# dan panel log praktis kosong dari suara agen. Sebab, satu per satu:
#   groq/*                        401 "expired_api_key" -> KUNCI kedaluwarsa (bukan modelnya).
#                                 Ia pin PRIMARY ANALIS + primary jalur tool analis.
#   openrouter/openai/gpt-oss-120b 402 "never purchased credits" -> pin dewan teknikal.
#   mistral/mistral-large-latest  403 "model is not available" -> pin PRIMARY EVALUATOR (0/4).
#   glm/*                         429 saldo habis; cerebras/* 402 payment required.
#   nara*/deepseek-v4-flash       JSON 4/4 @2,5s TAPI TOOL-CALL MENGGANTUNG -> timeout 45s.
#                                 Ia primary TRADER_TOOL_CHAIN, jadi tiap keputusan CTO mode
#                                 agen hangus 45s sebelum jatuh. Boleh dipakai di chain NON-tool
#                                 saja. Ini penyebab "keputusan lambat + log agen kosong".
#
# Pengganti diukur 4 prompt BERBEDA (anti cache vendor) + 1 uji tool-call, payload CTO produksi
# + response_format=json_object, DUA kali (paralel & serial) dari VM live:
#   provider/model                      JSON   median  tool-call
#   mistral/mistral-medium-latest       4/4+4/4  0,8s   OK 1,7s   <- paling andal & cepat
#   mistral/mistral-small-latest        4/4      0,9s   OK 1,9s
#   openrouter/minimax/minimax-m3:free  4/4+4/4  1,8s   429 (tool) <- chat saja, kuota free 50/hari
#   nara_smart/gpt-5.6-luna             4/4      3,8s   OK 5,8s
#   nara_smart/gpt-5.6-sol              4/4+4/4  4,9s   OK 7,2s
#   gemini/gemini-flash-latest          2/4+3/4  5,7s   400 (skema tool) <- chat saja, kadang 503
#   nara/qwen3.7-flash                  4/4      6,7s   OK 8,0s
#   nara_free/minimax-m3-free           4/4      7,1s   OK 7,5s
#   nara_free/qwen3.8-flash-free        4/4      7,2s   OK 26,8s
#   nara/kimi-k3                        4/4+3/4 11,1s   OK 9,3s   <- penalaran terkuat yg hidup
#   nara_smart/qwen3.8-max              4/4     11,3s   OK 7,4s
#   nara_free/glm-5.3-flash-free        4/4     16,4s   OK 17,7s
#   openrouter/nemotron-3-super:free    2/4+3/4  -      balas {"":""} -> DICABUT
#   nvidia/minimaxai/minimax-m3         2/4+2/4  -      429 berulang -> DICABUT
#
# PENTING — ANGKA DI TABEL ATAS DIUKUR DENGAN PROMPT PENDEK DAN TERNYATA MENYESATKAN.
# E2E live 2026-09-02 membuktikannya: kimi-k3 tool-call 9,3s di uji mainan, tapi TIMEOUT 45s
# dengan prompt analis SUNGGUHAN (23.817 char / ~6k token + 9 skema tool). Pengukuran ulang
# memakai prompt produksi apa adanya:
#
#   JALUR TOOL ANALIS (prompt 23.817 char, 9 tool, 4 ronde, batas 45s)
#   provider/model                 waktu   tool dipakai   panjang tesis
#   nara_smart/gpt-5.6-luna        42,6s   4              3.910 char   <- SATU-SATUNYA yg benar
#   nara/qwen3.7-flash             42,1s   1              3.471 char      benar memakai tool
#   mistral/mistral-medium-latest  16,2s   0              5.672 char   <- cepat, tapi TAK PERNAH
#   mistral/mistral-small-latest   15,8s   0              5.623 char      menyentuh tool
#   nara/kimi-k3                   45,1s   TIMEOUT
#   nara_smart/qwen3.8-max         45,1s   TIMEOUT
#   nara_free/minimax-m3-free       7,0s   502 Cloudflare
#   nara_free/qwen3.8-flash-free    1,5s   400 "model tak mendukung tool"
#
#   VOTE DEWAN (brief 1.800 char, batas 15s)
#   mistral-medium 0,9s | mistral-small 0,8s | nara_smart/gpt-5.6-sol 3,9s |
#   nara_smart/gpt-5.6-luna 3,9s | openrouter/minimax-m3:free 1,5s |
#   nara_smart/qwen3.8-max 13,3s | nara/qwen3.7-flash 12,9s (mepet) |
#   nara_free/minimax-m3-free 502 | nara/kimi-k3 TIMEOUT | gemini 429 kuota habis
#
# Konsekuensi yang menentukan pin: model tercepat justru MENGABAIKAN tool. Memilih mistral
# sebagai analis = mengembalikan pipeline inject-all dgn nama agen — kebalikan dari yang
# diminta. Jadi analis dipegang model yang MEMANG memakai tool, dan batas waktunya yang
# dinaikkan (ANALYST_TOOL_TIMEOUT), bukan tool-nya yang dibuang.
#
# REVISI SETELAH 3,5 JAM LIVE (1.213 baris log, 60 tesis): luna dan qwen tertukar perannya.
# gpt-5.6-luna MULUS untuk panggilan pendek (53 sukses, vote dewan 3,9s) tapi loop tool
# multi-ronde-nya kena 502 Cloudflare di gateway 5x lalu breaker-nya trip; qwen3.7-flash justru
# MENYELESAIKAN 60 dari 60 tesis (96 sukses) dengan 2-4 tool-call. Ditukar: qwen jadi analis,
# luna jadi kursi kontrarian. Bonus, ini juga melepas jalur nara yang tadinya diperebutkan
# analis & kursi dewan (7 dari 10 kursi kosong berbunyi "nara: cooldown").
#
#   PIN 2026-09-02   Analis            nara / qwen3.7-flash           (60/60 tesis, 2-4 tool-call)
#                    Trader/CTO        mistral / mistral-medium-latest (0,9s)
#                    Dewan teknikal    openrouter / minimax-m3:free   (1,5s)
#                    Dewan kontrarian  nara_smart / gpt-5.6-luna      (3,9s)
#                    Agen Sentimen     nara_free / minimax-m3-free    (lexicon tetap lapis dasar)
#                    Evaluator         gemini / gemini-flash-latest   (kuota gemini HABIS hari ini
#                                      -> jatuh ke lapis-2; dipilih justru karena perannya cuma
#                                      1 panggilan/hari, jadi kuota harian yang sempit cukup)
#                    Risk Agent        - (rule-based, 0 token)
# Hanya LIMA provider yang benar-benar sehat (mistral, nara, nara_free, nara_smart, openrouter)
# untuk ENAM peran, jadi peran paling jarang dipanggil sengaja mendapat provider paling rapuh.
# PRIMARY tiap peran DISTINCT -> kondisi normal "banyak agen" bukan 1 model ganti topeng.
# Uji: test_council.test_role_pins_distinct.
#
# CARA MEMBANGKITKAN LAGI provider yang mati: cukup isi kunci barunya di .env — groq/cerebras/
# glm/nvidia SENGAJA tetap terdaftar di PROVIDER_BASE/PROVIDER_KEY, hanya DICABUT dari chain.
GROQ_OSS = _env("GROQ_OSS", "openai/gpt-oss-120b")        # kunci 401 kedaluwarsa - di luar chain
GROQ_OSS_MINI = _env("GROQ_OSS_MINI", "openai/gpt-oss-20b")
OR_OSS = _env("OR_OSS", "openai/gpt-oss-120b")            # 402 tanpa kredit - di luar chain
# --- model yang TERUKUR HIDUP 2026-09-02 (dipakai chain di bawah) ---
MISTRAL_MED = _env("MISTRAL_MED", "mistral-medium-latest")
MISTRAL_SMALL = _env("MISTRAL_SMALL", "mistral-small-latest")
OR_FREE = _env("OR_FREE", "minimax/minimax-m3:free")
GEMINI_FLASH = _env("GEMINI_FLASH", "gemini-flash-latest")
NARA_KIMI = _env("NARA_KIMI", "kimi-k3")
NARA_QWEN = _env("NARA_QWEN", "qwen3.7-flash")
NARA_SOL = _env("NARA_SOL", "gpt-5.6-sol")
NARA_QWEN_MAX = _env("NARA_QWEN_MAX", "qwen3.8-max")
NARA_MINIMAX_FREE = _env("NARA_MINIMAX_FREE", "minimax-m3-free")
NARA_GLM_FREE = _env("NARA_GLM_FREE", "glm-5.3-flash-free")
# CTO = pembuat keputusan akhir (JSON). Primary mistral-medium: 8/8 JSON valid di dua lari
# terpisah, median 0,8s, tool-call 1,7s — tercepat DAN terandal dari seluruh kandidat hidup.
# deepseek-v4-flash turun ke lapis-2 (JSON-nya sehat, hanya tool-call-nya yang menggantung).
TRADER_CTO_CHAIN = [
    ("mistral", MISTRAL_MED),
    ("nara", NARA_FAST),            # deepseek-v4-flash: JSON 4/4 @2,5s (jalur NON-tool saja)
    ("nara_smart", NARA_SMART),
    ("openrouter", OR_FREE),
    ("mistral", MISTRAL_SMALL),
]
# ANALIS = penyusun tesis (teks, timeout 45s → boleh model yang lebih lambat tapi lebih dalam).
# Primary kimi-k3: penalaran terkuat di antara yang hidup, median 11,1s, tool-call 9,3s.
ANALYST_CHAIN = [
    ("nara", NARA_QWEN),
    ("nara_smart", NARA_SMART),
    ("mistral", MISTRAL_MED),       # 16,2s, tesis 5.672 char - cadangan paling andal
    ("openrouter", OR_FREE),
    ("nara_free", NARA_GLM_FREE),
]
# Anggota DEWAN (selain CTO) - tiap anggota LENSA berbeda (bukan N voter identik). Format:
# (peran, provider, model). Jumlah anggota tetap 2 LLM + 1 reuse sentimen = token setara.
COUNCIL_MEMBERS = [
    ("teknikal", "openrouter", OR_FREE),
    ("kontrarian", "nara_smart", NARA_SMART),
    # Suara sentimen = reuse cache agen sentimen (council memanggil sentiment.council_vote,
    # BUKAN vote LLM baru) -> 0 token marginal; provider di sini cuma label pin.
    ("sentimen", "gemini", GEMINI_FLASH),
]
USE_COUNCIL = _env_bool("USE_COUNCIL", True)

# --- AGEN SENTIMEN (anggota inti MAS - "berbasis analisis sentimen") ---
# Dua lapis: lexicon (0 token, selalu) + LLM hanya saat berita emiten < SENT_FRESH_H jam
# dan kunci berita berubah (memo). Pin PRIMARY = nara_smart (distinct dari analis/CTO/dewan).
SENTIMENT_AGENT = _env_bool("SENTIMENT_AGENT", True)
SENT_FRESH_H = _env_int("SENT_FRESH_H", 12)
# Pin di sini membusuk diam-diam: model ditarik gateway, kuota habis, kunci dicabut. Kalau
# ketiganya mati, agen sentimen jatuh ke leksikon tanpa tanda apa pun. Periksa berkala dengan
# tools/provider_audit.py, dan jaga provider tiap slot tetap berbeda dari chain peran lain.
SENTIMENT_CHAIN = [
    ("nara_free", NARA_GLM_FREE),   # diukur: JSON ok 7,8s (minimax-m3-free = 404)
    ("mistral", MISTRAL_SMALL),  # 0,8s - sentimen dipanggil sering (~62x/hari), model kecil
    ("nara", NARA_QWEN),            # diukur: JSON ok 6,7s (deepseek-v4-flash JSON rusak)
]

# --- AGEN EVALUATOR (evaluasi otonom): 1 panggilan LLM/hari pasca-laporan ---
# Menyuling scoreboard faktor + lessons 7 hari + kalibrasi -> maks AUTO_RULES_MAX aturan
# dinamis (lessons kind='auto-rule', batch harian menggantikan batch lama). ADVISORY only:
# limit angka keras (MAX_ALLOC, stop-loss, veto) tetap di kode. Pin PRIMARY = mistral (distinct).
AUTO_TUNE = _env_bool("AUTO_TUNE", True)
AUTO_RULES_MAX = _env_int("AUTO_RULES_MAX", 10)
EVALUATOR_CHAIN = [
    ("gemini", GEMINI_FLASH),  # 1 panggilan/hari → kuota gemini yang sempit justru cukup
    ("mistral", MISTRAL_MED),
    ("nara_smart", NARA_SMART),
]

# --- AGENT MODE: minggu multi-agent (A/B vs pipeline inject-all) ---
# Master switch. true = agen memutuskan sendiri: analis menarik data via tool on-demand,
# CTO memutuskan sendiri kapan konsultasi dewan (bukan trigger skrip `if ragu`). TRADE-OFF
# JUJUR: tiap tool-call = 1 round-trip -> system prompt dikirim ulang -> hemat token HANYA jika
# tool jarang dipanggil. Perbandingan: python compare_modes.py (win-rate + token per mode;
# prediksi di-tag factors.engine_mode, usage di-tag payload.mode).
AGENT_MODE = _env_bool("AGENT_MODE", True)   # default ON (kill-switch: AGENT_MODE=false di .env)
ANALYST_TOOLS = _env_bool("ANALYST_TOOLS", AGENT_MODE)  # override granular bila perlu
TRADER_TOOLS = _env_bool("TRADER_TOOLS", AGENT_MODE)
ENGINE_MODE = "agent" if AGENT_MODE else "pipeline"     # tag utk prediksi & usage log
# Primary WAJIB = primary chain pipeline padanannya -> beda hasil A/B = ARSITEKTUR, bukan model.
ANALYST_TOOL_CHAIN = [
    ("nara", NARA_QWEN),            # WAJIB sama dgn primary ANALYST_CHAIN (anti-confound A/B)
    ("nara_smart", NARA_SMART),     # loop tool-nya kena 502 gateway - lapis-2 saja
    ("mistral", MISTRAL_MED),       # 0 tool-call: jaring pengaman, bukan pilihan
    # kimi-k3 & qwen3.8-max SENGAJA di luar: keduanya TIMEOUT 45s dgn prompt analis nyata.
]
# Jalur tool analis butuh lebih dari 45s: model yang benar-benar MEMAKAI tool (gpt-5.6-luna)
# selesai di 42,6s, tanpa margin sama sekali. Menurunkan batas = memaksa engine memilih model
# yang mengabaikan tool. Jalur NON-tool tetap 45s (llm.ask_analyst).
ANALYST_TOOL_TIMEOUT = float(_env_int("ANALYST_TOOL_TIMEOUT", 75))
# Vote dewan berjalan PARALEL antar kursi, jadi biayanya = kursi TERLAMBAT, bukan jumlahnya.
# 15s memotong nara/qwen3.7-flash yang butuh 12,9s (tak ada margin); 20s memberi ruang.
COUNCIL_VOTE_TIMEOUT = float(_env_int("COUNCIL_VOTE_TIMEOUT", 20))
# 4 putaran (naik dari 3): dgn tool screen_market + track_record yang baru, agen butuh ronde
# ekstra utk (1) menyaring pasar, (2) memeriksa kandidat temuannya, (3) menguji rekam jejaknya,
# (4) menulis tesis. Batas tetap ada — putaran terakhir tool_choice=none, tak bisa loop abadi.
TOOL_MAX_ROUNDS = _env_int("TOOL_MAX_ROUNDS", 4)  # batas putaran tool (cegah loop tak berujung)
AGENT_SUGGEST_MAX = _env_int("AGENT_SUGGEST_MAX", 4)  # maks saran agen per siklus (pagar token)
# Hasil screen_market: batas baris supaya satu panggilan tool tak meledakkan prompt.
SCREEN_MAX_HITS = _env_int("SCREEN_MAX_HITS", 12)
# Orkestrasi MAS penuh via StateGraph LangGraph (analis -> sentimen -> debat -> risk gate).
# Node membungkus fungsi agen yang sama (bisa dirender: python -m app.agents.graph).
# Graf gagal -> jalur manual _decide_for -> heuristik (3 lapis, zero-risk sistem uang).
USE_LANGGRAPH = _env_bool("USE_LANGGRAPH", True)
# --- TANPA FALLBACK DIAM-DIAM (permintaan user 2026-09-02) ---
# Dulu: semua provider LLM mati -> heuristic.decide() mengisi tempatnya dan prediksinya masuk
# papan skor TANPA tanda apa pun. Efeknya persis yang dikeluhkan: log agen terlihat kosong
# padahal sistem "jalan" — yang jalan cuma skorer numerik memakai nama agen.
# STRICT=True: kalau analis DAN CTO sama-sama gagal, ticker itu DILEWATI (tak ada prediksi,
# tak ada trade) dan alasannya dicatat level error. Heuristik tetap dipakai di tempat yang
# memang jujur menyebut dirinya heuristik (_heuristic_commit hasil scan) dan saat USE_LLM=false.
# RISIKO: kalau semua provider tumbang, engine berhenti mengambil keputusan ber-LLM (sengaja —
# lebih baik diam daripada mengaku agen). Kill-switch: LLM_STRICT=false di .env → perilaku lama.
LLM_STRICT = _env_bool("LLM_STRICT", True)
# Jendela peredam log kegagalan provider (llm._log_fail). Kegagalan PERTAMA tiap
# (provider, model, jenis error) dicatat utuh; identik berikutnya dihitung lalu diringkas
# satu baris tiap jendela ini. 0 = redam mati (semua dicatat, perilaku lama).
LOG_DEDUPE_SEC = _env_int("LOG_DEDUPE_SEC", 300)
TRADER_TOOL_CHAIN = [
    ("mistral", MISTRAL_MED),   # WAJIB sama dgn primary TRADER_CTO_CHAIN (anti-confound A/B)
    ("nara_smart", NARA_SMART),
    ("nara", NARA_QWEN),
    # deepseek-v4-flash SENGAJA TIDAK di sini: JSON-nya 4/4 tapi tool-call-nya menggantung
    # sampai timeout 45s (diukur 2x, VM live 2026-09-02). Chain non-tool masih memakainya.
]


def _fable_first(chain: list) -> list:
    """Prepend fable-5 sebagai PRIMARY di chain keputusan. Aktif hanya bila
    USE_FABLE5 & key ada; kalau tidak, chain tak berubah → fallback lama tetap jalan.
    Fungsi (bukan literal) agar 1 sumber kebenaran + gampang dibalik (USE_FABLE5=false)."""
    if USE_FABLE5 and ANTHROPIC_API_KEY:
        return [("anthropic", FABLE_MODEL)] + chain
    return chain


# Terapkan ke semua chain KEPUTUSAN (2-tuple). COUNCIL_MEMBERS (role,provider,model) SENGAJA
# tak disentuh: pin peran distinct-nya diuji test_council.test_role_pins_distinct; fable-5
# sudah memimpin analis+CTO+sentimen yang menyusun & memutus, jadi "for all" tercakup.
TRADER_CTO_CHAIN = _fable_first(TRADER_CTO_CHAIN)
ANALYST_CHAIN = _fable_first(ANALYST_CHAIN)
SENTIMENT_CHAIN = _fable_first(SENTIMENT_CHAIN)
EVALUATOR_CHAIN = _fable_first(EVALUATOR_CHAIN)
ANALYST_TOOL_CHAIN = _fable_first(ANALYST_TOOL_CHAIN)
TRADER_TOOL_CHAIN = _fable_first(TRADER_TOOL_CHAIN)

# --- ABLASI BAYANGAN (app.agents.shadow): heuristik vs LLM-tunggal vs multi-agent ---
# Tiga lengan menilai kandidat, tanggal, harga masuk, dan horizon yang SAMA. Dipisah dari
# jalur produksi: tak ada lengan yang menyentuh portofolio atau gerbang.
# SHADOW_LLM = "provider:model" DIPATOK (bukan chain). Rantai fallback sengaja TAK dipakai:
# rantai itulah yang membuat treatment tidak stabil (36 pasangan provider/model benar-benar
# terpanggil dalam 8 pekan). Provider mati → lengan ini kosong untuk batch itu, dan lubangnya
# terlihat di data, bukan ditambal model lain.
#
# Kenapa BUKAN mistral-medium (primary CTO), yang secara ilmiah paling rapi karena menyisakan
# KOORDINASI sebagai satu-satunya beda: diukur 2026-09-06 dari mesin lokal, kuncinya sudah
# 429 rate-limited karena dipakai CTO produksi tiap siklus. Lengan bayangan yang memakai
# kuota yang sama akan (a) mengosongkan dirinya sendiri dan (b) menggerogoti jatah CTO.
# gpt-5.6-luna dipilih karena antrean kuncinya terpisah dan terbukti membalas JSON valid
# (11,2s, uji 2026-09-06). Identitas model tersimpan per baris (`meta_json`, dan `cto_model`
# pada lengan mas), jadi analisis tetap bisa menyaring ke batch yang CTO-nya model tertentu.
SHADOW_ARMS = _env_bool("SHADOW_ARMS", True)
SHADOW_LLM = _env("SHADOW_LLM", "nara_smart:" + NARA_SMART)

# Tujuan push arus asing residensial (tools/push_idxflow.py). Kosong = skrip menolak jalan.
# Dipisah ke env supaya alamat server tak ikut ter-commit saat repo dibagikan.
PUSH_HOST = _env("PUSH_HOST")            # mis. ubuntu@10.0.0.1
PUSH_KEY = _env("PUSH_KEY")              # path kunci SSH; kosong = .ssh/id_ed25519 di root repo
PUSH_REMOTE = _env("PUSH_REMOTE", "~/Prediction/data/foreign_flow.json")

# --- Trading / engine ---
START_CASH = float(_env_int("START_CASH", 100_000_000))
# Biaya transaksi khas broker ritel IDX
FEE_BUY = 0.0015   # 0.15%
FEE_SELL = 0.0025  # 0.25% (sudah termasuk pajak jual)
MAX_POSITIONS = 8           # maksimum saham yang dipegang bersamaan
MAX_ALLOC_PER_TRADE = 0.15  # maksimum 15% modal per posisi
MIN_PRICE = float(_env_int("MIN_PRICE", 50))  # abaikan saham < ini (gocap/junk)
# LIKUIDITAS — saham yang nyaris tak diperdagangkan TIDAK BOLEH masuk analisis, training,
# maupun evaluasi. Audit 2026-07-20: 52% sampel historis ber-turnover < Rp1M/hari dan 11%
# ber-return ke depan PERSIS 0 (saham beku). Keduanya (a) mencemari label training model,
# (b) menciptakan "edge" palsu — desil-atas model tampak +1,95%/5hr, tapi itu datang dari
# 87 sampel ekor (BEEF +106%, BUKK +103%, BABY +96%) ber-turnover Rp0,07-2,59 juta/hari
# yang TAK BISA dibeli dalam ukuran berarti; buang ekor itu → -1,62%.
# Gate: FRAKSI SESI (bukan rata-rata turnover) — minimal MIN_LIQUID_FRAC dari sesi dalam
# LIQUID_WINDOW_DAYS terakhir harus ber-turnover (close x volume) >= MIN_TURNOVER. Pakai
# fraksi supaya 1 hari ramai (pom-pom/ARA) tak meloloskan saham yang sisa harinya beku.
# Konsekuensi yang DISENGAJA: listing baru butuh >= 5 sesi sebelum lolos, jadi IPO hari 1-4
# tak masuk scanner otomatis (analisis per-ticker eksplisit dari UI tetap jalan). Ini
# menabrak catatan stocks.py:91-93 yang dulu sengaja memasukkan IPO baru — diputuskan
# ulang 2026-07-20: saham tanpa riwayat likuiditas memang tak punya bukti bisa dieksekusi,
# dan jendela ARA/ARB-nya justru lotere yang audit ini tunjukkan sebagai sumber edge palsu.
MIN_TURNOVER = float(_env_int("MIN_TURNOVER", 1_000_000_000))  # Rp1 miliar/hari
LIQUID_WINDOW_DAYS = _env_int("LIQUID_WINDOW_DAYS", 30)
MIN_LIQUID_FRAC = float(os.getenv("MIN_LIQUID_FRAC", "") or 0.5)
# Riwayat minimum sebuah listing sebelum boleh masuk scanner (sesi tersimpan di tabel prices).
# Terpisah dari gate turnover: yang itu menanyakan "ramai?", ini menanyakan "sudah punya
# rentang harga yang bisa dinilai?". Lihat repo.liquid_tickers untuk kasus JELI.
MIN_LIQUID_HISTORY = _env_int("MIN_LIQUID_HISTORY", 30)
# Exit: jaring pengaman statik (agen tetap boleh jual lebih awal). Backstop biar tak nyangkut.
STOP_LOSS_PCT = float(_env_int("STOP_LOSS_PCT", -10))     # auto-jual jika rugi <= ini
TAKE_PROFIT_PCT = float(_env_int("TAKE_PROFIT_PCT", 20))  # auto-jual jika untung >= ini
MAX_HOLD_DAYS = _env_int("MAX_HOLD_DAYS", 15)             # auto-jual jika dipegang > ini (risiko kelamaan)
# Anti-churn: setelah JUAL ticker, larang BELI lagi selama ini (cegah whipsaw bayar fee 2×).
REENTRY_COOLDOWN_HOURS = _env_int("REENTRY_COOLDOWN_HOURS", 6)
# Anti-churn sisi jual: posisi < ini jam TAK boleh dijual di zona P/L noise (-3%..+5%).
# Kasus BBRI 2026-07-08: beli lalu jual 1 jam kemudian -2.4%+fee = -Rp284rb. Stop/TP besar lolos.
MIN_HOLD_H = _env_int("MIN_HOLD_H", 18)  # ~1 hari bursa: cegah flip-jual intraday (forensik
# 2026-07-13: 85% posisi dijual <24 jam → fee Rp4.7jt biang rugi; naikkan 4→18 = jual diskresioner
# baru boleh sesi berikutnya. Stop-loss/take-profit (check_exits force=True) tetap jalan seketika.
# Edge minimum BUY vs biaya: fee round-trip ~0.4% → BUY ber-ekspektasi tipis kalah oleh fee.
# Forensik 2026-07-07 (87 round-trip): exp<2% = 21% win PnL -Rp1.36jt; exp>=2% = 39% win +Rp1.24jt.
MIN_EDGE_PCT = float(_env("MIN_EDGE_PCT", "2.0"))

# Edge minimum (pp DI ATAS base rate pasar) sebelum keyakinan boleh memicu aksi. Menggantikan
# lantai MUTLAK 60/66 yang dipakai sejak awal. Lantai mutlak itu ditetapkan waktu `probability`
# masih keluaran formula `50 + |score|*9`; setelah skill.calibrated_probability membuatnya
# berarti peluang menang SEBENARNYA, menuntut 60 sama dengan menuntut edge +19 pp di atas base
# rate UP h=3 (40,9%) — mustahil, dan terbukti mematikan program beli 5 hari bursa (24-31 Agt
# 2026, portofolio 100% kas). Lihat app.eval.edge_floor. Selisih risk-on/risk-off dipertahankan
# 6 pp, persis seperti pasangan 60/66 yang lama.
# ponytail: dua konstanta, bukan tabel per-gaya. Turunkan HANYA setelah lengan yang bersangkutan
# lolos app.eval.verdict_winrate(side="above") — menurunkannya tanpa itu = melonggarkan rem.
MIN_EDGE_PP = float(_env("MIN_EDGE_PP", "10.0"))
MIN_EDGE_PP_RISKOFF = float(_env("MIN_EDGE_PP_RISKOFF", "16.0"))

# --- Universe ---
USE_FULL_UNIVERSE = _env_bool("USE_FULL_UNIVERSE", True)
UNIVERSE_FILE = DATA_DIR / "idx_universe.txt"  # daftar ticker (1 per baris) override

# --- FOKUS & GAYA TRADING ---
# FOCUS_TICKERS="BBCA:invest,BBRI:swing,ANTM:scalp" → saham prioritas: dianalisis LLM tiap
# siklus (di depan hasil scan) dengan MANDAT gaya. Gaya → horizon (hari bursa) + instruksi:
#   scalp  = 1 hari, momentum intraday, target kecil, keluar cepat
#   swing  = 5 hari, tren beberapa hari (default kalau gaya tak ditulis)
#   invest = 20 hari, bobot FUNDAMENTAL (valuasi/ROE/growth/dividen) di atas teknikal
# Gaya fokus -> horizon dalam SESI BURSA. "invest" dinaikkan 10 -> 20 (1 bulan penuh) dan
# tiga gaya panjang ditambahkan; base rate tiap horizon ada di app.eval.BASELINE_WINRATE
# (baris 60/120/250 masih tipis, lihat peringatan di sana).
FOCUS_STYLES = {"scalp": 1, "swing": 5, "invest": 20,
                "kuartal": 60, "semester": 120, "tahunan": 250}  # invest maks 10 hari bursa (pasar ID tak terduga)
# Throttle re-analisis LLM per gaya (menit) — fokus ≠ bakar token tiap 12 menit.
FOCUS_REFRESH_MIN = {"scalp": 45, "swing": 180, "invest": 720}


def _parse_focus(raw: str) -> dict[str, str]:
    """'BBCA:invest, bbri, ANTM:scalp' → {'BBCA':'invest','BBRI':'swing','ANTM':'scalp'}.
    Gaya tak dikenal/kosong → 'swing'. Entri tak valid dilewati diam-diam."""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        ticker, _, style = part.partition(":")
        ticker = ticker.strip().upper()
        style = style.strip().lower() or "swing"
        if len(ticker) < 3 or not ticker.isalpha():
            continue
        out[ticker] = style if style in FOCUS_STYLES else "swing"
    return out


FOCUS = _parse_focus(_env("FOCUS_TICKERS"))

# --- Scheduler (menit) ---
PRICE_REFRESH_MIN = _env_int("PRICE_REFRESH_MIN", 5)
# Price-guard (app/data/price_guard.py): loop harga cepat TANPA LLM di thread terpisah.
# Target jarak antar sweep saat bursa buka (detik). 20s utk ~700 ticker Yahoo gratis =
# TARGET, bukan jaminan: saat throttle, backoff otomatis melebarkan interval lalu
# menyempit lagi. Bursa tutup (harga beku) → sweep jarang (PRICE_REFRESH_CLOSED_SEC).
PRICE_REFRESH_SEC = _env_int("PRICE_REFRESH_SEC", 20)
PRICE_REFRESH_CLOSED_SEC = _env_int("PRICE_REFRESH_CLOSED_SEC", 600)
# Ticker tanpa harga segar >= ini sweep beruntun dianggap BASI → log warn price-guard
# (harga last-known tetap disajikan, tidak pernah dikosongkan).
PRICE_STALE_SWEEPS = _env_int("PRICE_STALE_SWEEPS", 15)
HOT_REFRESH_MIN = _env_int("HOT_REFRESH_MIN", 2)   # hot-set (posisi/prediksi/big-cap) live cepat
UNIVERSE_REFRESH_MIN = _env_int("UNIVERSE_REFRESH_MIN", 30)
NEWS_REFRESH_MIN = _env_int("NEWS_REFRESH_MIN", 8)   # berita lebih segar (dulu 20) → prediksi lebih reaktif
# --- Panel "Berita & Sentimen" (TAMPILAN saja — tidak mengubah jendela analisis agen) ---
# Berita lebih tua dari NEWS_DISPLAY_HOURS TIDAK ditampilkan (dulu 72 jam → feed terasa basi).
# Volume nyata 2026-07-18: ~270 berita/24 jam (124 lokal + 108 global) → jendela 24 jam tetap
# padat, dan LIMIT-lah yang selama ini membatasi (60), bukan jendelanya.
NEWS_DISPLAY_HOURS = _env_int("NEWS_DISPLAY_HOURS", 24)
NEWS_DISPLAY_LIMIT = _env_int("NEWS_DISPLAY_LIMIT", 200)
# Filing IDX = sumber PRIMER tercepat (terbit sebelum media). Poll rapat saat buka = edge
# latensi legal (data publik, cuma lebih cepat baca). Deterministik/0-token → aman dirapatkan.
DISCLOSURE_REFRESH_MIN = _env_int("DISCLOSURE_REFRESH_MIN", 5)  # dulu 15; jam-tutup tetap di-gate 60mnt
AGENT_CYCLE_MIN = _env_int("AGENT_CYCLE_MIN", 12)
TICKERS_PER_CYCLE = _env_int("TICKERS_PER_CYCLE", 5)
BACKTEST_REFRESH_MIN = _env_int("BACKTEST_REFRESH_MIN", 360)  # backtest tiap 6 jam

# --- Scanner (cari yang terbaik) ---
# Tiap siklus: heuristik scan SEMUA saham (cepat), lalu LLM hanya analisis kandidat terbaik.
# 3 kandidat × debat (≤2 putaran) ≈ hemat kuota free-tier; naikkan jika kuota lega.
CANDIDATES_PER_CYCLE = _env_int("CANDIDATES_PER_CYCLE", 2)   # jumlah top utk deep-LLM (turun 3→2: hemat kuota harian)
DEBATE_ROUNDS = _env_int("DEBATE_ROUNDS", 2)                 # maks putaran debat Analyst<->Trader
# Rebuttal hanya bila ada yang dipertaruhkan (BUY/SELL atau keyakinan >=60) — disagreement
# low-stakes (HOLD lemah) tak layak ~3.8k token/putaran. Lihat trader.should_debate.
DEBATE_ONLY_ACTIONABLE = _env_bool("DEBATE_ONLY_ACTIONABLE", True)
SCAN_RECORD_TOP = _env_int("SCAN_RECORD_TOP", 25)           # top-K dicatat prediksinya
SCAN_WORKERS = 8
# Berapa kandidat di-deep-dive LLM SERENTAK. _decide_for read-only (aman paralel), commit
# tetap sekuensial. CATATAN 2026-06-29: =3 bikin BURST → cerebras "Tokens/min exceeded" & groq
# RPM → "semua provider gagal" jatuh ke heuristik. Turun ke 2 (burst separuh, kuota free-tier
# lebih aman). Tiap kandidat masih lebih cepat berkat cerebras-analis + CTO fast-fail.
# Audit percakapan 2026-07-07: dgn =2 pun 24% percakapan kehilangan CTO (badai 429 semua
# provider serentak → keputusan jatuh ke heuristik, tesis analis terbuang). Turun ke 1:
# kandidat dianalisis sekuensial — siklus lebih lambat tapi tiap keputusan utuh ber-CTO.
LLM_PARALLEL = _env_int("LLM_PARALLEL", 1)
# CTO gagal total → tunggu ini lalu ulang SEKALI sebelum menyerah ke heuristik
# (> CB_COOLDOWN 90s supaya breaker semua provider sudah pulih).
TRADER_RETRY_WAIT_S = _env_int("TRADER_RETRY_WAIT_S", 95)

# --- Rate limit LLM (free tier NVIDIA mudah kena 429) ---
LLM_MIN_INTERVAL = float(_env_int("LLM_MIN_INTERVAL_MS", 1800)) / 1000.0  # jarak antar call
LLM_MAX_RETRIES = _env_int("LLM_MAX_RETRIES", 2)
LLM_RETRY_BACKOFF = _env_int("LLM_RETRY_BACKOFF", 6)  # detik (×percobaan)

# --- Circuit breaker per-provider: provider kena rate-limit/timeout → di-skip sejenak,
#     berhenti menghantam yang sudah mati (biang 540 error 429/24j: chain & dewan retry
#     provider ke-limit tiap siklus). Provider pulih otomatis setelah cooldown lewat.
CB_COOLDOWN = _env_int("CB_COOLDOWN_S", 90)          # rate-limit/timeout biasa (kuota per-menit)
CB_COOLDOWN_MAX = _env_int("CB_COOLDOWN_MAX_S", 900)  # plafon backoff eksponensial per-menit (15 mnt)
CB_COOLDOWN_DAY = _env_int("CB_COOLDOWN_DAY_S", 7200)  # "per day/daily" limit → istirahat 2 jam
# Timeout bukan penolakan provider, cuma jawaban lambat, jadi cooldown-nya datar dan pendek
# serta baru menyala pada timeout kedua beruntun (lihat llm._trip).
CB_TIMEOUT_COOLDOWN = _env_int("CB_TIMEOUT_COOLDOWN_S", 30)

# --- Auth dashboard (login web, lihat app/auth.py) ---
# SESSION_SECRET menandatangani cookie sesi (Starlette SessionMiddleware). WAJIB diisi di
# produksi; kosong -> server pakai secret acak per-proses (sesi putus tiap restart).
SESSION_SECRET = _env("SESSION_SECRET")
AUTH_USER = _env("AUTH_USER", "yosua")
# Password login dari env — TIDAK di-hardcode. Kosong = login dimatikan (fail-closed:
# tak ada yang bisa masuk, bukan terbuka tanpa auth).
AUTH_PASSWORD = _env("AUTH_PASSWORD")

# --- Role GUEST (akses terbatas: hanya lihat 5 saham, kuota analis harian) ---
# Guest login pakai GUEST_USER/GUEST_PASSWORD. Password dari env (kosong = guest DIMATIKAN,
# fail-closed). Guest hanya boleh melihat GUEST_TICKERS (maks 5) dan menjalankan analis LLM
# maks GUEST_DAILY_ANALYST kali/hari. Enforcement server-side terpusat di app/guest.py.
GUEST_USER = _env("GUEST_USER", "guest")
GUEST_PASSWORD = _env("GUEST_PASSWORD")
GUEST_TICKERS = [t.strip().upper() for t in
                 _env("GUEST_TICKERS", "BBCA,BBRI,BMRI,TLKM,ASII").split(",") if t.strip()]
GUEST_DAILY_ANALYST = _env_int("GUEST_DAILY_ANALYST", 3)
# Rate-limit request guest (anti-DoS satu-sumber & bot): maks N request / jendela detik per IP.
# Bukan anti-DDoS terdistribusi (itu tugas Caddy/CDN) — ini menahan 1 IP membanjiri endpoint
# baca murah. Admin TIDAK kena. Default 40 req / 10 dtk = longgar utk pemakaian manusia normal.
GUEST_RATE_MAX = _env_int("GUEST_RATE_MAX", 40)
GUEST_RATE_WINDOW_S = _env_int("GUEST_RATE_WINDOW_S", 10)
# Cookie sesi Secure flag: ON di produksi (HTTPS). Set COOKIE_SECURE=0 hanya utk dev http lokal.
COOKIE_SECURE = _env_bool("COOKIE_SECURE", True)

# Mode LOKAL/MIRROR. RUN_ENGINE=0 mematikan scheduler LLM (lokal jadi penonton/cadangan,
# tidak membakar kuota; script engine tetap ada, hanya tidak dijalankan). Live: biarkan
# default (1). MIRROR_UPSTREAM di-set di lokal -> saat start, tarik snapshot DB dari server
# live dan simpan ke DB lokal (cadangan tahan-mati). Login pakai AUTH_USER/AUTH_PASSWORD.
RUN_ENGINE = _env_bool("RUN_ENGINE", True)
MIRROR_UPSTREAM = _env("MIRROR_UPSTREAM").rstrip("/")     # mis. https://host-kamu.example
MIRROR_ON_START = _env_bool("MIRROR_ON_START", bool(MIRROR_UPSTREAM))
# Rahasia PAIRING mirror: sama persis di server live & mirror lokal. /api/backup hanya
# melayani permintaan yang membawa secret ini (di atas login + TLS) -> cuma mesin yang
# di-pair yang bisa menarik seluruh DB. Server: kosong = /api/backup tertutup (fail-closed).
MIRROR_SECRET = _env("MIRROR_SECRET")
# Proxy opsional untuk situs ber-Cloudflare yang blokir IP DATACENTER (arus asing/disclosure
# IDX). Set ke URL residential-proxy atau scraping-API proxy-mode (http://user:pass@host:port)
# → VPS fetch sendiri tanpa dorong-dari-lokal. Kosong = fetch langsung + fallback push residensial.
IDX_PROXY = _env("IDX_PROXY")
# Interval sync INKREMENTAL mirror lokal (detik): tarik /api/mirror (payload kecil) berkala
# supaya DB lokal tetap segar tanpa mengunduh ulang seluruh DB. Lantai 15 dtk di-enforce di
# app/mirror_sync.py (jangan menghantam server live). Hanya jalan bila RUN_ENGINE=0.
MIRROR_INTERVAL = _env_int("MIRROR_INTERVAL", 60)

PORT = _env_int("PORT", 8800)
# Alamat bind server. Default 127.0.0.1 = HANYA bisa diakses dari komputer ini (localhost).
# Ini menutup paparan LAN: di WiFi publik, tanpa ini siapa pun di jaringan sama bisa membuka
# dashboard DAN memanggil endpoint POST yang membakar kuota LLM (biaya). Set HOST=0.0.0.0 di
# .env HANYA kalau memang sengaja mau diakses dari perangkat lain di jaringan tepercaya.
HOST = _env("HOST", "127.0.0.1")

# Zona waktu pasar (IDX = WIB / UTC+7)
MARKET_TZ = "Asia/Jakarta"
MARKET_OPEN = (9, 0)     # 09:00 WIB
MARKET_LUNCH = (12, 0)   # rehat siang
MARKET_RESUME = (13, 30)
MARKET_CLOSE = (15, 50)  # ~16:00 WIB

# --- Indeks pasar (akan diawali ^ / sesuai simbol yfinance) ---
# IHSG = Indeks Harga Saham Gabungan (Jakarta Composite Index)
INDICES = {
    "IHSG": "^JKSE",
}

# --- Watchlist saham IDX (akan ditambah .JK untuk yfinance) ---
# Daftar luas lintas sektor + IPO terbaru. Simbol tak valid otomatis dilewati
# (tidak akan crash) — sesuaikan/tambah sesukamu.
WATCHLIST = [
    # Perbankan
    "BBCA", "BBRI", "BMRI", "BBNI", "BRIS", "ARTO", "BBTN", "BTPS", "BNGA",
    "BBYB", "MEGA", "PNBN", "BJBR", "BJTM", "BANK", "AGRO", "BBKP", "NISP",
    # Telekomunikasi, menara & teknologi
    "TLKM", "EXCL", "ISAT", "GOTO", "EMTK", "BUKA", "MTEL", "TOWR", "TBIG",
    "DCII", "WIFI", "BELI", "MLPT", "MTDL", "DMMX",
    # Konsumer
    "UNVR", "ICBP", "INDF", "MYOR", "AMRT", "GGRM", "HMSP", "SIDO", "MAPI",
    "MAPA", "ACES", "ERAA", "RALS", "HRTA", "CMRY", "ULTJ", "KEJU", "WIIM",
    # Energi, batu bara & migas
    "ADRO", "PTBA", "ITMG", "INDY", "MEDC", "PGAS", "AKRA", "HRUM", "BUMI",
    "BYAN", "DSSA", "DOID", "PTRO", "RAJA", "ELSA", "ENRG", "ADMR",
    # Tambang logam & mineral
    "ANTM", "INCO", "TINS", "MDKA", "NCKL", "AMMN", "MBMA", "BRMS", "PSAB",
    # Otomotif, industri & semen
    "ASII", "UNTR", "SMGR", "INTP", "HEXA", "AUTO", "GJTL", "SMSM",
    # Properti & konstruksi
    "BSDE", "CTRA", "PWON", "SMRA", "PANI", "APLN", "DMAS", "KIJA", "ASRI",
    "LPKR", "WIKA", "PTPP", "ADHI", "JSMR", "WTON", "CBDK",
    # Kimia & energi baru
    "BRPT", "TPIA", "BREN", "CUAN", "ESSA", "AVIA",
    # Unggas & agribisnis
    "CPIN", "JPFA", "MAIN", "AALI", "LSIP", "SSMS", "DSNG", "TAPG",
    # Kesehatan
    "KLBF", "KAEF", "INAF", "SILO", "HEAL", "MIKA", "PRDA",
    # Media, ritel & holding
    "MNCN", "SCMA", "FILM", "PZZA", "MAPB", "SRTG", "BMTR", "PNLF", "RATU",
]

# --- Sinyal makro global (ticker yfinance) yang dipantau agen ---
# Dipakai untuk konteks: minyak naik -> energi/tambang, rupiah lemah -> importir, dst.
MACRO_SIGNALS = {
    "IHSG": "^JKSE",
    "WTI Crude Oil": "CL=F",
    "Brent Oil": "BZ=F",
    "Gold": "GC=F",
    "Copper": "HG=F",
    "Natural Gas": "NG=F",
    "USD/IDR": "IDR=X",
    "CNY/IDR": "CNYIDR=X",
    "JPY/IDR": "JPYIDR=X",
    "EUR/IDR": "EURIDR=X",
    "SGD/IDR": "SGDIDR=X",
    "Dollar Index (DXY)": "DX-Y.NYB",
    "US 10Y Yield": "^TNX",
    "VIX (Fear)": "^VIX",
    "Coal (proxy ITMG)": "ITMG.JK",
    "Nasdaq": "^IXIC",
    "S&P 500": "^GSPC",
    "Nikkei 225": "^N225",
    "Hang Seng": "^HSI",
    "Shanghai": "000001.SS",
    "Bitcoin": "BTC-USD",
}

# --- Sumber berita ---
# Lokal (RSS media keuangan Indonesia)
NEWS_FEEDS_LOCAL = [
    "https://www.cnbcindonesia.com/market/rss",
    "https://www.cnbcindonesia.com/news/rss",
    "https://www.cnbcindonesia.com/investment/rss",
    "https://investasi.kontan.co.id/rss",
    "https://insight.kontan.co.id/rss",
    "https://market.bisnis.com/rss",
    "https://finance.detik.com/rss",
    "https://www.idnfinancials.com/rss/news",
    "https://www.antaranews.com/rss/ekonomi",
    "https://www.antaranews.com/rss/terkini",
    "https://emitennews.com/rss",
    "https://www.idxchannel.com/rss",
    # +4 divalidasi 2026-07-02 (200 OK & berisi; filter _is_relevant buang off-topic):
    "https://www.cnnindonesia.com/ekonomi/rss",
    "https://katadata.co.id/rss",
    "https://ekbis.sindonews.com/rss",
    "https://wartaekonomi.co.id/rss",
]
# OSINT: Google News RSS per-topik (ekonomi/makro/komoditas) — "apa yang terjadi & mungkin terjadi"
NEWS_GOOGLE_TOPICS = [
    "suku bunga Bank Indonesia BI rate",
    "inflasi Indonesia BPS",
    "nilai tukar rupiah dollar AS",
    "harga minyak dunia brent",
    "harga batu bara acuan",
    "harga nikel dunia",
    "harga emas dunia",
    "harga CPO sawit",
    "tarif impor Amerika Serikat perang dagang",
    "The Fed suku bunga FOMC",
    "resesi ekonomi global",
    "MSCI Indonesia rebalancing asing",
    "investor asing jual beli saham IHSG",
    # --- IPO / pencatatan saham baru (biar emiten baru tak kelewat) ---
    "IPO BEI saham baru tercatat kode emiten",
    "pencatatan perdana saham melantai BEI",
    "calon emiten IPO penawaran umum perdana",
    "saham IPO mendatang minggu ini Bursa Efek Indonesia",
    "ekonomi China stimulus",
    "IHSG hari ini sentimen pasar",
    "Presiden Prabowo kebijakan ekonomi",
    "Prabowo pidato investasi APBN",
    "Prabowo gaji guru kopdes makan bergizi gratis MBG",
    "defisit APBN anggaran negara dampak pasar",
    "reshuffle kabinet Prabowo sentimen investor",
    # --- Global breaking / geopolitik (penggerak besar lewat harga komoditas & risiko) ---
    "Selat Hormuz Strait of Hormuz oil",
    "perang Timur Tengah Iran Israel konflik",
    "gangguan pasokan minyak global oil supply disruption",
    "OPEC produksi minyak harga",
    "krisis geopolitik global pasar saham",
    "sanksi embargo perdagangan global",
    "perang Rusia Ukraina dampak energi",
    "bencana besar gangguan ekonomi pemadaman",
    "harga minyak melonjak lonjakan",
    "resesi krisis keuangan global",
    # --- Kebijakan AS / Trump (penggerak pasar global) ---
    "kebijakan Presiden Trump tarif impor",
    "Trump perang dagang China tarif",
    "Trump tekanan Federal Reserve suku bunga",
    "Amerika Serikat sanksi kebijakan dagang dampak Asia",
    "kebijakan tarif AS dampak emerging market Indonesia",
]

# Global (RSS makro / ekonomi / komoditas — "apa yang terjadi & mungkin terjadi")
NEWS_FEEDS_GLOBAL = [
    "https://www.investing.com/rss/news_25.rss",      # commodities
    "https://www.investing.com/rss/news_1.rss",       # economy
    "https://www.investing.com/rss/news_95.rss",      # economic indicators
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",  # WSJ markets
    "https://feeds.a.dj.com/rss/RSSWorldNews.xml",    # WSJ world
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # CNBC markets
    "https://www.cnbc.com/id/20910258/device/rss/rss.html",   # CNBC economy
    "https://www.cnbc.com/id/100727362/device/rss/rss.html",  # CNBC world
    "http://feeds.marketwatch.com/marketwatch/topstories/",   # MarketWatch
    "https://www.federalreserve.gov/feeds/press_all.xml",     # The Fed press
]

# YouTube RSS (judul + deskripsi video = teks yang diparse pipeline berita yang sama).
# Menangkap pidato/kebijakan politik penggerak IHSG yang sering luput dari RSS teks.
# Channel_id divalidasi via tools/yt_news.py (resolve). scope=local → auto-naik ke global
# kalau market-wide (lihat news.MARKET_WIDE: prabowo/trump/gaji guru/kopdes/MBG/dst).
NEWS_YOUTUBE = [
    "https://www.youtube.com/feeds/videos.xml?channel_id=UC_m_NBgf7ieJBHzb6vvJC5A",  # Sekretariat Presiden (pidato Prabowo resmi)
    "https://www.youtube.com/feeds/videos.xml?channel_id=UCQA6NejSxQguRkD3L8eXHzA",  # IDX Channel (pasar modal)
    "https://www.youtube.com/feeds/videos.xml?channel_id=UCneA4BuveCEgJql1m7lwFag",  # KOMPAS TV
    "https://www.youtube.com/feeds/videos.xml?channel_id=UCzl0OrB3-ehunyotIQvK77A",  # Metro TV
]
