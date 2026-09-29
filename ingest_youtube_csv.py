"""
ingest_youtube_csv.py - load the YouTube comment tool's CSV export into Postgres.

    python ingest_youtube_csv.py /path/to/folder
    python ingest_youtube_csv.py rootganic-youtube-comments.csv

Client is parsed from the filename ('{client}-youtube-comments.csv' - same
convention the web upload box uses), so a folder can hold files for many
different clients at once. Pass --client to override that for every file
in the run instead, e.g. for a file that isn't named that way:

    python ingest_youtube_csv.py one-file.csv --client rootganic

Expects the 12-column export:
    video_id, video_title, video_url, video_published_at,
    comment_id, parent_comment_id, comment_author, author_channel_id,
    comment_likes, comment_published_at, total_reply_count, comment_text

Re-running is safe: comment_id is YouTube's own stable id, so a second
run updates like counts instead of inserting duplicates. Unlike a content
hash, this also survives someone editing their comment.

Nothing is filtered on like count. 69% of comments (and 72% of questions)
in real exports have zero likes - filtering here would throw away most of
what the research is for. Gate the expensive model calls instead.
"""

import argparse
import csv
import re
import sys
from pathlib import Path

from db import get_conn
from ingest_ai_visibility import get_or_create_client, slugify

# Web-upload convention (see upload.html): '{client}-youtube-comments.csv'.
# The CLI --client flag still works and takes priority when given, so
# this only matters for files ingested without one.
FILENAME_RE = re.compile(r"^(?P<client>.+?)-youtube-comments\.csv$", re.IGNORECASE)

EXPECTED = {
    "video_id", "video_title", "video_url", "video_published_at",
    "comment_id", "parent_comment_id", "comment_author", "author_channel_id",
    "comment_likes", "comment_published_at", "total_reply_count", "comment_text",
}


def _clean(v):
    """Empty strings from CSV should be NULL, not ''."""
    if v is None:
        return None
    v = v.strip()
    return v or None


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def ingest_file(path: Path, client_slug: str = None) -> dict:
    if client_slug:
        client_name = client_slug
    else:
        m = FILENAME_RE.match(path.name)
        if not m:
            return {
                "file": path.name,
                "status": "skipped",
                "reason": "name doesn't match '{client}-youtube-comments.csv' and no --client given",
            }
        client_name = m.group("client").replace("-", " ").replace("_", " ").strip().title()

    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, UnicodeDecodeError) as e:
        return {"file": path.name, "status": "skipped", "reason": f"unreadable: {e}"}

    if not rows:
        return {"file": path.name, "status": "skipped", "reason": "no rows"}

    missing = EXPECTED - set(rows[0].keys())
    if missing:
        return {
            "file": path.name,
            "status": "skipped",
            "reason": f"missing columns: {', '.join(sorted(missing))}",
        }

    with get_conn() as conn:
        with conn.cursor() as cur:
            client_id = get_or_create_client(cur, client_name)

            # one file may hold several videos; register each once
            source_ids = {}
            for r in rows:
                vid = _clean(r["video_id"])
                if not vid or vid in source_ids:
                    continue
                cur.execute(
                    """
                    insert into research_sources
                        (client_id, platform, external_id, url, title,
                         published_at, fetched_at)
                    values (%(cid)s, 'youtube', %(vid)s, %(url)s, %(title)s,
                            %(pub)s::timestamptz, now())
                    on conflict (platform, external_id) do update
                        set title        = excluded.title,
                            url          = excluded.url,
                            published_at = excluded.published_at,
                            fetched_at   = now()
                    returning id
                    """,
                    {
                        "cid": client_id,
                        "vid": vid,
                        "url": _clean(r["video_url"]),
                        "title": _clean(r["video_title"]),
                        "pub": _clean(r["video_published_at"]),
                    },
                )
                source_ids[vid] = cur.fetchone()["id"]

            loaded = skipped = 0
            top_level = replies = 0

            for r in rows:
                comment_id = _clean(r["comment_id"])
                body = r.get("comment_text")
                if not comment_id or not (body or "").strip():
                    skipped += 1
                    continue

                parent = _clean(r["parent_comment_id"])
                if parent:
                    replies += 1
                else:
                    top_level += 1

                cur.execute(
                    """
                    insert into research_comments
                        (source_id, client_id, platform, external_id,
                         parent_external_id, author, author_id, body, score,
                         reply_count, posted_at, raw)
                    values (%(sid)s, %(cid)s, 'youtube', %(ext)s, %(parent)s,
                            %(author)s, %(author_id)s, %(body)s, %(score)s,
                            %(replies)s, %(posted)s::timestamptz, '{}')
                    on conflict (platform, external_id) do update
                        set parent_external_id = excluded.parent_external_id,
                            author             = excluded.author,
                            author_id          = excluded.author_id,
                            body               = excluded.body,
                            score              = excluded.score,
                            reply_count        = excluded.reply_count,
                            posted_at          = excluded.posted_at,
                            fetched_at         = now()
                    """,
                    {
                        "sid": source_ids[_clean(r["video_id"])],
                        "cid": client_id,
                        "ext": comment_id,
                        "parent": parent,
                        "author": _clean(r["comment_author"]),
                        "author_id": _clean(r["author_channel_id"]),
                        "body": body.strip(),
                        "score": _int(r["comment_likes"]),
                        "replies": _int(r["total_reply_count"]),
                        "posted": _clean(r["comment_published_at"]),
                    },
                )
                loaded += 1

    return {
        "file": path.name,
        "status": "ok",
        "client": client_name,
        "client_slug": slugify(client_name),
        "videos": len(source_ids),
        "loaded": loaded,
        "top_level": top_level,
        "replies": replies,
        "skipped": skipped,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="a .csv file, or a folder of them")
    ap.add_argument(
        "--client", default=None,
        help="client slug/name - overrides the filename for every file in this run. "
             "Omit to parse each file's client from '{client}-youtube-comments.csv'.",
    )
    args = ap.parse_args()

    p = Path(args.path)
    files = sorted(p.glob("*.csv")) if p.is_dir() else [p]
    if not files:
        print(f"No .csv files found at {p}")
        sys.exit(1)

    total = 0
    clients_seen = set()
    for f in files:
        r = ingest_file(f, args.client)
        if r["status"] == "ok":
            total += r["loaded"]
            clients_seen.add(r["client"])
            print(f"  OK   {r['file']} -> client='{r['client']}': {r['loaded']} comments "
                  f"({r['top_level']} top-level, {r['replies']} replies) "
                  f"from {r['videos']} video(s)"
                  + (f", {r['skipped']} skipped" if r["skipped"] else ""))
        else:
            print(f"  SKIP {r['file']}: {r['reason']}")
    print(f"\n{total} comments loaded across {len(clients_seen)} client(s): {', '.join(sorted(clients_seen)) or '(none)'}")


if __name__ == "__main__":
    main()
