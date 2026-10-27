#!/usr/bin/env python3
"""
ingest_shopify_ai.py — ingest Shopify AI-sales rows (shopify_ai,
shopify_ai_prior_year, shopify_ai_product) from weekly/monthly pull CSVs
into the shopify_weekly table.

Run: cd /root/database-AEO && ./venv/bin/python ingest_shopify_ai.py
     or pass specific file paths.

Row format: client,month,source,metric,dimension,value,window_start,window_end,caveat
Sources: shopify_ai (channel totals), shopify_ai_prior_year (directional),
shopify_ai_product (variant-level rows).

shopify_weekly: one row per (client, week_start):
  orders, revenue = canonical ChatGPT agentic totals
  metrics JSON = {total_sales_usd, excluded_artifact_sales_usd,
                  total_sales_ex_exclusions_usd, prior_year{...}, products[...], caveats}
Idempotent: delete+insert per (client_id, week_start).
"""
import sys, csv, json, os, glob
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
    weeks = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            src = r["source"].strip()
            if not src.startswith("shopify"):
                continue
            ws = r["window_start"].strip()
            if not ws:
                continue
            metric, dim, val = r["metric"].strip(), r["dimension"].strip(), r["value"].strip()
            caveat = r["window_end"].strip()
            caveat = r["caveat"].strip()
            we = r["window_end"].strip()
            weeks.setdefault(ws, {"week_end": we, "ai": {}, "py": {}, "products": {}, "caveats": []})
            w = weeks[ws]
            if caveat and caveat not in w["caveats"]:
                w["caveats"].append(caveat)
            if src == "shopify_ai":
                w["ai"][metric] = val
            elif src == "shopify_ai_prior_year":
                w["py"][metric] = val
            elif src == "shopify_ai_product":
                w["products"].setdefault(dim, {})[metric] = val
            # plain 'shopify' source = status row (NO-SHOPIFY) — nothing numeric
    n = 0
    with db.get_conn() as conn:
        with conn.cursor() as c:
            for ws, w in weeks.items():
                orders = _num(w["ai"].get("orders"))
                revenue = _num(w["ai"].get("total_sales_usd"))
                metrics = {
                    "total_sales_usd": _num(w["ai"].get("total_sales_usd")),
                    "excluded_artifact_sales_usd": _num(w["ai"].get("excluded_artifact_sales_usd")),
                    "total_sales_ex_exclusions_usd": _num(w["ai"].get("total_sales_ex_exclusions_usd")),
                    "net_items_sold": _num(w["ai"].get("net_items_sold")),
                    "prior_year": {k: _num(v) for k, v in w["py"].items()} or None,
                    "products": [
                        {"product": p, **{k: _num(v) for k, v in vals.items()}}
                        for p, vals in w["products"].items()
                    ] or None,
                    "caveats": w["caveats"] or None,
                    "no_shopify": not w["ai"] and not w["products"],
                }
                c.execute("DELETE FROM shopify_weekly WHERE client_id=%s AND week_start=%s", (cid, ws))
                c.execute("""INSERT INTO shopify_weekly (client_id, week_start, week_end, orders, revenue, metrics)
                             VALUES (%s,%s,%s,%s,%s,%s)""",
                          (cid, ws, w["week_end"], orders, revenue, json.dumps(metrics)))
                n += 1
        conn.commit()
    return (path, None, n)

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    paths = sys.argv[1:] or (sorted(glob.glob(os.path.join(here, "weekly-pulls", "*", "*.csv")))
                             + sorted(glob.glob(os.path.join(here, "monthly-pulls", "*", "*.csv"))))
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
    print("TOTAL shopify_weekly rows:", total)
    for e in errs: print("ERR:", e)
    with db.get_conn() as conn:
        with conn.cursor() as c:
            c.execute("""SELECT cl.name, count(*) n, sum(s.revenue) rev, sum(s.orders) ord
                         FROM shopify_weekly s JOIN clients cl ON cl.id=s.client_id
                         GROUP BY cl.name ORDER BY cl.name""")
            for r in c.fetchall():
                print(f"  {r['name']}: {r['n']} periods, revenue={r['rev']}, orders={r['ord']}")

if __name__ == "__main__":
    main()
