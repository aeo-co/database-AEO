from db import get_conn


def seed_clients():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO clients (name, slug) VALUES
                    ('Acme Co', 'acme-co'),
                    ('Example Client', 'example-client')
                ON CONFLICT (slug) DO NOTHING;
                """
            )
    print("Seeded example clients.")


if __name__ == "__main__":
    seed_clients()
