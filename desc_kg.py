"""Describe kg_nodes / kg_edges live schema (run from repo dir with app venv)."""
from db import get_conn

with get_conn() as cn:
    for t in ("kg_nodes", "kg_edges"):
        print("===", t)
        rows = cn.execute(
            """SELECT a.attname AS column_name, format_type(a.atttypid, a.atttypmod) AS typ,
                      a.attnotnull, pg_get_expr(d.adbin, d.adrelid) AS dflt
               FROM pg_attribute a
               JOIN pg_class c ON a.attrelid = c.oid
               LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
               WHERE c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
               ORDER BY a.attnum""",
            (t,),
        ).fetchall()
        for r in rows:
            print("  {:14} {:22} {:8} {}".format(
                r["column_name"], r["typ"],
                "NOT NULL" if r["attnotnull"] else "", r["dflt"] or ""))
        for r in cn.execute(
                "SELECT indexdef FROM pg_indexes WHERE tablename = %s", (t,)):
            print("  IDX:", r["indexdef"])
        for r in cn.execute(
                "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conrelid = %s::regclass",
                (t,)):
            print("  CONSTRAINT:", r["conname"], r["def"])
