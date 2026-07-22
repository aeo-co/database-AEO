from pathlib import Path

from db import get_conn


def run_migration():
    schema_path = Path(__file__).parent / "schema.sql"
    sql = schema_path.read_text()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    print("Schema applied.")


if __name__ == "__main__":
    run_migration()
