import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
    "dbname": os.getenv("DB_NAME", "smartmarketer"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


@contextmanager
def get_conn():
    """
    Single place every DB connection comes from.

    If DATABASE_URL is set (e.g. a DigitalOcean managed Postgres connection
    string), that's used directly. Otherwise falls back to the individual
    DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD vars, which is what
    you'd use for a self-installed Postgres on a plain Droplet, or for
    local testing.
    """
    if DATABASE_URL:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    else:
        conn = psycopg.connect(**DB_CONFIG, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
