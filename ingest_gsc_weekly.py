#!/usr/bin/env python3
"""
ingest_gsc_weekly.py - load Google Search Console rows into gsc_weekly.

GSC numbers arrive in the same long-row pull CSVs as GA4
(monthly-pulls/<client>/*.csv, weekly-pulls/<client>/*.csv):

    client,month,source,metric,dimension,value,window_start,window_end,caveat

Every row whose source starts with 'gsc' is used. One gsc_weekly row per
(client, window_start, window_end), so a month and the week that starts
on the same day don't overwrite each other.

    python ingest_gsc_weekly.py                 # scan both pull dirs
    python ingest_gsc_weekly.py a.csv b.csv     # specific files
    python ingest_gsc_weekly.py --from-db       # backfill from the gsc_*
                                                # sections already stored in
                                                # shopify_report_sections

Columns: clicks / impressions / ctr (pct) / avg_position come from the
gsc_web 'total' rows. Everything else goes into metrics:

    metrics.web       - gsc_web non-total dimensions (branded_queries,
                        nonbranded_queries, export counts, ...)
    metrics.<section> - one key per other gsc_* source (country, device,
                        page, query_branded, query_nonbranded,
                        search_appearance, ai_features, ai_features_page, ...)
                        each {"rows": {dimension: {metric: value}},
                              "caveats": [...]}
    metrics.internal_only - branded_concern_queries + query_concern.
                        INTERNAL ONLY - never client-facing without Julian.

Periods where GSC access is still pending (gsc_web has only a
'status: pending' row) are skipped. Re-running updates in place.
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

# Pull-folder names that differ from the client slug (same as ingest_ga4_weekly).
ALIAS = {"kj-trading": "kj-trading-systems", "living-well": "living-well-with-dr-michelle"}

# Dimensions / sections that must never reach a client-facing report.
INTERNAL_WEB_DIMS = {"branded_concern_queries"}
INTERNAL_SECTIONS = {"query_concern"}


def _num(v):
    if v in (None, ""):
        return None
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except ValueError:
        return v


def _flat(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _period_kind(ws: str, we: str) -> str:
    days = (date.fromisoformat(we) - date.fromisoformat(ws)).days + 1
    return "weekly" if days <= 7 else "monthly"


def build_periods(rows):
    """
    rows: iterable of (source, metric, dimension, value, window_start,
    window_end, caveat). Returns {(ws, we): period_dict} for GSC rows only.
    """
    periods = {}
    for src, metric, dim, val, ws, we, caveat in rows:
        src, metric, dim = src.strip(), metric.strip(), dim.strip()
        val, ws, we, caveat = val.strip(), ws.strip(), we.strip(), (caveat or "").strip()
        if not src.startswith("gsc") or not ws or not we:
            continue
        p = periods.setdefault((ws, we), {"total": {}, "web": {}, "sections": {}, "pending": False})
        section = src[4:] if src.startswith("gsc_") else src  # gsc_web -> web
        if section == "web":
            if metric == "status" and dim == "pending":
                p["pending"] = True
            elif dim == "total":
                p["total"][metric] = _num(val)
                if caveat:
                    p.setdefault("total_caveats", set()).add(caveat)
            else:
                d = p["web"].setdefault(dim, {"caveat": caveat or None})
                d[metric] = _num(val)
        else:
            s = p["sections"].setdefault(section, {"rows": {}, "caveats": set()})
            s["rows"].setdefault(dim, {})[metric] = _num(val)
            if caveat:
                s["caveats"].add(caveat)
    return periods


def _to_metrics(p, ws, we, label):
    web = {k: v for k, v in p["web"].items() if k not in INTERNAL_WEB_DIMS}
    internal = {k: v for k, v in p["web"].items() if k in INTERNAL_WEB_DIMS}
    sections, internal_sections = {}, {}
    for name, s in p["sections"].items():
        out = {"rows": s["rows"], "caveats": sorted(s["caveats"])}
        if "window" in s:
            out["window"] = s["window"]
        (internal_sections if name in INTERNAL_SECTIONS else sections)[name] = out
    totals_extra = {k: v for k, v in p["total"].items()
                    if k not in ("clicks", "impressions", "ctr_pct", "avg_position")}
    metrics = {
        "period_kind": _period_kind(ws, we),
        "label": label,
        "total_extra": totals_extra or None,
        "total_caveats": sorted(p.get("total_caveats", [])) or None,
        "web": web or None,
        **sections,
    }
    if internal or internal_sections:
        metrics["internal_only"] = {
            "note": "INTERNAL ONLY - never client-facing without Julian",
            "web": internal or None,
            **internal_sections,
        }
    return metrics


def _fold_partial_windows(periods):
    """
    A section can cover only part of a period - e.g. gsc_ai_features in May
    2026 runs 05-18..05-31 because the surface launched mid-month. Merge
    such a window into the full period that contains it (the one with
    gsc_web totals), noting its own window on each merged section.
    """
    full = [k for k, p in periods.items() if p["total"]]
    for key in [k for k, p in periods.items() if not p["total"] and not p["pending"]]:
        ws, we = key
        host = next((k for k in full if k[0] <= ws and we <= k[1]), None)
        if not host:
            continue
        for name, s in periods.pop(key)["sections"].items():
            s["window"] = [ws, we]
            periods[host]["sections"].setdefault(name, s)
    return periods


def write_periods(cur, client_id, periods, label):
    n = skipped = 0
    for (ws, we), p in sorted(_fold_partial_windows(periods).items()):
        if not p["total"]:
            skipped += 1  # pending access or no gsc_web totals
            continue
        t = p["total"]
        cur.execute(
            """INSERT INTO gsc_weekly
                   (client_id, week_start, week_end, clicks, impressions, ctr, avg_position, metrics)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (client_id, week_start, week_end) DO UPDATE SET
                   clicks = EXCLUDED.clicks,
                   impressions = EXCLUDED.impressions,
                   ctr = EXCLUDED.ctr,
                   avg_position = EXCLUDED.avg_position,
                   metrics = EXCLUDED.metrics,
                   ingested_at = now()""",
            (client_id, ws, we, t.get("clicks"), t.get("impressions"),
             t.get("ctr_pct"), t.get("avg_position"),
             json.dumps(_to_metrics(p, ws, we, label))),
        )
        n += 1
    return n, skipped


def _client_map(cur):
    cur.execute("SELECT id, slug, name FROM clients")
    m = {}
    for r in cur.fetchall():
        m[r["slug"]] = r["id"]
        m.setdefault(_flat(r["slug"]), r["id"])
        m.setdefault(_flat(r["name"]), r["id"])
    cur.execute("SELECT alias, client_id FROM client_aliases")
    for r in cur.fetchall():
        m.setdefault(_flat(r["alias"]), r["client_id"])
    return m


def _resolve(cmap, folder):
    slug = ALIAS.get(folder, folder)
    return cmap.get(slug) or cmap.get(_flat(slug))


def ingest_csv(cur, cmap, path):
    folder = os.path.basename(os.path.dirname(os.path.abspath(path)))
    cid = _resolve(cmap, folder)
    if not cid:
        return f"skipped {path}: no client for folder '{folder}'"
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        needed = {"source", "metric", "dimension", "value", "window_start", "window_end"}
        missing = needed - set(reader.fieldnames or [])
        if missing:
            return f"skipped {path}: missing columns {sorted(missing)}"
        rows = [(r["source"], r["metric"], r["dimension"], r["value"],
                 r["window_start"], r["window_end"], r.get("caveat", "")) for r in reader]
    label = os.path.splitext(os.path.basename(path))[0]
    n, skipped = write_periods(cur, cid, build_periods(rows), label)
    return f"ok {os.path.basename(path)}: {n} period(s)" + (f", {skipped} pending/empty skipped" if skipped else "")


def ingest_from_db(cur):
    """Backfill from gsc_* sections already in shopify_report_sections."""
    cur.execute(
        """SELECT client_id, report_period, section_name, rows
             FROM shopify_report_sections
            WHERE section_name ~ ' gsc_'
            ORDER BY client_id, report_period""")
    grouped = {}
    for r in cur.fetchall():
        source = r["section_name"].split(" ", 1)[1]
        key = (r["client_id"], r["report_period"])
        for row in r["rows"]:
            metric, dim, val, ws, we, caveat = (list(row) + [""] * 6)[:6]
            grouped.setdefault(key, []).append((source, metric, dim, val, ws, we, caveat))
    msgs, total = [], 0
    for (cid, period), rows in grouped.items():
        n, skipped = write_periods(cur, cid, build_periods(rows), period)
        total += n
        msgs.append(f"client {cid} {period}: {n} period(s)" + (", pending" if skipped else ""))
    return msgs, total


def main():
    ap = argparse.ArgumentParser(description="Load GSC rows into gsc_weekly.")
    ap.add_argument("files", nargs="*", help="pull CSVs (default: monthly-pulls/ + weekly-pulls/)")
    ap.add_argument("--from-db", action="store_true",
                    help="backfill from gsc_* sections in shopify_report_sections")
    args = ap.parse_args()

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            if args.from_db:
                msgs, total = ingest_from_db(cur)
                for m in msgs:
                    print(m)
                print("TOTAL periods written:", total)
                return
            paths = args.files or (
                sorted(glob.glob(os.path.join(HERE, "monthly-pulls", "*", "*.csv")))
                + sorted(glob.glob(os.path.join(HERE, "weekly-pulls", "*", "*.csv"))))
            if not paths:
                print("No CSVs found. Pass file paths, or use --from-db.")
                return
            cmap = _client_map(cur)
            for p in paths:
                print(ingest_csv(cur, cmap, p))


if __name__ == "__main__":
    main()
