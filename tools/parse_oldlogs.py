"""Rekonstruksi DB historis dari log engine lama (Prediction_OLD_LOGS_BACKUP).

Sumber (read-only):
  - engine.log            : trade tereksekusi (BELI/JUAL, ts WIB) + keputusan trader/decide
  - report_YYYYMMDD.md    : portofolio, transaksi (ts UTC), prediksi, pelajaran,
                            ledger harian, percakapan agen

Output:
  - data/history_oldlocal.db          (skema sama persis dgn app/db.py -> SCHEMA)
  - data/history_oldlocal_summary.csv (ledger harian gabungan)

Konvensi ts di DB: UTC naive "YYYY-MM-DDTHH:MM[:SS]" (report bagian 4 memang UTC;
ts engine.log/WIB dikonversi -7 jam). Semua insert pakai autoincrement baru.

Jalankan ulang kapan saja: python tools/parse_oldlogs.py
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

BACKUP_DIR = os.path.join(os.path.dirname(ROOT), "Prediction_OLD_LOGS_BACKUP")
DB_PY = os.path.join(ROOT, "app", "db.py")
DEST_DB = os.path.join(ROOT, "data", "history_oldlocal.db")
DEST_CSV = os.path.join(ROOT, "data", "history_oldlocal_summary.csv")

WIB_OFFSET = timedelta(hours=7)  # WIB = UTC+7, tanpa DST
START_CASH_DEFAULT = 100_000_000.0

# ---------------------------------------------------------------- util

def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", "").replace("Rp", "").strip())
    except (ValueError, AttributeError):
        return None


def _wib_to_utc(dt: datetime) -> datetime:
    return dt - WIB_OFFSET


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def load_schema() -> str:
    """Ambil string SCHEMA persis dari app/db.py tanpa mengeksekusi kode app."""
    src = _read(DB_PY)
    m = re.search(r'^SCHEMA = """(.*?)"""', src, re.S | re.M)
    if not m:
        raise RuntimeError("SCHEMA tidak ditemukan di app/db.py")
    return m.group(1)


# ---------------------------------------------------------------- engine.log

RE_TRADE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ \| \[trader/trade/info\] "
    r"\S+ (BELI|JUAL) (\S+) ([\d.]+) lembar @ ([\d.]+) \((.*)\)"
)
RE_DECIDE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ \| \[trader/decide/info\] "
    r"\S+ (\S+): (UP|DOWN|FLAT) ([\d.]+)% /(\d+)h -> (\w+)(.*)$"
)


def parse_engine_log(path: str):
    """-> (trades, decides). ts dikonversi WIB -> UTC."""
    trades, decides = [], []
    if not os.path.exists(path):
        return trades, decides
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                m = RE_TRADE.match(line)
                if m:
                    d, t, side_id, ticker, qty, price, paren = m.groups()
                    ts = _wib_to_utc(datetime.fromisoformat(f"{d} {t}"))
                    qty_f, price_f = float(qty), float(price)
                    gross = round(qty_f * price_f, 2)
                    fee = net = None
                    reason = None
                    if side_id == "BELI":
                        side = "BUY"
                        net = _num(paren)          # "(Rp12,016,798)" = biaya total
                        if net is not None:
                            fee = round(net - gross, 2)
                    else:
                        side = "SELL"
                        reason = paren.strip() or None  # "realized Rp-27,462"
                    trades.append({
                        "ts": ts.strftime("%Y-%m-%dT%H:%M:%S"),
                        "ticker": ticker, "side": side, "qty": qty_f,
                        "price": price_f, "gross": gross, "fee": fee,
                        "net": net, "reason": reason,
                    })
                    continue
                m = RE_DECIDE.match(line)
                if m:
                    d, t, ticker, direction, prob, hor, action, note = m.groups()
                    ts = _wib_to_utc(datetime.fromisoformat(f"{d} {t}"))
                    decides.append({
                        "ts": ts.strftime("%Y-%m-%dT%H:%M:%S"),
                        "ticker": ticker, "direction": direction,
                        "probability": float(prob), "horizon_days": int(hor),
                        "action": action, "note": note.strip() or None,
                    })
            except Exception:
                continue  # baris rusak -> lewati
    return trades, decides


# ---------------------------------------------------------------- report_*.md

SECTION_KEYS = [
    ("portofolio", r"^## 1\. Portofolio"),
    ("akurasi",    r"^## 2\. Akurasi"),
    ("posisi",     r"^## 3\. Posisi"),
    ("transaksi",  r"^## 4\. Transaksi"),
    ("penggerak",  r"^## 4b\. Penggerak"),
    ("prediksi",   r"^## 5\. Prediksi"),
    ("ledger",     r"^## 5b\. Ledger"),
    ("pelajaran",  r"^## 6\. Pelajaran"),
    ("faktor",     r"^## 6b\. Evaluasi"),
    ("skill",      r"^## 7\. Skill"),
    ("katalis",    r"^## 8\. Katalis"),
    ("ipo",        r"^## 8b\. IPO"),
    ("percakapan", r"^## 9\. Percakapan"),
    ("untuk",      r"^## 10\. Untuk Claude"),
]
_SECTION_RES = [(k, re.compile(p)) for k, p in SECTION_KEYS]


def split_sections(text: str) -> dict[str, list[str]]:
    """Potong report jadi seksi. Header spesifik (judul+nomor) supaya heading
    markdown di dalam body percakapan (mis. '## 1. Katalis...') tidak ikut."""
    lines = text.splitlines()
    marks = []  # (line_no, key)
    for i, line in enumerate(lines):
        for key, rx in _SECTION_RES:
            if rx.match(line):
                marks.append((i, key))
                break
    out: dict[str, list[str]] = {}
    for j, (i, key) in enumerate(marks):
        if key in out:      # ambil kemunculan pertama tiap seksi
            continue
        end = marks[j + 1][0] if j + 1 < len(marks) else len(lines)
        out[key] = lines[i + 1:end]
    return out


RE_REPORT_TS = re.compile(r"_Dibuat: (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})")


def report_ts_utc(text: str, fname: str) -> str:
    """Header '_Dibuat: ... WIB' -> UTC naive ISO menit."""
    m = RE_REPORT_TS.search(text)
    if m:
        dt = datetime.fromisoformat(f"{m.group(1)} {m.group(2)}")
    else:  # fallback dari nama file
        m2 = re.search(r"(\d{4})(\d{2})(\d{2})", fname)
        dt = datetime(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)), 12, 0)
    return _wib_to_utc(dt).strftime("%Y-%m-%dT%H:%M:%S")


def parse_portfolio(sec: list[str]) -> dict:
    out = {"start_cash": None, "cash": None, "equity": None,
           "total": None, "pnl": None, "pnl_pct": None}
    for line in sec:
        try:
            if m := re.search(r"Modal awal: Rp([\d,.-]+)", line):
                out["start_cash"] = _num(m.group(1))
            elif m := re.search(r"Kas: Rp([\d,.-]+)", line):
                out["cash"] = _num(m.group(1))
            elif m := re.search(r"Nilai posisi: Rp([\d,.-]+)", line):
                out["equity"] = _num(m.group(1))
            elif m := re.search(r"Total: Rp([\d,.-]+)", line):
                out["total"] = _num(m.group(1))
            elif m := re.search(r"P/L: Rp(-?[\d,.]+) \(([+-]?[\d.]+)%\)", line):
                out["pnl"] = _num(m.group(1))
                out["pnl_pct"] = float(m.group(2))
        except Exception:
            continue
    return out


RE_TX = re.compile(
    r"^- (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}) (BUY|SELL) ([A-Z0-9]+) ([\d.]+) @ ([\d.]+)"
)


def parse_trades_report(sec: list[str]) -> list[dict]:
    out = []
    for line in sec:
        m = RE_TX.match(line)
        if not m:
            continue
        ts, side, ticker, qty, price = m.groups()
        try:
            out.append({"ts": ts, "ticker": ticker, "side": side,
                        "qty": float(qty), "price": float(price)})
        except ValueError:
            continue
    return out


RE_PRED = re.compile(
    r"^- ([A-Z0-9]{2,6}): (UP|DOWN|FLAT) ([\d.]+)% /(\d+)h, target ([\d.]+)"
    r"(?: \(dibuat (\d{2})/(\d{2}) (\d{2}):(\d{2}) WIB\))? \[(\w+)\]"
)


def parse_predictions_report(sec: list[str], rpt_ts: str) -> list[dict]:
    """rpt_ts = ts report (UTC ISO). 'dibuat DD/MM HH:MM WIB' -> UTC."""
    year = int(rpt_ts[:4])
    out: list[dict] = []
    cur: dict | None = None
    for line in sec:
        try:
            m = RE_PRED.match(line)
            if m:
                tick, direction, prob, hor, target, dd, mm, hh, mi, status = m.groups()
                if dd:
                    dt = datetime(year, int(mm), int(dd), int(hh), int(mi))
                    ts = _wib_to_utc(dt).strftime("%Y-%m-%dT%H:%M:%S")
                else:
                    ts = rpt_ts
                cur = {
                    "ts": ts, "ticker": tick, "direction": direction,
                    "probability": float(prob), "horizon_days": int(hor),
                    "target_price": float(target),
                    "status": "open" if status == "open" else "resolved",
                    "outcome": None if status == "open" else status,
                    "teknikal": None, "berita": None, "has_dibuat": bool(dd),
                }
                out.append(cur)
            elif cur is not None:
                if m := re.match(r"^\s+- Teknikal: (.+)$", line):
                    cur["teknikal"] = m.group(1).strip()
                elif m := re.match(r"^\s+- Berita: (.+)$", line):
                    cur["berita"] = m.group(1).strip()
                elif line.strip().startswith("- "):
                    cur = None
        except Exception:
            cur = None
            continue
    return out


RE_LEDGER = re.compile(
    r"^\| (\d{4}-\d{2}-\d{2}) \| ([\d,.-]+) \| ([+-]?[\d,.]+) \| (\d+) \| (\d+) \| (\d+) \| ([\d.]+%|-) \|"
)


def parse_ledger(sec: list[str]) -> list[dict]:
    out = []
    for line in sec:
        m = RE_LEDGER.match(line)
        if not m:
            continue
        d, eq, pnl, tr, pr, rs, wr = m.groups()
        try:
            out.append({
                "date": d, "equity": _num(eq), "pnl": _num(pnl),
                "trades": int(tr), "preds": int(pr), "resolved": int(rs),
                "win_rate": None if wr == "-" else float(wr.rstrip("%")),
            })
        except Exception:
            continue
    return out


RE_LESSON = re.compile(r"^- \[(\w+)\] (.+)$")
RE_LESSON_TICKER = re.compile(r"Prediksi (?:UP|DOWN|FLAT) ([A-Z0-9]{2,6})")


def parse_lessons(sec: list[str]) -> list[dict]:
    out = []
    for line in sec:
        m = RE_LESSON.match(line)
        if not m:
            continue
        kind, lesson = m.groups()
        tick = RE_LESSON_TICKER.search(lesson)
        out.append({"kind": kind, "lesson": lesson.strip(),
                    "ticker": tick.group(1) if tick else None})
    return out


RE_CONV_HDR = re.compile(r"^### ([A-Z0-9]{2,6}) — (.+)$")  # "### TPIA — ..."


def parse_conversations(sec: list[str]) -> list[dict]:
    convs: list[dict] = []
    blocks: list[tuple[str, str, list[str]]] = []  # (ticker, summary, body)
    cur_body: list[str] | None = None
    for line in sec:
        m = RE_CONV_HDR.match(line)
        if m and "->" in m.group(2):
            cur_body = []
            blocks.append((m.group(1), f"{m.group(1)} — {m.group(2).strip()}", cur_body))
        elif cur_body is not None:
            cur_body.append(line)
    for ticker, summary, body in blocks:
        text = "\n".join(body).strip()
        analyst = trader = None
        m = re.search(r"\*\*ANALYST:\*\*\s*(.*?)\s*\*\*TRADER:\*\*\s*(.*)\Z", text, re.S)
        if m:
            analyst = m.group(1).strip() or None
            trader = m.group(2).strip() or None
        elif text:
            analyst = text
        convs.append({"ticker": ticker, "summary": summary,
                      "analyst": analyst, "trader": trader})
    return convs


# ---------------------------------------------------------------- build

def _trade_key(t: dict):
    return (t["ts"][:16], t["ticker"], t["side"], int(round(t["qty"])),
            round(float(t["price"]), 2))


def build(dest_db: str = DEST_DB, backup_dir: str = BACKUP_DIR,
          dest_csv: str = DEST_CSV) -> dict:
    if os.path.basename(dest_db) == "prediction.db":
        raise RuntimeError("menolak menulis ke prediction.db (DB live)")
    if os.path.exists(dest_db):
        os.remove(dest_db)  # re-runnable: bangun ulang dari nol

    conn = sqlite3.connect(dest_db)
    conn.executescript(load_schema())
    # kolom migrasi ringan yang ditambah init_db() app (expires_at, style)
    for col in ("expires_at", "style"):
        try:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass

    # ---- engine.log: trades + keputusan (prediksi mentah)
    eng_trades, eng_decides = parse_engine_log(os.path.join(backup_dir, "engine.log"))

    seen_trades: set = set()
    for t in eng_trades:
        k = _trade_key(t)
        if k in seen_trades:
            continue
        seen_trades.add(k)
        conn.execute(
            "INSERT INTO trades(ts,ticker,side,qty,price,gross,fee,net,reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (t["ts"], t["ticker"], t["side"], t["qty"], t["price"],
             t["gross"], t["fee"], t["net"], t["reason"]),
        )

    pred_ids: dict = {}   # (ts_menit, ticker, direction) -> rowid
    seen_decides: set = set()
    for d in eng_decides:
        k = (d["ts"], d["ticker"], d["direction"], d["probability"], d["horizon_days"])
        if k in seen_decides:
            continue
        seen_decides.add(k)
        factors = {"decision": d["action"]}
        if d["note"]:
            factors["note"] = d["note"]
        cur = conn.execute(
            "INSERT INTO predictions(ts,ticker,direction,probability,horizon_days,"
            "factors_json,status) VALUES (?,?,?,?,?,?,'open')",
            (d["ts"], d["ticker"], d["direction"], d["probability"],
             d["horizon_days"], json.dumps(factors, ensure_ascii=False)),
        )
        pred_ids.setdefault((d["ts"][:16], d["ticker"], d["direction"]), cur.lastrowid)

    # ---- reports (urut tanggal; report belakangan menimpa enrich/status)
    reports = sorted(f for f in os.listdir(backup_dir)
                     if re.fullmatch(r"report_\d{8}\.md", f))
    report_dates: set = set()
    ledger_merged: dict[str, dict] = {}
    seen_pred_nodib: set = set()
    seen_lessons: set = set()
    seen_convs: set = set()
    last_total = None

    for fname in reports:
        try:
            text = _read(os.path.join(backup_dir, fname))
        except OSError:
            continue
        rts = report_ts_utc(text, fname)
        report_dates.add(rts[:10])
        sec = split_sections(text)

        # portofolio -> satu baris per report
        p = parse_portfolio(sec.get("portofolio", []))
        if p["total"] is not None:
            conn.execute(
                "INSERT OR REPLACE INTO portfolio(ts,cash,equity,total,pnl,pnl_pct) "
                "VALUES (?,?,?,?,?,?)",
                (rts, p["cash"], p["equity"], p["total"], p["pnl"], p["pnl_pct"]),
            )
            last_total = p["total"]

        # transaksi -> dedup terhadap engine.log (kunci menit)
        for t in parse_trades_report(sec.get("transaksi", [])):
            k = _trade_key(t)
            if k in seen_trades:
                continue
            seen_trades.add(k)
            conn.execute(
                "INSERT INTO trades(ts,ticker,side,qty,price,gross) VALUES (?,?,?,?,?,?)",
                (t["ts"], t["ticker"], t["side"], t["qty"], t["price"],
                 round(t["qty"] * t["price"], 2)),
            )

        # prediksi -> enrich baris decide bila cocok, kalau tidak insert baru
        pred_target_ids: dict = {}
        for pr in parse_predictions_report(sec.get("prediksi", []), rts):
            reasoning = pr["teknikal"]
            factors = None
            if pr["teknikal"] or pr["berita"]:
                factors = json.dumps(
                    {k: v for k, v in
                     (("teknikal", pr["teknikal"]), ("berita", pr["berita"])) if v},
                    ensure_ascii=False)
            rid = None
            if pr["has_dibuat"]:
                base = datetime.fromisoformat(pr["ts"])
                for dm in (0, -1, 1):  # toleransi selisih 1 menit
                    kmin = ((base + timedelta(minutes=dm)).strftime("%Y-%m-%dT%H:%M"),
                            pr["ticker"], pr["direction"])
                    if kmin in pred_ids:
                        rid = pred_ids[kmin]
                        break
            else:
                knd = (pr["ticker"], pr["direction"], pr["probability"],
                       pr["horizon_days"], pr["target_price"])
                if knd in seen_pred_nodib:
                    continue
                seen_pred_nodib.add(knd)
            if rid is not None:
                conn.execute(
                    "UPDATE predictions SET target_price=?, status=?, outcome=?, "
                    "reasoning=COALESCE(?,reasoning), "
                    "factors_json=COALESCE(?,factors_json) WHERE id=?",
                    (pr["target_price"], pr["status"], pr["outcome"],
                     reasoning, factors, rid),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO predictions(ts,ticker,direction,probability,"
                    "horizon_days,target_price,reasoning,factors_json,status,outcome) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (pr["ts"], pr["ticker"], pr["direction"], pr["probability"],
                     pr["horizon_days"], pr["target_price"], reasoning, factors,
                     pr["status"], pr["outcome"]),
                )
                rid = cur.lastrowid
                if pr["has_dibuat"]:
                    pred_ids[(pr["ts"][:16], pr["ticker"], pr["direction"])] = rid
            pred_target_ids[(pr["ticker"], round(pr["target_price"], 2))] = rid

        # pelajaran
        for l in parse_lessons(sec.get("pelajaran", [])):
            k = (l["kind"], l["lesson"])
            if k in seen_lessons:
                continue
            seen_lessons.add(k)
            conn.execute(
                "INSERT INTO lessons(ts,ticker,kind,lesson) VALUES (?,?,?,?)",
                (rts, l["ticker"], l["kind"], l["lesson"]),
            )

        # percakapan agen
        for c in parse_conversations(sec.get("percakapan", [])):
            k = (c["ticker"], c["summary"], (c["analyst"] or "")[:120])
            if k in seen_convs:
                continue
            seen_convs.add(k)
            pid = None
            if m := re.search(r"target ([\d.]+)", c["summary"]):
                pid = pred_target_ids.get((c["ticker"], round(float(m.group(1)), 2)))
            conn.execute(
                "INSERT INTO conversations(ts,ticker,prediction_id,analyst,trader,summary) "
                "VALUES (?,?,?,?,?,?)",
                (rts, c["ticker"], pid, c["analyst"], c["trader"], c["summary"]),
            )

        # ledger harian (report belakangan menimpa tanggal sama)
        for row in parse_ledger(sec.get("ledger", [])):
            ledger_merged[row["date"]] = row

    # tanggal ledger tanpa report -> baris portfolio tambahan (kas/posisi tak diketahui)
    start_cash = START_CASH_DEFAULT
    for d in sorted(ledger_merged):
        if d in report_dates:
            continue
        row = ledger_merged[d]
        total = row["equity"]
        if total is None:
            continue
        pnl = round(total - start_cash, 2)
        conn.execute(
            "INSERT OR REPLACE INTO portfolio(ts,cash,equity,total,pnl,pnl_pct) "
            "VALUES (?,NULL,NULL,?,?,?)",
            (d, total, pnl, round(pnl / start_cash * 100, 4)),
        )

    conn.commit()

    # ---- CSV ledger gabungan
    os.makedirs(os.path.dirname(dest_csv), exist_ok=True)
    with open(dest_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "equity", "pnl", "trades", "preds", "resolved", "win_rate"])
        for d in sorted(ledger_merged):
            r = ledger_merged[d]
            w.writerow([d, r["equity"], r["pnl"], r["trades"], r["preds"],
                        r["resolved"], "" if r["win_rate"] is None else r["win_rate"]])

    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("trades", "predictions", "portfolio", "lessons", "conversations")}
    counts["ledger_days"] = len(ledger_merged)
    counts["last_total"] = last_total
    conn.close()
    return counts


if __name__ == "__main__":
    c = build()
    for k, v in c.items():
        print(f"{k}: {v}")
