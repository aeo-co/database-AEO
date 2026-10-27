#!/usr/bin/env python3
"""
ingest_ga4_weekly.py — ingest GA4 (and AI-referral) rows from monthly-pulls/ and
weekly-pulls/ CSVs into the ga4_weekly table.

Run: cd /root/database-AEO && ./venv/bin/python ingest_ga4_weekly.py
Optionally pass specific file paths as args; default scans both pull dirs.

CSV format (long rows):
  client,month,source,metric,dimension,value,window_start,window_end,caveat
Sources ingested: ga4 (sessions, engaged_sessions, key_events, engagement_rate_pct,
avg_engagement_time_sec), ga4_ai (AI-referral totals), ga4_ai_source (per-engine).
meta rows (ga4_property etc.) are captured into metrics JSON.
Idempotent: deletes existing rows for (client_id, week_start) before insert.

RULE: whenever new GA4 pull CSVs appear, run this so ga4_weekly stays current —
do not leave GA4 data un-ingested.
"""
import sys, csv, glob, json, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

alias = {"kj-trading": "kj-trading-systems", "living-well": "living-well-with-dr-michelle"}

def _num(v):
    if v in (None, ""): return None
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except ValueError:
        return v

def ingest_file(path, slug_map):
    slug = alias.get(os.path.basename(os.path.dirname(path)), os.path.basename(os.path.dirname(path)))
    cid = slug_map.get(slug)
    if not cid:
        return (path, "NO-CLIENT for slug " + slug, 0)
    rows, meta = {}, {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            src, metric, dim, val = (r["source"].strip(), r["metric"].strip(),
                                     r["dimension"].strip(), r["value"].strip())
            ws, we = r["window_start"].strip(), r["window_end"].strip()
            if not ws: continue
            if src == "meta":
                meta[metric] = val
                continue
            if not src.startswith("ga4"):
                continue
            rows.setdefault(ws, {"week_end": we, "sources": {}})
            m = rows[ws]
            if src == "ga4":
                m[metric] = val
            elif src in ("ga4_ai", "ga4_ai_source"):
                m["sources"].setdefault(src if src == "ga4_ai" else dim, {})[metric] = val
    n = 0
    with db.get_conn() as conn:
        with conn.cursor() as c:
            for ws, m in rows.items():
                sessions = int(float(m.get("sessions") or 0))
                ke = m.pop("key_events", None)
                conversions = int(float(ke)) if ke not in (None, "") else None
                metrics = {
                    "engaged_sessions": _num(m.get("engaged_sessions")),
                    "engagement_rate_pct": _num(m.get("engagement_rate_pct")),
                    "avg_engagement_time_sec": _num(m.get("avg_engagement_time_sec")),
                    "key_events": _num(ke),
                    "ga4_property": meta.get("ga4_property"),
                    "ai_total": {k: _num(v) for k, v in m["sources"].get("ga4_ai", {}).items()} if "ga4_ai" in m["sources"] else None,
                    "ai_by_source": {k: {kk: _num(vv) for kk, vv in v.items()} for k, v in m["sources"].items() if k != "ga4_ai"} or None,
                }
                c.execute("DELETE FROM ga4_weekly WHERE client_id=%s AND week_start=%s", (cid, ws))
                c.execute("""INSERT INTO ga4_weekly (client_id, week_start, week_end, sessions, conversions, metrics)
                             VALUES (%s,%s,%s,%s,%s,%s)""",
                          (cid, ws, m["week_end"], sessions, conversions, json.dumps(metrics)))
                n += 1
        conn.commit()
    return (path, None, n)

def main():
    paths = sys.argv[1:] or (sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "monthly-pulls", "*", "*.csv")))
                             + sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "weekly-pulls", "*", "*.csv"))))
    with db.get_conn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT id, slug FROM clients")
            slug_map = {r["slug"]: r["id"] for r in c.fetchall()}
    total, errs = 0, []
    for p in paths:
        _, e, n = ingest_file(p, slug_map)
        total += n
        if e: errs.append(e)
        else: print("ok:", os.path.basename(p), n, "rows")
    print("TOTAL rows inserted:", total)
    for e in errs: print("ERR:", e)

if __name__ == "__main__":
    main()
