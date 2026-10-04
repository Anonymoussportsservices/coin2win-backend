import argparse
import json
import re
import sys
import psycopg2

def connect(dsn):
    if not dsn or not dsn.strip():
        raise RuntimeError("Explicit PostgreSQL DSN is required")
    return psycopg2.connect(dsn)

def fetch_rows(cur, sql):
    cur.execute(sql)
    return cur.fetchall()

def snapshot_tables(cur):
    rows = fetch_rows(cur, """
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename <> 'alembic_version'
        ORDER BY tablename
    """)
    return [row[0] for row in rows]

def snapshot_columns(cur):
    rows = fetch_rows(cur, """
        SELECT table_name,
               column_name,
               data_type,
               udt_name,
               is_nullable,
               column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name <> 'alembic_version'
        ORDER BY table_name, column_name
    """)
    return [
        {
            "table": row[0],
            "column": row[1],
            "data_type": row[2],
            "udt_name": row[3],
            "nullable": row[4],
            "default": row[5],
        }
        for row in rows
    ]

def normalize_check_definition(value):
    if value is None:
        return None
    value = re.sub(r"::character varying::text", "::character varying", value)
    value = re.sub(r"(ARRAY\[.*?\])::text\[\]", r"\1", value)
    return value

def snapshot_constraints(cur):
    rows = fetch_rows(cur, """
        SELECT
            rel.relname AS table_name,
            con.conname,
            con.contype,
            COALESCE(
                ARRAY(
                    SELECT att.attname
                    FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                    JOIN pg_attribute att
                      ON att.attrelid = con.conrelid
                     AND att.attnum = k.attnum
                    ORDER BY k.ord
                ),
                ARRAY[]::name[]
            ) AS columns,
            CASE
                WHEN con.contype = 'f' THEN frel.relname
                ELSE NULL
            END AS referenced_table,
            CASE
                WHEN con.contype = 'f' THEN ARRAY(
                    SELECT att.attname
                    FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord)
                    JOIN pg_attribute att
                      ON att.attrelid = con.confrelid
                     AND att.attnum = k.attnum
                    ORDER BY k.ord
                )
                ELSE ARRAY[]::name[]
            END AS referenced_columns,
            CASE
                WHEN con.contype = 'c'
                THEN pg_get_constraintdef(con.oid, true)
                ELSE NULL
            END AS check_definition
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = rel.relnamespace
        LEFT JOIN pg_class frel ON frel.oid = con.confrelid
        WHERE n.nspname = 'public'
          AND rel.relname <> 'alembic_version'
          AND con.contype IN ('p','u','f','c')
        ORDER BY rel.relname, con.conname
    """)

    return [
        {
            "table": row[0],
            "name": row[1],
            "type": row[2],
            "columns": list(row[3]),
            "referenced_table": row[4],
            "referenced_columns": list(row[5]),
            "check_definition": normalize_check_definition(row[6]),
        }
        for row in rows
    ]

def snapshot_indexes(cur):
    rows = fetch_rows(cur, """
        SELECT tablename, indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename <> 'alembic_version'
        ORDER BY tablename, indexname
    """)
    return [
        {
            "table": row[0],
            "name": row[1],
            "definition": row[2],
        }
        for row in rows
    ]

def snapshot_sequences(cur):
    rows = fetch_rows(cur, """
        SELECT sequencename,
               data_type,
               start_value,
               min_value,
               max_value,
               increment_by,
               cycle
        FROM pg_sequences
        WHERE schemaname = 'public'
        ORDER BY sequencename
    """)
    return [
        {
            "name": row[0],
            "data_type": row[1],
            "start": row[2],
            "min": row[3],
            "max": row[4],
            "increment": row[5],
            "cycle": row[6],
        }
        for row in rows
    ]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--compare-to")
    args = parser.parse_args()

    conn = connect(args.dsn)
    try:
        conn.set_session(readonly=True, autocommit=False)

        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            actual_database = cur.fetchone()[0]

            if actual_database != args.expected_database:
                raise RuntimeError(
                    f"Database identity mismatch: expected "
                    f"{args.expected_database!r}, got {actual_database!r}"
                )

            snapshot = {
                "schema_version": 1,
                "database": actual_database,
                "tables": snapshot_tables(cur),
                "columns": snapshot_columns(cur),
                "constraints": snapshot_constraints(cur),
                "indexes": snapshot_indexes(cur),
                "sequences": snapshot_sequences(cur),
            }

        conn.rollback()

        if args.compare_to:
            with open(args.compare_to, "r", encoding="utf-8") as handle:
                expected = json.load(handle)

            differences = compare_snapshots(expected, snapshot)
            if differences:
                print("SCHEMA_DRIFT=" + ",".join(differences), file=sys.stderr)
                return 1

            print("SCHEMA_VALIDATION=PASS")
            return 0

        print(json.dumps(snapshot, indent=2, sort_keys=True))
        return 0

    finally:
        conn.close()


def compare_snapshots(expected, actual):
    sections = ("tables", "columns", "constraints", "indexes", "sequences")
    differences = []

    for section in sections:
        if expected.get(section) != actual.get(section):
            differences.append(section)

    return differences


if __name__ == "__main__":
    sys.exit(main())
