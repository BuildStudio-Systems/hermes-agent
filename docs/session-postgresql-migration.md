# SessionDB PostgreSQL migration preparation

Date: 2026-10-08 JST. **Offline migration, native auxiliary runtime and core transcript/search preparation.
The running SessionDB still uses SQLite. Production activation remains blocked
until the remaining direct consumers and controlled cutover are complete.**

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

## Core transcript/search preparation — source only

`hermes_state_postgres.PostgresSessionDB` reuses the existing SessionDB business
methods with native pooled transactions. The ordinary `SessionDB` constructor
still refuses the `sessions` selection: this is an isolated integration engine,
not permission to enable it in production. Package metadata includes the new
module and offline runtime SQL.

Covered behavior includes prompts and transcripts, imported/deleted identity
high-water marks, export/import, token accounting and per-model attribution,
activity/preview/listing, title lineage, prune filters, turn/compression leases,
and Telegram topic opt-in. Core SQL now uses explicit conflict targets,
`RETURNING id`, null-safe comparisons, typed nullable cost parameters and
qualified upsert counters. SQLite regressions passed as well.

Read contexts use one repeatable read snapshot, and the server enforces their
read-only status. Only failures before business SQL begins can receive bounded
admission retries. Statement errors and ambiguous COMMIT acknowledgements are
not replayed. Core standalone autocommit preserves the existing core handle
contract; auxiliary ledger handles retain their explicit transaction contract.

`session_postgres_runtime.sql` is an offline, transactional, first-install
migration for narrow JSON lineage functions and three native GIN indexes. The
DBA must provision `pg_trgm` in `public` first. Runtime DML credentials must not
receive schema/table creation or maintenance ownership. This script is separate
from the empty-schema importer and must be applied after importing the snapshot.

Search matches complete content/tool-name/tool-call fields with parameterized
native expressions and trigram indexes. It supports word terms, quoted phrases,
AND/OR/NOT, prefixes, literal CJK substrings, source/role filters, pagination,
time sorting and field projection. It does not truncate searchable large tool
outputs into a size-limited tsvector. Relevance uses normalized term frequency,
**not SQLite BM25**; exact ranking/tokenization equivalence is not claimed.
Rewound rows are excluded from default matches and context; archived compaction
rows remain searchable. Context and snippets are bounded in returned results.
Native vacuum/reindex remain explicit DBserver maintenance responsibilities.

Readiness and unclean-exit probes select PostgreSQL before inspecting retained
files and fail closed on broken configuration/schema. The latter reports
`postgresql-schema-ok`, which proves schema reachability, not physical database
integrity. Approval-history scanning also selects the native read-only store.
SQLite FTS conversion/backfill statuses are inapplicable to native indexes.

Latest canonical run: **341 passed, 0 failed, 1 platform skipped**, 14 files,
including **76 core PostgreSQL tests** and the earlier 54 ledger tests. A separate
broader run passed the 243 existing SessionDB cases and 9 batch-insert cases
(two additional capability skips); these are separate receipts, not a single
combined run. Intermediate import-fixture, type-inference, named-parameter,
upsert-ambiguity and test-portability failures were retained and fixed.

Private DB-host rehearsal imported the retained real snapshot into an isolated
PostgreSQL cluster. **256 sessions / 4,024 messages** produced identical complete
exports, session listings and model-consumable conversations through the SQLite
and PostgreSQL readers. Synthetic chat/usage/multilingual search passed, and no
local `state.db` was created. The snapshot SHA-256 stayed
`70c30f2dad84182951b5b2b921750a1830be28729a71113025c033efaa2cf9c2`.
Canonical read-payload digest:
`6c96d735bdda73b2c8b3eee7c8bf770931b78c8d7530bd1020a16be5d2a5b80d`.
No conversation body or credentials were published. This earlier snapshot is
rehearsal evidence only; final cutover still needs a new backup with all writers
stopped, current role/connection-budget validation, live API acceptance and an
independent restore of the final production dump.

## Remaining runtime integration — do not activate yet

Changing a connection URL alone is insufficient. These paths still need native
PostgreSQL behavior and real functional tests:

| Component | Required behavior |
| --- | --- |
| Core transcript/search | Native integration and real-snapshot read parity completed above; ordinary constructor activation, broader API/consumer integration and production cutover remain |
| Rooms, driver, policy, asynchronous delegation and delivery | Native source/tests completed above; release only with the complete shared-session cutover |
| Direct consumers | Readiness/lifecycle and approval-history scanning adapted; CLI observability/recovery/backup, cross-profile search and A2A still require native handling rather than stale file reads |
| Projects/optional plugins, shared metrics | Audit lazy creation of separate SQLite stores; absence of an open descriptor is not proof these paths cannot create one later. Cron is already migrated. |

The current production Agent still opens `state.db`, `state.db-wal` and
`state.db-shm`. The previously migrated operational stores and Kanban continue
using PostgreSQL. Runtime permissions, services, source release and database
selection were not changed by this preparation.

Final acceptance must include real authenticated chat/history/search, owner
isolation, task/delivery retry behavior, concurrent lease acquisition, failure
recovery, inspection of all active database handles and scheduled DB backup
coverage. Formal numbered Word/PDF books remain pending final cutover.


## Named-profile consumers and diagnostics (2026-10-08, source only)

Named-profile session links and A2A forwarding now resolve each registered
profile's own storage under a context-local home override, without process
environment changes or credential inheritance. Native storage is selected before
checking for a retained state.db. Read-only search stays read-only; forwarding
storage failure stops the task instead of creating a fresh conversation. Native
schema errors and connection diagnostics are not echoed in remote task errors.

Doctor and collect_state_db_stats inspect PostgreSQL schema, counts, logical
size and the three valid/ready native search indexes. They do not checkpoint,
repair, or vacuum retained SQLite files. Configuration failure is unavailable,
not permission to fall back. SQLite FTS conversion/rebuild is explicitly rejected
by the native engine; DBA maintenance remains separate from runtime DML rights.

Canonical runner: **517 passed, 0 failed, 1 platform-specific skipped**, 17 files,
including 80 native core cases, 54 native auxiliary cases and existing A2A/search/
SQLite-stat contracts. One first-run legacy fixture had inconsistent profile
registry/path resolution; corrected without weakening named-profile validation.
The private production-snapshot rehearsal again matched all 256 exports,
listings and conversation views (4,024 messages); source snapshot stayed unchanged.

This is not production activation. The ordinary SessionDB constructor still
rejects sessions selection until backup/restore, remaining direct consumers and
final cutover acceptance are complete. Running release remains
20261008-cron-postgres; final numbered Word/PDF publication is pending.
