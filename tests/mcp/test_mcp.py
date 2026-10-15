"""Phase 2: the same requests through real MCP servers over stdio, as an MCP client (Claude Code, Claude Desktop) sends them.

- Postgres MCP Pro 0.3.0, --access-mode=restricted: rejects writes and COMMIT before the database (nothing in the log).
- Google MCP Toolbox 1.14.0, --prebuilt=postgres: no read-only switch, so a DELETE reaches PostgreSQL, which refuses it.
Both log in as ai_reader. Answers, error texts and tool annotations go to tests/out/mcp.json.
"""
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import time

import psycopg
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HOST = os.environ.get("PGHOST", "db")
ADMIN = dict(host=HOST, user="postgres", password=os.environ.get("POSTGRES_PASSWORD", "lab"))
ROOT = pathlib.Path(__file__).parent.parent.parent
OUT = ROOT / "tests" / "out"
FOUND: dict[str, object] = {}

PRO = StdioServerParameters(
    command="postgres-mcp",
    args=["--access-mode=restricted", f"postgresql://ai_reader:ai_reader_lab@{HOST}:5432/reporting?application_name=mcp-pro"],
    env={**os.environ},
)
TOOLBOX = StdioServerParameters(
    command="npx",
    args=["--no-install", "toolbox", "--prebuilt", "postgres", "--stdio", "--disable-version-check"],
    env={**os.environ, "POSTGRES_HOST": HOST, "POSTGRES_PORT": "5432", "POSTGRES_DATABASE": "reporting",
         "POSTGRES_USER": "ai_reader", "POSTGRES_PASSWORD": "ai_reader_lab"},
)


async def _session(server, sql=None):
    async with stdio_client(server) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            if sql is None:
                return tools, None
            tool = next(t for t in tools if t.name == "execute_sql")
            arg = next(iter(tool.inputSchema.get("properties", {})))  # the one SQL parameter, whatever it's called
            res = await s.call_tool("execute_sql", {arg: sql})
            text = " ".join(c.text for c in res.content if getattr(c, "type", "") == "text")
            return tools, (bool(res.isError), text)


def run(server, sql=None):
    return asyncio.run(_session(server, sql))


def count_checks():
    with psycopg.connect(**ADMIN, dbname="reporting", autocommit=True) as c:
        return c.execute("SELECT count(*) FROM reporting.address_checks").fetchone()[0]


def log_has(marker):
    time.sleep(1)
    with psycopg.connect(**ADMIN, dbname="postgres", autocommit=True) as c:
        log = c.execute("SELECT pg_read_file('log/postgresql.log')").fetchone()[0]
    return [l for l in log.splitlines() if marker in l]


def refused(answer):
    """An MCP answer that says no: an error result, or an error message in the text."""
    is_error, text = answer
    low = text.lower()
    return is_error or any(w in low for w in ("error", "denied", "not allowed", "read-only", "rejected", "cannot"))


@pytest.fixture(scope="session", autouse=True)
def refreshed():
    subprocess.run([sys.executable, str(ROOT / "refresh" / "refresh.py"), "reporting"], check=True)
    yield
    OUT.mkdir(exist_ok=True)
    (OUT / "mcp.json").write_text(json.dumps(FOUND, indent=2, default=str) + "\n")


@pytest.mark.parametrize("name,server", [("pro", PRO), ("toolbox", TOOLBOX)])
def test_both_servers_offer_execute_sql(name, server):
    tools, _ = run(server)
    FOUND[f"{name}_tools"] = {t.name: (t.annotations.model_dump(exclude_none=True) if t.annotations else None) for t in tools}
    assert "execute_sql" in [t.name for t in tools]


@pytest.mark.parametrize("name,server", [("pro", PRO), ("toolbox", TOOLBOX)])
def test_select_works_through_both(name, server):
    _, answer = run(server, "SELECT count(*) AS checks FROM address_checks")
    FOUND[f"{name}_select"] = answer
    assert not refused(answer) and "120" in answer[1]


def test_pro_restricted_rejects_delete_before_the_database():
    marker = f"pro_delete_{int(time.time())}"
    before = count_checks()
    _, answer = run(PRO, f"DELETE FROM address_checks /* {marker} */")
    FOUND["pro_delete"] = answer
    assert refused(answer)
    assert count_checks() == before
    assert log_has(marker) == [], "Postgres MCP Pro restricted should stop it before PostgreSQL"


def test_pro_restricted_rejects_commit_trick_before_the_database():
    marker = f"pro_commit_{int(time.time())}"
    before = count_checks()
    _, answer = run(PRO, f"COMMIT; BEGIN READ WRITE; DELETE FROM address_checks /* {marker} */")
    FOUND["pro_commit_trick"] = answer
    assert refused(answer)
    assert count_checks() == before
    assert log_has(marker) == []


def test_toolbox_passes_delete_to_postgres_which_refuses():
    marker = f"toolbox_delete_{int(time.time())}"
    before = count_checks()
    _, answer = run(TOOLBOX, f"DELETE FROM address_checks /* {marker} */")
    FOUND["toolbox_delete"] = answer
    lines = log_has(marker)
    FOUND["toolbox_delete_log"] = lines[:1]
    assert refused(answer)
    assert count_checks() == before
    assert lines and "user=ai_reader" in lines[0], "the DELETE should reach PostgreSQL as ai_reader"


def test_toolbox_commit_trick_still_refused_by_the_role():
    marker = f"toolbox_commit_{int(time.time())}"
    before = count_checks()
    _, answer = run(TOOLBOX, f"COMMIT; BEGIN READ WRITE; DELETE FROM address_checks /* {marker} */")
    FOUND["toolbox_commit_trick"] = answer
    FOUND["toolbox_commit_trick_log"] = log_has(marker)[:1]
    assert refused(answer)
    assert count_checks() == before


async def _two_calls(server, first, second):
    async with stdio_client(server) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tool = next(t for t in (await s.list_tools()).tools if t.name == "execute_sql")
            arg = next(iter(tool.inputSchema.get("properties", {})))
            out = []
            for sql in (first, second):
                res = await s.call_tool("execute_sql", {arg: sql})
                out.append((bool(res.isError), " ".join(c.text for c in res.content if getattr(c, "type", "") == "text")))
            return out


def test_toolbox_model_switches_read_only_off_then_the_privilege_refuses():
    """A model can send SET first: the read-only default is only a session setting. The missing privilege is what holds."""
    marker = f"toolbox_set_{int(time.time())}"
    before = count_checks()
    set_answer, delete_answer = asyncio.run(_two_calls(
        TOOLBOX, "SET default_transaction_read_only = off", f"DELETE FROM address_checks /* {marker} */"))
    FOUND["toolbox_set_off"] = set_answer
    FOUND["toolbox_delete_after_set"] = delete_answer
    assert refused(delete_answer)
    assert count_checks() == before


def test_pro_restricted_rejects_a_lone_commit():
    """Pro's restricted mode parses the SQL against an allowlist; a transaction command on its own is rejected too."""
    _, answer = run(PRO, "COMMIT")
    FOUND["pro_lone_commit"] = answer
    assert refused(answer)
