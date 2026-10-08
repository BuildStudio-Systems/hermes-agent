# Native PostgreSQL cron persistence

2026-10-08 JST. The profile can select `cron` and `cron_notes` under the existing
`storage.postgresql.stores` setting. Both use the same protected credential
file and profile binding as the other operational stores. Production assigns
both scopes to one schema so the database advisory transaction lock also
serializes cross-process admission, incident deduplication and quota checks.

`cron_postgres_schema.sql` is applied offline by the schema owner. The runtime
has only schema USAGE and table SELECT/INSERT/UPDATE/DELETE. It performs no
schema initialization, and a missing or invalid selected configuration never
falls back to SQLite. Existing profiles that have not selected PostgreSQL
retain their original local storage behavior.

The implementation preserves execution state transitions and terminal-state
immutability, interrupted-owner recovery semantics, error-signature incident
deduplication, closed-incident behavior, and per-key/per-job UTF-8 byte limits.
PostgreSQL uses `LIMIT ALL` for terminal-history retention and `octet_length`
for byte accounting. The notepad resolves the active profile at operation
time; an import-time profile pathname no longer leaks notes across profiles.
Job removal deletes PostgreSQL notes even when no local notepad file exists.

Validation: 80 tests passed through `scripts/run_tests.sh` across cron and the
existing operational PostgreSQL suite. Seven cron cases use real PostgreSQL
(including separate-process contention); one additional case tests dynamic
local-profile resolution. The first run exposed SQLite's negative LIMIT
syntax in retention; the native branch and retention regression fix it.

Production activation `20261008-cron-postgres` verified that the existing
execution table had zero rows and the incident/notepad stores had no records
to import. The activation script explicitly refuses nonempty sources. This
is not a generic migration procedure for another installation with cron data.
The existing empty SQLite file is retained as evidence and is no longer used
by the selected runtime. Synthetic execution, incident and multilingual-note
tests passed as the actual service account and were cleaned. Verified TLS,
no schema CREATE permission, authenticated API 200/anonymous 401 and health
200 passed. A three-table dump restored into an independent PostgreSQL cluster
with matching counts/content.

SessionDB `state.db` remains SQLite. Its separate 31-table offline migration
and real-data rehearsal are documented in `session-postgresql-migration.md`;
the live session backend, search, room and delivery adapters are not yet
converted. This release must not be described as eliminating every SQLite
path in Agent, or as final formal-design-book publication.
