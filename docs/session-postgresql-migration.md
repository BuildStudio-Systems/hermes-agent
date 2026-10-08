# SessionDB PostgreSQL migration preparation

Date: 2026-10-08 JST. **Offline migration and native auxiliary runtime preparation.
The running SessionDB still uses SQLite. Production activation remains blocked
until the transcript/search and remaining direct consumers are migrated.**

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

## Native shared-session ledgers — source only

`hermes_cli/session_postgres.py` now provides native pooled transactions for
room logs, room driver leases/tasks, policy projections, asynchronous delegation,
and delivery obligations. All five consumers resolve the active profile before
checking/creating a SQLite file. PostgreSQL has no runtime schema creation.
The `sessions` store selection is recognized for integration tests, but the
core SessionDB explicitly refuses activation while its migration is unfinished.
Do not enable this selection in a production profile yet.

Idle handles hold no database socket; detached result rows preserve named and
positional access. Each operation uses a bounded pool and transaction-local
schema selection. Write transactions share a schema-specific advisory lock;
read-only connections use server-enforced read-only transactions. Failed
statements cannot subsequently commit, and a failed/lost COMMIT is surfaced
without replay. The next separate operation can acquire a fresh connection.
No generic SQLite SQL translation or PRAGMA emulation is used.

Portable/native SQL covers quoted grant identifiers, explicit conflict clauses,
null-safe ownership checks, boolean predicates, retention pagination, monotonic
watermarks and UTF-8 byte limits. Room ownership probes query PostgreSQL even
when the retained file is absent. Policy connections now close deterministically;
a duplicate room-table helper was removed so it cannot shadow the native path.

Canonical runner: **246 passed, 0 failed, 1 platform-specific skipped**, across
11 files. This includes **54 native PostgreSQL ledger tests**, of which 40 reuse
existing room/driver behavior contracts against the real new backend. Coverage
includes competing claims, stale authority/lease rejection, cancellation and
uncertain execution, retention tombstones, replay projections, multilingual data,
read-only CTE rejection, DML-only role permissions, backend termination without
write replay, rollback after deferred FK failure, and 60 idle handles sharing a
bounded pool. The 15 offline importer tests and prior operational/cron PG tests
also passed. Initial test/integration failures are retained separately; this was
not a clean first run. No production process, profile or database was switched.

## Remaining runtime integration — do not activate yet

Changing a connection URL alone is insufficient. These paths still need native
PostgreSQL behavior and real functional tests:

| Component | Required behavior |
| --- | --- |
| `hermes_state.py`, common/schema/search/portability mixins | Transcript and prompt persistence, model usage, activity, compression and turn leases, transactional guards, export/import, native full-text and CJK search, pagination and previews |
| Rooms, driver, policy, asynchronous delegation and delivery | Native source/tests completed above; release only with the complete shared-session cutover |
| Gateway readiness/lifecycle, CLI observability/recovery/backup, A2A adapter | Read the selected backend rather than a stale retained SQLite file; native backup/restore and health checks |
| Projects/optional plugins, shared metrics | Audit lazy creation of separate SQLite stores; absence of an open descriptor is not proof these paths cannot create one later. Cron is already migrated. |

The current production Agent still opens `state.db`, `state.db-wal` and
`state.db-shm`. The previously migrated operational stores and Kanban continue
using PostgreSQL. Runtime permissions, services, source release and database
selection were not changed by this preparation.

Final acceptance must include real authenticated chat/history/search, owner
isolation, task/delivery retry behavior, concurrent lease acquisition, failure
recovery, inspection of all active database handles and scheduled DB backup
coverage. Formal numbered Word/PDF books remain pending final cutover.
