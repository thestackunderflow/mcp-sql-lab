# Read-only AI access to PostgreSQL: a test-first lab

**Lab snapshot: October 2026.** PostgreSQL 18.6 (`postgres:18.6`). MCP servers for phase 2: Postgres MCP Pro 0.3.0,
Google MCP Toolbox 1.14.0 (MCP spec 2026-07-28).

An AI agent connected to a database through MCP should be able to read what it needs and nothing else. This lab
proves, with tests, where that guarantee actually comes from: not from a "read-only" switch, but from the database
identity the agent logs in as and from the data that identity can see.

All data is made up: a small property-check service (customers, address checks, invoices, support tickets). Names
and emails are fake.

## What's in it

| Database | What it is | Who connects |
|---|---|---|
| `live` | The application's tables, including `customers.email` (sensitive). | Nobody from the AI side. |
| `reporting` | What the AI may see: approved columns only, refreshed by `refresh/refresh.py`. | `ai_reader` (hardened). |
| `naive` | The same reporting tables with a plain SELECT-only role and PostgreSQL's defaults left alone. | `naive_reader` (the "before" picture). |

**The refresh** (`refresh/refresh.py`) opens one `REPEATABLE READ READ ONLY` transaction on `live` and copies each
table with `COPY (SELECT <approved columns>) TO STDOUT` into `reporting`. Email is never selected, so it never leaves
`live`: not in a dump file, not in `reporting`, not even for a moment. `reporting.refresh_info.refresh_started_at`
records when the script started (not the exact snapshot point).

**`ai_reader`**: NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS, a connection limit, member of no
other role; CONNECT on `reporting` only; USAGE + SELECT on the `reporting` schema only; no EXECUTE on routines
(PUBLIC's default EXECUTE revoked, also for future functions and for the built-in large-object functions); a fixed `search_path` (reporting, pg_catalog, pg_temp
last); `statement_timeout` 5 s and `default_transaction_read_only = on` as role defaults, which the session can change
(the tests show that, on purpose). PUBLIC's TEMPORARY and CONNECT on `reporting` are revoked.

**Statement logging** (`log_statement=all`, with role and application name in every line) shows what reached
PostgreSQL. It is not an audit trail: it skips statements with syntax errors and can record sensitive values. For real
audit needs use an audit extension such as pgAudit, or your managed database's audit facility.

## The tests (phase 1: the database)

1. `ai_reader` can SELECT from the reporting tables.
2. `ai_reader` cannot connect to `live`.
3. DELETE / UPDATE / INSERT / DROP as `ai_reader` are refused (exact texts saved).
4. `COMMIT; DELETE …` in one string is still refused, even after the same string switches the read-only default off:
   privileges protect data operations; session settings don't.
5. Asking for email fails: `reporting` never had the column.
6. `naive_reader` can create a temp table, run a function, and create a large object through PUBLIC's defaults;
   `ai_reader` can't. Large objects: PUBLIC has no large-object *privilege*, but it can EXECUTE the built-in `lo_*`
   functions, so a SELECT-only role in a `BEGIN READ WRITE` transaction could create one until those were revoked
   (found by this test, 14 Oct 2026).
7. `SET ROLE` to anything else fails; the server-file roles are not granted.
8. The role's `statement_timeout` cancels a long query; `SET statement_timeout = 0` in the same session undoes it.
   A limit the session must not be able to disable belongs in an independent layer.
10. Every test statement expected to reach PostgreSQL at the logged execution stage appears in the lab log with its
    role and application name.
11. `refresh_started_at` is set; `reporting` has no email column; the refresh script's column lists hold only approved
    columns.
12. From the catalogs: `ai_reader`'s attributes, search_path and memberships are as listed; it can execute no
    SECURITY DEFINER function; PUBLIC has no CREATE on schema public and no TEMPORARY or CONNECT on `reporting`.
13. With read-only switched off (`SET default_transaction_read_only = off`), `naive_reader` deletes rows by calling
    `purge_old_checks()`, a SECURITY DEFINER function that runs with its owner's rights: PUBLIC's default EXECUTE is a
    way around a missing DELETE privilege. `ai_reader` gets "permission denied for function purge_old_checks" because
    EXECUTE was revoked from PUBLIC (15 Oct 2026).

## The tests (phase 2: through real MCP servers, `tests/mcp/`)

The same requests sent by an MCP client over stdio, as Claude Code or Claude Desktop would, to two servers that both log in
as `ai_reader`. What the lab found (14 Oct 2026, exact texts in `tests/out/mcp.json`):

| Request | Postgres MCP Pro 0.3.0, restricted | Google MCP Toolbox 1.14.0, prebuilt postgres |
|---|---|---|
| SELECT | answered | answered |
| `DELETE` | rejected by the server ("Error validating query"; a lone COMMIT too); never reaches PostgreSQL (not in the log) | reaches PostgreSQL as `ai_reader`; refused: "cannot execute DELETE in a read-only transaction" (the role's default) |
| `COMMIT; BEGIN READ WRITE; DELETE` | rejected by the server; not in the log | refused: prepared statements can't hold several commands |
| `SET default_transaction_read_only = off`, then `DELETE`, same session | (not tested: SET is rejected in restricted mode) | the SET is accepted silently; the DELETE then gets **"permission denied for table address_checks"**: the setting fell, the privilege held |
| Tool annotations | none on any tool | `execute_sql` marked destructive (readOnlyHint false) |

Worth knowing: Postgres MCP Pro returns its refusal as a normal result (`isError: false`, the text starts "Error:"), so a
client that only checks the error flag would see success. In every case the row count is unchanged.

Exact error texts and findings are written to `tests/out/` by the tests; the video quotes them from there, never by
hand.

## Run it

Everything runs in Docker:

```bash
docker compose up -d db
docker compose run --rm tests        # phase 1: the database
docker compose run --rm tests-mcp    # phase 2: through the MCP servers
docker compose down -v      # start again from scratch
```

## Licence

Apache License 2.0 (`LICENSE`). The passwords in `compose.yaml` and `db/init/` are lab-only; never reuse them.
