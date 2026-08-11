"""Verify dup-prevention: flattened-name matching collapses spelling variants.

Run: python test_dup.py
"""
from ingest_ai_visibility import flatten


def _pg_key(name: str) -> str:
    # Mirrors the SQL predicate: regexp_replace(lower(name), '[^a-z0-9]','','g')
    return flatten(name)


def main():
    # Pairs that must resolve to the SAME client key.
    same = [
        ("Outdoor Vitals", "Outdoorvitals"),
        ("The Swell Score", "Theswellscore"),
        ("Living Well with Dr. Michelle", "Livingwellwithdr.Michelle"),
        ("Outdoor Vitals", "outdoor-vitals"),
    ]
    for a, b in same:
        assert _pg_key(a) == _pg_key(b), f"{a!r} != {b!r}"

    # Distinct clients must NOT collide.
    distinct = [("Rootganic", "Rootganic (Isa Herrera)"), ("Winona", "BabyRx")]
    for a, b in distinct:
        assert _pg_key(a) != _pg_key(b), f"{a!r} == {b!r}"

    print("OK: variants collapse, distinct clients stay distinct")


if __name__ == "__main__":
    main()
