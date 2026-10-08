-- Offline schema for profile-scoped cron audit and bounded notepad state.
CREATE TABLE executions (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
    process_id TEXT NOT NULL, pid BIGINT NOT NULL, process_started_at BIGINT,
    status TEXT NOT NULL CHECK(status IN ('claimed','running','completed','failed','unknown')),
    claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT
);
CREATE INDEX idx_executions_job_claimed ON executions(job_id,claimed_at DESC,id DESC);
CREATE INDEX idx_executions_status_claimed ON executions(status,claimed_at DESC,id DESC);
CREATE TABLE cron_incidents (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, error_sig TEXT NOT NULL,
    state TEXT NOT NULL, failure_type TEXT NOT NULL DEFAULT 'unknown',
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    acked_at TEXT, closed_at TEXT, error TEXT NOT NULL, output_file TEXT
);
CREATE INDEX idx_cron_incidents_job ON cron_incidents(job_id);
CREATE INDEX idx_cron_incidents_state ON cron_incidents(state);
CREATE TABLE cron_notepad (
    job_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    updated_at TEXT NOT NULL, PRIMARY KEY(job_id,key)
);
