# SessionDB PostgreSQL migration preparation

Date: 2026-10-08 JST. **Offline preparation and rehearsal only. The running
SessionDB still uses SQLite. No runtime backend selector has been added.**

## What is implemented

`hermes_cli/session_postgres_schema.sql` defines 31 native PostgreSQL tables
and the existing canonical indexes. It covers SessionDB schema version 26,
gateway room/event/policy/driver data, delivery obligations, and Telegram topic
bindings. The async-delegation initializer adds `origin_session_id` separately
from the core schema version; this column is explicitly retained, including
legacy NULL values. PostgreSQL's reserved `grant` column is quoted.

`hermes_cli.session_postgres_migrate.import_snapshot(settings, path)` accepts
a consistent protected SQLite backup and a temporary offline migration role.
It does not change any application configuration or running service. The
destination must already contain exactly the expected 31 empty tables.

The importer:

- Reads the source in one read-only transaction and checks database integrity,
  foreign keys, version, table names, columns, and derived view definitions.
- Rejects unknown tables, views, generated columns and source column drift.
  It explicitly recognizes the derived FTS tables/view, which must be replaced
  by native PostgreSQL search before runtime cutover.
- Locks destination tables and imports in one transaction. A second import or
  a nonempty optional table is rejected; there is no upsert or destructive reset.
- Preserves every source column, identity, JSON string, prompt and float64
  timestamp. It hashes typed row values and compares counts and content before
  commit. Receipts contain counts/digests, never message text or credentials.
- Preserves the SQLite message sequence high-water mark, including deleted IDs.
  Deferred foreign keys are checked before a transactional `ALTER IDENTITY`;
  unlike `setval`, the sequence restart rolls back with a failed transaction.
- Uses verified TLS, a bounded connection timeout and bounded server lock and
  statement waits. Unsupported values are rejected rather than coerced away.

Callers must stop **all** writers before the final cutover backup. An online
backup is sufficient for a rehearsal but cannot include subsequent writes.
Do not reuse a rehearsal snapshot for production activation.

## Verified evidence

The canonical per-file runner passed 15 real PostgreSQL tests in
`tests/hermes_cli/test_session_postgres_migrate.py`. These exercise the real
SQLite initializers and disposable PostgreSQL, including full-column parity,
read-only source preservation, precision-loss detection, rollback, missing
tables, unknown schema objects, simultaneous importers, sequence exhaustion,
and rejected destination/owner/TLS settings.

A protected online snapshot of the current deployment contained 27 tables and
4,840 rows: 256 sessions, 4,024 messages, 522 model-usage rows, 33 system prompts,
four metadata rows and one version row. All source columns/counts/content
matched after import into a disposable PostgreSQL cluster. Four optional
tables were absent from the source and remained empty. The next message ID
was 4045, preserving the source sequence rather than using the row count.

An actual `pg_dump` was restored into a separate disposable database. Every
source-table count/content digest and the message sequence matched again.
Private snapshots and dumps remain in the controlled infrastructure archive;
no conversations or credentials are stored in this repository.

Initial tests caught the reserved column name and the pending deferred-FK
trigger restriction on identity restart. A later guard correctly exposed the
derived search view and the lazily added async column. Those failures are
retained in private evidence, not reported as a clean first run.

## Remaining runtime integration — do not activate yet

Changing a connection URL alone is insufficient. These paths still need native
PostgreSQL behavior and real functional tests:

| Component | Required behavior |
| --- | --- |
| `hermes_state.py`, common/schema/search/portability mixins | Transcript and prompt persistence, model usage, activity, compression and turn leases, transactional guards, export/import, native full-text and CJK search, pagination and previews |
| `gateway/hosted_rooms.py`, room driver and policy checkpoint | Native transactions, room authority/leases/events, lazy-schema replacement, portable SQL and quoted identifiers |
| `tools/async_delegation.py`, `gateway/delivery_ledger.py` | Durable task and delivery state; preserve uncertain-delivery semantics and deduplication |
| Gateway readiness/lifecycle, CLI observability/recovery/backup, A2A adapter | Read the selected backend rather than a stale retained SQLite file; native backup/restore and health checks |
| Cron executions/incidents/notepad and projects/plugins | Audit lazy creation of separate SQLite stores; absence of an open descriptor is not proof these paths cannot create one later |

The current production Agent still opens `state.db`, `state.db-wal` and
`state.db-shm`. The previously migrated operational stores and Kanban continue
using PostgreSQL. Runtime permissions, services, source release and database
selection were not changed by this preparation.

Final acceptance must include real authenticated chat/history/search, owner
isolation, task/delivery retry behavior, concurrent lease acquisition, failure
recovery, inspection of all active database handles and scheduled DB backup
coverage. Formal numbered Word/PDF books remain pending final cutover.
