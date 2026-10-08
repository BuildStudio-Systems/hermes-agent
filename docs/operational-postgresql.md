# Operational PostgreSQL stores

The optional `postgresql` extra supports four profile-scoped stores:
Responses API history, durable run idempotency, chat-file metadata and coding
verification evidence. SessionDB and kanban require separate native migration;
this change does not switch those databases.

## Configuration

In the active profile's `config.yaml`:

```yaml
storage:
  postgresql:
    config_file: /protected/agent-postgres.json
    stores: [responses, runs, artifacts, verification]
```

The protected JSON contains `profile_home` (the exact active profile directory),
`connection` (host, hostaddr, port, dbname, user, password, sslmode and sslrootcert),
and a `schemas` mapping from each selected store to a distinct `agent_*` schema.
Use `verify-full` TLS, a trusted CA and an owner/group-restricted credential file.
Do not put credentials, source records or private paths in Git or test reports.
Profiles never inherit a default profile's database configuration.

Run this check as the service user before starting a migrated production gateway:

```sh
python -m hermes_cli.postgres_runtime --require responses runs artifacts verification
```

Install it as an additional `ExecStartPre` check. It performs no schema creation
or business writes. Missing or invalid PostgreSQL configuration stops startup;
selected stores never fall back to SQLite or process memory on connection failure.
Legacy unconfigured profiles retain their original behavior until migrated.

## Provisioning and migration

1. Create isolated schemas using `SCHEMA_SQL` in `hermes_cli.postgres_migrate`.
   A NOLOGIN owner owns tables and identity sequences. Grant the runtime role
   schema USAGE, table SELECT/INSERT/UPDATE/DELETE and sequence USAGE only.
   Restrict HBA to the authenticated TLS client address and intended database.
2. Stop all writers and inspect open handles. Take consistent SQLite snapshots;
   do not copy live WAL files as the sole backup. Preserve the original files.
3. Call `import_stores(snapshot_home, settings)`. Supply a temporary migration
   credential and optionally `owner_role=there_agent_operational_owner` for
   offline SET ROLE. All selected stores import in one transaction. Unexpected
   tables/columns, nonempty targets or any content mismatch roll back the batch.
4. Compare the returned per-table counts and SHA-256 digests. Every source
   column is checked; text collation differences do not affect the comparison.
   Identity sequence restart is transactional, unlike `setval`.
5. Activate the reviewed release and PostgreSQL configuration together, run the
   startup guard, check health and run authenticated API acceptance tests.
   Disable the temporary migration login and revoke owner membership afterward.
6. Dump the four schemas and restore into an isolated PostgreSQL instance.
   Verify records again. Include the schemas in the regular full database/PITR
   backup and keep file-content backup paired with the chat-file metadata.

After the first PostgreSQL write, do not automatically fall back to an old
SQLite snapshot. A rollback then requires quiescing writers and explicit data
reconciliation; code rollback alone can lose newer records.

## Runtime guarantees and limits

- Native PostgreSQL DML, bounded pooled connections and per-schema transaction
  locks preserve request admission and write consistency across processes.
- Run keys remain tenant-scoped; an active run never expires merely due to age.
- File metadata preserves owner, business chat, size, nanosecond modification
  time and expiration. File bytes remain in the managed file store.
- A broken connection fails its current operation. The next operation can
  reconnect; no ambiguous transaction is automatically retried.
- The adapter only converts qmark parameter bindings for reviewed portable
  queries. It does not emulate SQLite DDL, PRAGMA, FTS5 or SessionDB semantics.

## Validation

Use the canonical `scripts/run_tests.sh` runner. The PostgreSQL tests in
`tests/gateway/test_operational_postgres.py` require a disposable checkout with
an untracked `.test-postgres.json`; the database name must be
`test_agent_operational`. Never point it at a live database. Fixtures create
unique schemas and non-owner runtime roles, and remove them after each test.

Coverage includes concurrent idempotency admission, persistence/reopen, history
eviction, file ownership and chat isolation, exact migration, all-store rollback,
identity continuation, missing-config refusal, verified TLS, least privilege and
connection termination/recovery. Deployment and formal publication status are
recorded in the parent repository's release record, not inferred from this guide.
