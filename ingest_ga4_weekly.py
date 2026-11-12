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
Sources ingested: ga4 (sessions, engaged_sessions, key_events, revenue_usd),
ga4_ai (AI-referral totals), ga4_ai_source (per-engine), ga4_direct,
ga4_youtube. meta rows (ga4_property) are captured into metrics JSON.

Data-quality rules:
- Several GA4 properties (Jamie Anne): only the '(primary)' one is kept.
- 'status: pending' (no GA4 access yet) writes no row and removes any old one.
- Source caveats go to metrics.caveats; any 'INVALID' caveat sets
  metrics.invalid = true (e.g. Swell Score pre-June 2026 tracking gap).
- A metric whose caveat says 'NOT REPORTED' (Winona revenue) is not
  stored; the reason goes to metrics.not_reported.

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
import re
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


PROP_RE = re.compile(r"^(p\d+[^|]*?)\s*\|\s*(.*)$")


def _split_property(dim):
    """'p420205458 JamieAnne.co (primary) | all_sources' -> (prop, 'all_sources').
    Single-property clients have no prefix -> (None, dim)."""
    m = PROP_RE.match(dim)
    return (m.group(1).strip(), m.group(2).strip()) if m else (None, dim)


def build_periods(rows):
    """rows: iterable of (source, metric, dimension, value, window_start,
    window_end, caveat). Returns ({(ws, we): period}, meta).

    - Clients with several GA4 properties: only the one marked '(primary)'
      is kept (or the first one seen if none is marked).
    - 'status: pending' rows mark the period pending (no GA4 access yet).
    - Caveats are kept. 'INVALID' in a caveat flags the whole period;
      a metric whose caveat starts with 'NOT REPORTED' is dropped and the
      reason recorded instead.
    """
    rows = [tuple((x or "").strip() for x in r) for r in rows]
    meta, props = {}, []
    for src, metric, dim, val, *_ in rows:
        if src == "meta":
            meta[metric] = val
        elif src.startswith("ga4"):
            prop, _ = _split_property(dim)
            if prop and prop not in props:
                props.append(prop)
    primary = next((p for p in props if "(primary)" in p), props[0] if props else None)

    periods = {}
    for src, metric, dim, val, ws, we, caveat in rows:
        if src == "meta" or not src.startswith("ga4") or not ws or not we:
            continue
        prop, dim = _split_property(dim)
        if prop and prop != primary:
            continue
        p = periods.setdefault((ws, we), {"ga4": {}, "ai_total": {}, "ai_by_source": {},
                                          "direct": {}, "youtube": {}, "caveats": set(),
                                          "not_reported": {}, "pending": False})
        if metric == "status" and dim == "pending":
            p["pending"] = True
            continue
        if caveat:
            for part in caveat.split("; "):
                p["caveats"].add(part)
        if caveat.upper().startswith("NOT REPORTED") or "; NOT REPORTED" in caveat.upper():
            p["not_reported"][f"{src}.{metric}"] = caveat
            continue
        v = _num(val)
        if src == "ga4":
            p["ga4"][metric] = v
        elif src == "ga4_ai":
            p["ai_total"][metric] = v
        elif src == "ga4_ai_source":
            p["ai_by_source"].setdefault(dim, {})[metric] = v
        elif src == "ga4_direct":
            p["direct"][metric] = v
        elif src == "ga4_youtube":
            p["youtube"][metric] = v
    for p in periods.values():
        p["primary_property"] = primary
    return periods, meta


def write_periods(c, cid, periods, meta):
    """Returns (written, removed). A pending period (no GA4 access) or one
    without sessions writes nothing and removes any old row for that window."""
    n = removed = 0
    for (ws, we), p in sorted(periods.items()):
        g = p["ga4"]
        if p["pending"] or g.get("sessions") is None:
            c.execute("DELETE FROM ga4_weekly WHERE client_id=%s AND week_start=%s AND week_end=%s",
                      (cid, ws, we))
            removed += c.rowcount
            continue
        caveats = sorted(p["caveats"])
        metrics = {
            "period_kind": _period_kind(ws, we),
            "invalid": any("INVALID" in x.upper() for x in caveats),
            "engaged_sessions": g.get("engaged_sessions"),
            "key_events": g.get("key_events"),
            "revenue_usd": g.get("revenue_usd"),
            "ga4_property": p["primary_property"] or meta.get("ga4_property"),
            "ai_total": p["ai_total"] or None,
            "ai_by_source": p["ai_by_source"] or None,
            "direct": p["direct"] or None,
            "youtube": p["youtube"] or None,
            "not_reported": p["not_reported"] or None,
            "caveats": caveats or None,
        }
        c.execute("DELETE FROM ga4_weekly WHERE client_id=%s AND week_start=%s AND week_end=%s", (cid, ws, we))
        c.execute("""INSERT INTO ga4_weekly (client_id, week_start, week_end, sessions, conversions, metrics)
                     VALUES (%s,%s,%s,%s,%s,%s)""",
                  (cid, ws, we, g.get("sessions"), g.get("key_events"), json.dumps(metrics)))
        n += 1
    return n, removed


def ingest_file(path, slug_map):
    slug = alias.get(os.path.basename(os.path.dirname(path)), os.path.basename(os.path.dirname(path)))
    cid = slug_map.get(slug)
    if not cid:
        return (path, "NO-CLIENT for slug " + slug, 0)
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [(r["source"], r["metric"], r["dimension"], r["value"],
                 r["window_start"], r["window_end"], r.get("caveat", "")) for r in csv.DictReader(f)]
    periods, meta = build_periods(rows)
    with db.get_conn() as conn:
        with conn.cursor() as c:
            n, removed = write_periods(c, cid, periods, meta)
    if removed:
        print(f"removed {removed} pending/empty row(s) for {slug}")
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
                    metric, dim, val, ws, we, caveat = (list(row) + [""] * 6)[:6]
                    groups.setdefault((r["client_id"], r["report_period"]), []).append(
                        (src, metric, dim, val, ws, we, caveat))
            for (cid, period), rows in sorted(groups.items()):
                periods, meta = build_periods(rows)
                if not periods:
                    continue
                n, removed = write_periods(c, cid, periods, meta)
                total += n
                print(f"ok client {cid} {period}: {n} period(s)"
                      + (f", removed {removed} pending/empty" if removed else ""))
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
