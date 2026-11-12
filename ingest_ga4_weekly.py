#!/usr/bin/env python3
"""
ingest_ga4_weekly.py - ingest GA4 (and AI-referral) rows from monthly-pulls/ and
weekly-pulls/ CSVs into the ga4_weekly table.

    python ingest_ga4_weekly.py                 # scan both pull dirs
    python ingest_ga4_weekly.py a.csv b.csv     # specific files
    python ingest_ga4_weekly.py --from-db       # rebuild from the ga4 / meta
                                                # sections already stored in
                                                # shopify_report_sections

CSV format (long rows):
  client,month,source,metric,dimension,value,window_start,window_end,caveat
Sources ingested: ga4 (sessions, engaged_sessions, key_events, engagement_rate_pct,
avg_engagement_time_sec), ga4_ai (AI-referral totals), ga4_ai_source (per-engine).
meta rows (ga4_property etc.) are captured into metrics JSON.

One row per (client, window_start, window_end): a month and the week that
starts on the same day (2026-10 and 2026-10-wk1) are separate rows.
metrics.period_kind says which ('weekly' <= 7 days, else 'monthly').
Idempotent: re-running replaces the row for that exact window.

RULE: whenever new GA4 pull CSVs appear, run this so ga4_weekly stays current -
do not leave GA4 data un-ingested.
"""
import argparse
import csv
import glob
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

HERE = os.path.dirname(os.path.abspath(__file__))

alias = {"kj-trading": "kj-trading-systems", "living-well": "living-well-with-dr-michelle"}


def _num(v):
    if v in (None, ""): return None
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except ValueError:
        return v


def _period_kind(ws, we):
    days = (date.fromisoformat(we) - date.fromisoformat(ws)).days + 1
    return "weekly" if days <= 7 else "monthly"


def build_periods(rows):
    """rows: iterable of (source, metric, dimension, value, window_start, window_end).
    Returns ({(ws, we): period}, meta)."""
    periods, meta = {}, {}
    for src, metric, dim, val, ws, we in rows:
        src, metric, dim, val = src.strip(), metric.strip(), dim.strip(), val.strip()
        ws, we = ws.strip(), we.strip()
        if src == "meta":           # meta rows carry no window
            meta[metric] = val
            continue
        if not ws or not we or not src.startswith("ga4"):
            continue
        m = periods.setdefault((ws, we), {"sources": {}})
        if src == "ga4":
            m[metric] = val
        elif src in ("ga4_ai", "ga4_ai_source"):
            m["sources"].setdefault(src if src == "ga4_ai" else dim, {})[metric] = val
    return periods, meta


def write_periods(c, cid, periods, meta):
    n = 0
    for (ws, we), m in sorted(periods.items()):
        sessions = int(float(m.get("sessions") or 0))
        ke = m.pop("key_events", None)
        conversions = int(float(ke)) if ke not in (None, "") else None
        metrics = {
            "period_kind": _period_kind(ws, we),
            "engaged_sessions": _num(m.get("engaged_sessions")),
            "engagement_rate_pct": _num(m.get("engagement_rate_pct")),
            "avg_engagement_time_sec": _num(m.get("avg_engagement_time_sec")),
            "key_events": _num(ke),
            "ga4_property": meta.get("ga4_property"),
            "ai_total": {k: _num(v) for k, v in m["sources"].get("ga4_ai", {}).items()} if "ga4_ai" in m["sources"] else None,
            "ai_by_source": {k: {kk: _num(vv) for kk, vv in v.items()} for k, v in m["sources"].items() if k != "ga4_ai"} or None,
        }
        c.execute("DELETE FROM ga4_weekly WHERE client_id=%s AND week_start=%s AND week_end=%s", (cid, ws, we))
        c.execute("""INSERT INTO ga4_weekly (client_id, week_start, week_end, sessions, conversions, metrics)
                     VALUES (%s,%s,%s,%s,%s,%s)""",
                  (cid, ws, we, sessions, conversions, json.dumps(metrics)))
        n += 1
    return n


def ingest_file(path, slug_map):
    slug = alias.get(os.path.basename(os.path.dirname(path)), os.path.basename(os.path.dirname(path)))
    cid = slug_map.get(slug)
    if not cid:
        return (path, "NO-CLIENT for slug " + slug, 0)
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [(r["source"], r["metric"], r["dimension"], r["value"],
                 r["window_start"], r["window_end"]) for r in csv.DictReader(f)]
    periods, meta = build_periods(rows)
    with db.get_conn() as conn:
        with conn.cursor() as c:
            n = write_periods(c, cid, periods, meta)
    return (path, None, n)


def ingest_from_db():
    """Rebuild from ga4* + meta sections in shopify_report_sections, one
    group per (client, report_period) - i.e. one group per pull CSV."""
    total = 0
    with db.get_conn() as conn:
        with conn.cursor() as c:
            c.execute("""SELECT client_id, report_period, section_name, rows
                           FROM shopify_report_sections
                          WHERE report_period IS NOT NULL
                            AND (section_name ~ ' ga4' OR section_name ~ ' meta$')""")
            groups = {}
            for r in c.fetchall():
                src = r["section_name"].split(" ", 1)[1]
                for row in r["rows"]:
                    metric, dim, val, ws, we = (list(row) + [""] * 5)[:5]
                    groups.setdefault((r["client_id"], r["report_period"]), []).append(
                        (src, metric or "", dim or "", val or "", ws or "", we or ""))
            for (cid, period), rows in sorted(groups.items()):
                periods, meta = build_periods(rows)
                if not periods:
                    continue
                n = write_periods(c, cid, periods, meta)
                total += n
                print(f"ok client {cid} {period}: {n} period(s)")
    print("TOTAL rows written:", total)


def main():
    ap = argparse.ArgumentParser(description="Load GA4 rows into ga4_weekly.")
    ap.add_argument("files", nargs="*", help="pull CSVs (default: monthly-pulls/ + weekly-pulls/)")
    ap.add_argument("--from-db", action="store_true",
                    help="rebuild from ga4/meta sections in shopify_report_sections")
    args = ap.parse_args()
    if args.from_db:
        ingest_from_db()
        return
    paths = args.files or (sorted(glob.glob(os.path.join(HERE, "monthly-pulls", "*", "*.csv")))
                           + sorted(glob.glob(os.path.join(HERE, "weekly-pulls", "*", "*.csv"))))
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
