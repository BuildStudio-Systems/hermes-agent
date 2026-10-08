# Native PostgreSQL Kanban storage

The optional `kanban` entry in the active profile's `storage.postgresql.stores`
uses its own schema, provisioned offline from `kanban_postgres_schema.sql`.
It shares the verified TLS transport and restricted credential-file mechanism
described in [operational-postgresql.md](operational-postgresql.md). Runtime has
table DML and sequence USAGE, never schema CREATE or schema-owner membership.

## Behavior

- Seven business tables retain the existing task, dependency, comment, event,
  run, attachment and notification behavior. Two additional tables store board
  identities and interrupted filesystem operations.
- Each board has an immutable internal ID. PostgreSQL row policies filter every
  business table by the selected ID and reject writes to deleted identities.
  Recreating an archived/deleted board name gets a fresh identity. The connection
  role is profile-wide: RLS prevents accidental cross-board queries, but does not
  make hostile SQL issued with that same profile credential a separate tenant.
- Board writes use native transactions, explicit nested savepoints and a
  board-specific advisory transaction lock. Dispatcher exclusion is a PostgreSQL
  session advisory lock, including across processes. Failed writes are not
  automatically replayed.
- Board metadata/workspaces remain files. Archive/delete records a durable
  PostgreSQL intent before renaming the directory. Recovery follows the committed
  board identity, including when the commit response was lost. Ambiguous paths
  fail closed; an unverified commit is never reversed blindly.
- PostgreSQL board export produces archive version 2 with `kanban.json`, not a
  SQLite file. A consistent read snapshot is scrubbed of active claims and
  notification routing. Imports use a new board, remap numeric run/event IDs,
  reject unsafe task identifiers and validate the complete column list.
  Version 1 SQLite exports are read without leaving a live SQLite store. Old
  archives with an outdated schema require an explicit schema upgrade first;
  they are rejected rather than silently losing columns.
- Local SQLite repair/checkpoint operations do not run against PostgreSQL.
  Database recovery is performed through DBserver backups and PostgreSQL tools.

## Migration

Stop all board writers and take SQLite online-backup snapshots first.
`kanban_postgres_migrate.import_boards` accepts only an empty destination,
imports all boards in one transaction, preserves every ID and column, verifies
row counts/content digests and transactionally advances identity sequences.
Disable the temporary migration login and revoke owner membership afterward.
Retained original SQLite files are evidence, not a supported live rollback after
new PostgreSQL writes. Do not remove unrelated SessionDB files.

## Validation and limits — 2026-10-08

The canonical per-file runner on Linux passed 195 targeted tests, including 17
real PostgreSQL tests for RLS, claims, nested rollback, board recreation, archive
recovery, portability, migration rollback and the production readiness command.
One Windows-only case was skipped; one Git worktree case was deselected because
the disposable DB host has no Git. A Windows run passed 52 cases and retained
three platform-related failures (symlink privilege, POSIX wait status, Git path
separator assertion); these are not reported as a clean full-suite result.

Production Agent release `20261008-kanban-postgres` passed blocked-task CRUD,
JSON export/import, verified TLS, authenticated session listing and anonymous
rejection. Synthetic boards were removed. Nine PostgreSQL tables were dumped,
restored in a separate socket-only cluster and compared. The first startup
readiness probe lacked the board scope; the factory was corrected and the actual
readiness command was added to the tests. SessionDB `state.db` is still SQLite.
