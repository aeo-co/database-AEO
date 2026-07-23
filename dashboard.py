import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from db import get_conn
from ingest_ai_visibility import ingest_file as ingest_ai_file
from ingest_shopify_reports import ingest_file as ingest_shopify_file

app = FastAPI(title="Smart Marketer Data Hub")

# Optional: set UPLOAD_PASSPHRASE in .env before this is reachable on the
# open internet, so uploading isn't wide open to anyone with the URL. If
# it's left unset, uploads work with no passphrase - fine for local use.
UPLOAD_PASSPHRASE = os.getenv("UPLOAD_PASSPHRASE")


def _num(val):
    """Decimal -> float, None stays None, so responses are plain JSON."""
    return float(val) if val is not None else None


@app.get("/api/clients")
def list_clients():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, slug FROM clients ORDER BY name;")
            return cur.fetchall()


@app.get("/api/summary")
def platform_summary(client: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    v.platform,
                    count(*) AS queries_tested,
                    avg(v.visibility_score) AS avg_visibility_score,
                    avg(v.brand_position) AS avg_brand_position
                FROM ai_visibility_checks v
                JOIN clients c ON c.id = v.client_id
                WHERE c.slug = %(slug)s
                GROUP BY v.platform
                ORDER BY v.platform;
                """,
                {"slug": client},
            )
            rows = cur.fetchall()
    for r in rows:
        r["avg_visibility_score"] = round(_num(r["avg_visibility_score"]), 1) if r["avg_visibility_score"] is not None else None
        r["avg_brand_position"] = round(_num(r["avg_brand_position"]), 1) if r["avg_brand_position"] is not None else None
    return rows


@app.get("/api/queries")
def query_detail(client: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    v.platform, v.check_date, v.query_text, v.visibility_score,
                    v.brand_position, v.total_brands, v.mentions,
                    v.raw_output, v.competitor_analysis
                FROM ai_visibility_checks v
                JOIN clients c ON c.id = v.client_id
                WHERE c.slug = %(slug)s
                ORDER BY v.check_date DESC, v.platform;
                """,
                {"slug": client},
            )
            rows = cur.fetchall()
    for r in rows:
        r["check_date"] = r["check_date"].isoformat()
        r["visibility_score"] = round(_num(r["visibility_score"]), 1) if r["visibility_score"] is not None else None
        r["brand_position"] = round(_num(r["brand_position"]), 1) if r["brand_position"] is not None else None
    return rows


@app.get("/api/mentions")
def top_mentions(client: str, limit: int = 6):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM clients WHERE slug = %(slug)s;", {"slug": client})
            client_row = cur.fetchone()
            if not client_row:
                return []
            cur.execute(
                """
                SELECT v.mentions
                FROM ai_visibility_checks v
                JOIN clients c ON c.id = v.client_id
                WHERE c.slug = %(slug)s;
                """,
                {"slug": client},
            )
            rows = cur.fetchall()

    own_name = client_row["name"].strip().lower()
    counts = Counter()
    for r in rows:
        for mention in (r["mentions"] or []):
            name = mention.strip()
            if not name or own_name in name.lower():
                continue
            counts[name] += 1

    return [{"name": name, "count": count} for name, count in counts.most_common(limit)]


@app.post("/api/upload")
async def upload_files(files: list[UploadFile] = File(...), passphrase: str = Form("")):
    if UPLOAD_PASSPHRASE and passphrase != UPLOAD_PASSPHRASE:
        raise HTTPException(status_code=401, detail="Wrong passphrase.")

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for f in files:
            # Keep the original filename - it's how both ingest_file()
            # functions identify the client/platform/date from the name.
            # Renaming would break parsing.
            dest = tmp_path / Path(f.filename).name
            with open(dest, "wb") as out:
                shutil.copyfileobj(f.file, out)
            # Dispatch on extension: AI visibility uses .xlsx, shopify
            # reports use .csv (named '{client}-all-data.csv'). Anything
            # else gets skipped with a clear reason.
            ext = dest.suffix.lower()
            if ext == ".xlsx":
                results.append(ingest_ai_file(dest))
            elif ext == ".csv":
                results.append(ingest_shopify_file(dest))
            else:
                results.append({
                    "filename": dest.name,
                    "status": "skipped",
                    "reason": f"unsupported extension '{ext}' (use .xlsx for AI visibility or .csv for shopify reports)",
                })
    return results


# Static frontend - must be mounted last so /api/* routes above take priority.
app.mount("/", StaticFiles(directory=Path(__file__).parent, html=True), name="static")


@app.get("/upload")
def upload_page():
    return FileResponse(Path(__file__).parent / "upload.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("dashboard:app", host="127.0.0.1", port=8000)
