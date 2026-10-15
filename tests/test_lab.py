"""Phase 1: what the database itself guarantees for the AI's login (see README.md, "The tests").

Exact error texts and findings go to tests/out/*.json: the video quotes them from there.
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time

import psycopg
import pytest

HOST = os.environ.get("PGHOST", "db")
ADMIN = dict(host=HOST, user="postgres", password=os.environ.get("POSTGRES_PASSWORD", "lab"))
AI = dict(host=HOST, user="ai_reader", password="ai_reader_lab", dbname="reporting")
NAIVE = dict(host=HOST, user="naive_reader", password="naive_reader_lab", dbname="naive")
OUT = pathlib.Path(__file__).parent / "out"
ROOT = pathlib.Path(__file__).parent.parent

ERRORS: dict[str, str] = {}
FINDINGS: dict[str, str] = {}


def connect(params, app="lab-tests", **kw):
    return psycopg.connect(**params, application_name=app, autocommit=True, **kw)


def error_of(params, sql, app="lab-tests"):
    """Run sql in a fresh session; return the error text (or None if it worked)."""
    with connect(params, app) as c:
        try:
            c.execute(sql)
            return None
        except psycopg.Error as e:
            return str(e).strip().splitlines()[0]


@pytest.fixture(scope="session", autouse=True)
def refreshed():
    """The refresh job runs first, as it would on a schedule."""
    for target in ("reporting", "naive"):
        subprocess.run([sys.executable, str(ROOT / "refresh" / "refresh.py"), target], check=True)
    yield
    OUT.mkdir(exist_ok=True)
    (OUT / "errors.json").write_text(json.dumps(ERRORS, indent=2) + "\n")
    (OUT / "findings.json").write_text(json.dumps(FINDINGS, indent=2) + "\n")


# 1
def test_ai_reader_can_select_reporting_tables():
    with connect(AI) as c:
        n = c.execute("SELECT count(*) FROM address_checks").fetchone()[0]
        top = c.execute(
            "SELECT c.name, count(*) FROM address_checks a JOIN customers c ON c.id = a.customer_id "
            "GROUP BY c.name ORDER BY 2 DESC, 1 LIMIT 1").fetchone()
    assert n > 0 and top[1] > 0


# 2
def test_ai_reader_cannot_connect_to_live():
    with pytest.raises(psycopg.OperationalError) as e:
        connect({**AI, "dbname": "live"})
    ERRORS["2_connect_live"] = next(l for l in str(e.value).splitlines() if "permission denied" in l).strip()
    assert "permission denied for database" in str(e.value)


# 3
@pytest.mark.parametrize("name,sql", [
    ("delete", "DELETE FROM address_checks"),
    ("update", "UPDATE customers SET status = 'closed'"),
    ("insert", "INSERT INTO invoices (id, customer_id, amount_cents) VALUES (999, 1, 1)"),
    ("drop", "DROP TABLE address_checks"),
])
def test_writes_are_refused(name, sql):
    # the transaction is opened read-write, as an attacker would, so the privilege is what refuses it
    err = error_of(AI, "BEGIN READ WRITE; " + sql)
    ERRORS[f"3_{name}"] = err
    assert err and (("permission denied" in err) or ("must be owner" in err))


# 4
def test_commit_then_delete_in_one_string_is_refused():
    as_sent = error_of(AI, "COMMIT; DELETE FROM address_checks")
    ERRORS["4_commit_delete"] = as_sent
    assert as_sent and "read-only transaction" in as_sent
    escaped = error_of(AI, "COMMIT; BEGIN READ WRITE; DELETE FROM address_checks")
    ERRORS["4_commit_set_delete"] = escaped
    assert escaped and "permission denied for table address_checks" in escaped
    # ...but the session setting itself did change: privileges protect data, not session settings
    with connect(AI) as c:
        c.execute("COMMIT; SET statement_timeout = 0")
        assert c.execute("SHOW statement_timeout").fetchone()[0] == "0"


# 5
def test_email_is_not_in_reporting():
    err = error_of(AI, "SELECT email FROM customers")
    ERRORS["5_email"] = err
    assert err and 'column "email" does not exist' in err


# 6
def test_public_defaults_naive_vs_hardened():
    naive_temp = error_of(NAIVE, "BEGIN READ WRITE; CREATE TEMP TABLE scratch (x int)")
    naive_fn = error_of(NAIVE, "SELECT checks_per_customer(1)")
    ai_temp = error_of(AI, "BEGIN READ WRITE; CREATE TEMP TABLE scratch (x int)")
    ai_fn = error_of(AI, "SELECT checks_per_customer(1)")
    ERRORS.update({"6_ai_temp": ai_temp, "6_ai_function": ai_fn})
    assert naive_temp is None and naive_fn is None, "the naive role should show PUBLIC's defaults"
    assert ai_temp and "permission denied to create temporary tables" in ai_temp
    assert ai_fn and "permission denied for function checks_per_customer" in ai_fn
    # large objects: PUBLIC has no large-object privilege, but it can EXECUTE the built-in lo_* functions
    naive_lo = error_of(NAIVE, "BEGIN READ WRITE; SELECT lo_create(0)")
    ai_lo = error_of(AI, "BEGIN READ WRITE; SELECT lo_create(0)")
    FINDINGS["6_lo_create_naive_reader"] = naive_lo or "worked"
    ERRORS["6_ai_lo_create"] = ai_lo
    assert naive_lo is None, "the naive role should show PUBLIC's EXECUTE on lo_create"
    assert ai_lo and "permission denied for function lo_create" in ai_lo


# 7
def test_no_set_role_and_no_server_file_roles():
    err = error_of(AI, "SET ROLE postgres")
    ERRORS["7_set_role"] = err
    assert err and "permission denied to set role" in err
    with connect(AI) as c:
        for r in ("pg_read_server_files", "pg_write_server_files", "pg_execute_server_program", "pg_read_all_data"):
            assert c.execute("SELECT pg_has_role('ai_reader', %s, 'USAGE')", (r,)).fetchone()[0] is False, r


# 8
def test_role_timeout_cancels_but_session_can_undo_it():
    err = error_of(AI, "SELECT pg_sleep(8)")
    ERRORS["8_timeout"] = err
    assert err and "canceling statement due to statement timeout" in err
    with connect(AI) as c:
        c.execute("SET statement_timeout = 0")
        t = time.monotonic()
        c.execute("SELECT pg_sleep(6)")
        assert time.monotonic() - t >= 6


# 10
def test_statements_reach_the_log_with_role_and_app():
    marker = f"lab_marker_{int(time.time())}"
    with connect(AI, app="lab-test-10") as c:
        c.execute(f"SELECT '{marker}' AS m, count(*) FROM customers")
    time.sleep(1)
    with connect({**ADMIN, "dbname": "postgres"}) as c:
        log = c.execute("SELECT pg_read_file('log/postgresql.log')").fetchone()[0]
    lines = [l for l in log.splitlines() if marker in l]
    assert lines, "statement not found in the log"
    FINDINGS["10_log_line"] = lines[0]
    assert "user=ai_reader" in lines[0] and "app=lab-test-10" in lines[0]


# 11
def test_refresh_moves_approved_columns_only():
    with connect(AI) as c:
        started = c.execute("SELECT refresh_started_at FROM refresh_info").fetchone()[0]
        cols = {r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'reporting'")}
    assert started is not None
    assert "email" not in cols
    src = (ROOT / "refresh" / "refresh.py").read_text()
    approved = re.search(r"APPROVED = \{(.*?)\n\}", src, re.S).group(1)
    assert "email" not in approved


# 12
def test_catalog_shows_the_hardening():
    with connect({**ADMIN, "dbname": "reporting"}) as c:
        r = c.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolconnlimit, "
                      "rolconfig FROM pg_roles WHERE rolname = 'ai_reader'").fetchone()
        assert r[:5] == (False, False, False, False, False) and r[5] > 0
        config = dict(x.split("=", 1) for x in r[6])
        assert config["search_path"] == "reporting, pg_catalog, pg_temp"
        assert config["default_transaction_read_only"] == "on"
        assert c.execute("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                         "WHERE r.rolname = 'ai_reader'").fetchone()[0] == 0
        definer = c.execute(
            "SELECT p.oid::regprocedure::text FROM pg_proc p WHERE p.prosecdef "
            "AND has_function_privilege('ai_reader', p.oid, 'EXECUTE')").fetchall()
        assert definer == [], definer
        assert c.execute("SELECT has_schema_privilege('public', 'public', 'CREATE')").fetchone()[0] is False
        assert c.execute("SELECT has_database_privilege('public', 'reporting', 'TEMPORARY')").fetchone()[0] is False
        assert c.execute("SELECT has_database_privilege('public', 'reporting', 'CONNECT')").fetchone()[0] is False


# 13
def test_definer_function_after_read_only_is_switched_off():
    """The escape chapter's condition: with read-only switched off, a SECURITY DEFINER function that deletes is a way
    around the missing DELETE privilege, unless EXECUTE was revoked from PUBLIC (the hardening)."""
    old_row = ("INSERT INTO reporting.address_checks VALUES "
               "(9001, 1, '1 Old Rd', 'X', timestamptz '2019-01-01') ON CONFLICT (id) DO NOTHING")
    count_old = "SELECT count(*) FROM reporting.address_checks WHERE id = 9001"
    for db in ("naive", "reporting"):
        with connect({**ADMIN, "dbname": db}) as c:
            c.execute(old_row)
    # before: PUBLIC's default EXECUTE lets the plain SELECT-only role delete through the owner's function
    with connect(NAIVE) as c:
        c.execute("SET default_transaction_read_only = off")
        c.execute("SELECT purge_old_checks()")
    with connect({**ADMIN, "dbname": "naive"}) as c:
        naive_left = c.execute(count_old).fetchone()[0]
    FINDINGS["13_naive_purge"] = "deleted" if naive_left == 0 else "not deleted"
    assert naive_left == 0, "the naive role should delete through the SECURITY DEFINER function"
    # after: the hardened login can switch read-only off, but can't run the function
    with connect(AI) as c:
        c.execute("SET default_transaction_read_only = off")
        try:
            c.execute("SELECT purge_old_checks()")
            err = None
        except psycopg.Error as e:
            err = str(e).strip().splitlines()[0]
    ERRORS["13_ai_purge"] = err
    with connect({**ADMIN, "dbname": "reporting"}) as c:
        assert c.execute(count_old).fetchone()[0] == 1, "the hardened login must not delete"
    assert err and "permission denied for function purge_old_checks" in err
