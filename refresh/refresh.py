"""Refresh a reporting database from live, approved columns only.

One REPEATABLE READ READ ONLY transaction on live, so every table comes from the same moment (the snapshot is taken
at the first query, not at BEGIN). Each table is copied with COPY (SELECT <approved columns>) TO STDOUT straight into
COPY ... FROM STDIN on the target: a column that isn't listed below never leaves live, not even into a file.
refresh_started_at records when this script started, not the exact snapshot point.

Usage: python refresh/refresh.py <reporting|naive>
"""
import os
import sys

import psycopg

APPROVED = {
    "customers": ["id", "name", "status"],
    "address_checks": ["id", "customer_id", "address", "flood_zone", "checked_at"],
    "invoices": ["id", "customer_id", "amount_cents"],
    "support_tickets": ["id", "customer_id", "body"],
}

ADMIN = dict(host=os.environ.get("PGHOST", "db"), user="postgres", password=os.environ.get("POSTGRES_PASSWORD", "lab"))


def refresh(target: str) -> None:
    with psycopg.connect(**ADMIN, dbname="live", application_name="refresh") as src, \
         psycopg.connect(**ADMIN, dbname=target, application_name="refresh") as dst:
        src.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        src.read_only = True
        with dst.transaction():
            dst.execute("TRUNCATE reporting.refresh_info")
            dst.execute("INSERT INTO reporting.refresh_info VALUES (clock_timestamp())")
            for table, cols in APPROVED.items():
                col_list = ", ".join(cols)
                dst.execute(f"TRUNCATE reporting.{table}")
                with src.cursor().copy(f"COPY (SELECT {col_list} FROM {table}) TO STDOUT") as out, \
                     dst.cursor().copy(f"COPY reporting.{table} ({col_list}) FROM STDIN") as into:
                    for block in out:
                        into.write(block)
        src.commit()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("reporting", "naive"):
        sys.exit("usage: refresh.py <reporting|naive>")
    refresh(sys.argv[1])
    print(f"refreshed {sys.argv[1]}")
