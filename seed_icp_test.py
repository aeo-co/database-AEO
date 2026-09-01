"""Ingest a few real ICP docs so refresh_similar_edges has something to compare."""
from client_context import upsert_client_context
from db import get_conn

DOCS = {
    "outdoor-vitals": """# ICP — Outdoor Vitals
Target: ultralight backpackers and thru-hikers, ages 25-45. Buyers of down
quilts, wind shells, and 15-25L day packs. Price-sensitive but quality-driven.
Community: r/Ultralight, YouTube gear reviews. DTC ecommerce.""",
    "groove-life": """# ICP — Groove Life
Target: active men 25-45 who wear silicone rings: climbers, CrossFitters,
hunters, tradesmen. Values durability and safety over looks. Mid-ticket
($35-60) DTC with strong lifetime-value via ring bands and accessories.""",
    "micro-matic": """# ICP — Micro Matic
Target: B2B — bars, breweries, restaurants needing draft beer equipment
and dispense technical parts. Purchasing managers and installers. Longer
sales cycle, quote-driven, high order values.""",
    "winona": """# ICP — Winona
Target: women 40-65 seeking hormone-related telehealth treatment (HRT,
perimenopause). Direct-to-consumer subscription, recurring refills,
compliance-sensitive health marketing.""",
}

with get_conn() as cn:
    clients = {r["slug"]: r["id"] for r in
               cn.execute("select slug, id from clients").fetchall()}

for slug, doc in DOCS.items():
    cid = clients[slug]
    r = upsert_client_context(cid, "icp", doc, title=f"{slug} ICP",
                              metadata={"source": "kg_rollout_test"})
    print("ingested:", slug, "client_id", cid, "-> context id", r)
