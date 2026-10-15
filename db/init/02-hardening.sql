-- Hardening for the AI's login (ai_reader). Each line closes something the tests prove is open without it.
-- naive is left alone on purpose: it is the "before" picture.

\connect postgres
ALTER ROLE ai_reader NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5;
-- second layers, set as role defaults: the session CAN change these (the tests show it); privileges are the guard
ALTER ROLE ai_reader SET default_transaction_read_only = on;
ALTER ROLE ai_reader SET statement_timeout = '5s';

-- CONNECT: PUBLIC gets it on every database by default
REVOKE CONNECT ON DATABASE live FROM PUBLIC;
REVOKE CONNECT ON DATABASE reporting FROM PUBLIC;
GRANT CONNECT ON DATABASE reporting TO ai_reader;
REVOKE CONNECT ON DATABASE naive FROM PUBLIC;
GRANT CONNECT ON DATABASE naive TO naive_reader;

-- TEMPORARY: PUBLIC gets it on every database by default, and a read-only transaction still allows temp tables
REVOKE TEMPORARY ON DATABASE reporting FROM PUBLIC;

\connect reporting
-- schema public: a fresh PostgreSQL 18 database gives PUBLIC no CREATE here already (upgraded ones may); make sure
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
-- EXECUTE: PUBLIC gets it on every new function by default; SELECT on tables doesn't cover it
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA reporting FROM PUBLIC;
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA reporting REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- Large objects: PUBLIC has no large-object privilege by default, but it can EXECUTE the built-in functions that
-- create and write them (proved in test 6: lo_create worked for this SELECT-only role in a read-write transaction).
-- These are per-database grants on pg_catalog functions, so this applies to reporting only.
REVOKE EXECUTE ON FUNCTION lo_create(oid), lo_creat(integer), lo_from_bytea(oid, bytea), lo_put(oid, bigint, bytea),
  lo_open(oid, integer), lowrite(integer, bytea), lo_truncate(integer, integer), lo_truncate64(integer, bigint),
  lo_unlink(oid) FROM PUBLIC;
