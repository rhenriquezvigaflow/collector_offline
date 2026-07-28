CREATE TABLE IF NOT EXISTS local_outage_session (
    id uuid PRIMARY KEY,
    source_node_id varchar(128) NOT NULL,
    lagoon_id varchar(64) NOT NULL,
    started_at timestamptz NOT NULL,
    connectivity_restored_at timestamptz,
    sync_completed_at timestamptz,
    status varchar(16) NOT NULL CHECK (status IN ('offline', 'syncing', 'completed')),
    failure_reason text,
    CHECK (connectivity_restored_at IS NULL OR connectivity_restored_at >= started_at),
    CHECK (sync_completed_at IS NULL OR sync_completed_at >= started_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_local_outage_session_active
    ON local_outage_session (source_node_id, lagoon_id)
    WHERE status IN ('offline', 'syncing');
CREATE INDEX IF NOT EXISTS ix_local_outage_session_status_started
    ON local_outage_session (status, started_at);
CREATE INDEX IF NOT EXISTS ix_local_outage_session_node
    ON local_outage_session (source_node_id, lagoon_id);

CREATE TABLE IF NOT EXISTS edge_runtime_state (
    id integer PRIMARY KEY CHECK (id = 1),
    state varchar(16) NOT NULL CHECK (state IN ('online', 'offline', 'syncing')),
    failure_count integer NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    recovery_count integer NOT NULL DEFAULT 0 CHECK (recovery_count >= 0),
    current_session_id uuid REFERENCES local_outage_session(id) ON DELETE RESTRICT,
    last_health_at timestamptz,
    last_health_ok boolean,
    last_sync_at timestamptz,
    last_error text
);

INSERT INTO edge_runtime_state (id, state)
VALUES (1, 'online')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS local_scada_minute (
    id uuid PRIMARY KEY,
    outage_session_id uuid NOT NULL REFERENCES local_outage_session(id) ON DELETE RESTRICT,
    source varchar(32) NOT NULL DEFAULT 'collector_offline',
    lagoon_id varchar(64) NOT NULL,
    tag_id varchar(64) NOT NULL,
    bucket timestamptz NOT NULL,
    state integer,
    value_num double precision,
    value_bool boolean,
    captured_at timestamptz NOT NULL,
    sync_status varchar(16) NOT NULL DEFAULT 'pending'
        CHECK (sync_status IN ('pending', 'syncing', 'sent', 'failed')),
    sync_attempts integer NOT NULL DEFAULT 0 CHECK (sync_attempts >= 0),
    last_sync_error text,
    next_retry_at timestamptz,
    synced_at timestamptz,
    lease_until timestamptz,
    lease_token uuid,
    CONSTRAINT uq_local_scada_minute UNIQUE (lagoon_id, tag_id, bucket),
    CONSTRAINT ck_local_scada_minute_source CHECK (source = 'collector_offline'),
    CONSTRAINT ck_local_scada_minute_one_value CHECK (
        (CASE WHEN state IS NULL THEN 0 ELSE 1 END
        + CASE WHEN value_num IS NULL THEN 0 ELSE 1 END
        + CASE WHEN value_bool IS NULL THEN 0 ELSE 1 END) = 1
    ),
    CHECK (date_trunc('minute', bucket) = bucket),
    CHECK ((sync_status = 'syncing') = (lease_until IS NOT NULL AND lease_token IS NOT NULL)),
    CHECK (sync_status <> 'sent' OR synced_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_local_scada_minute_pending
    ON local_scada_minute (sync_status, next_retry_at, bucket);
CREATE INDEX IF NOT EXISTS ix_local_scada_minute_session
    ON local_scada_minute (outage_session_id, bucket);

CREATE TABLE IF NOT EXISTS local_scada_event (
    id uuid PRIMARY KEY,
    outage_session_id uuid NOT NULL REFERENCES local_outage_session(id) ON DELETE RESTRICT,
    lagoon_id varchar(64) NOT NULL,
    tag_id varchar(64) NOT NULL,
    tag_label varchar(255),
    previous_state integer,
    state integer NOT NULL,
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    sync_status varchar(16) NOT NULL DEFAULT 'pending'
        CHECK (sync_status IN ('pending', 'syncing', 'sent', 'failed')),
    sync_attempts integer NOT NULL DEFAULT 0 CHECK (sync_attempts >= 0),
    last_sync_error text,
    next_retry_at timestamptz,
    synced_at timestamptz,
    lease_until timestamptz,
    lease_token uuid,
    CHECK (ended_at IS NULL OR ended_at >= started_at),
    CHECK ((sync_status = 'syncing') = (lease_until IS NOT NULL AND lease_token IS NOT NULL)),
    CHECK (sync_status <> 'sent' OR synced_at IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_local_scada_event_open_tag
    ON local_scada_event (lagoon_id, tag_id)
    WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_local_scada_event_pending
    ON local_scada_event (sync_status, next_retry_at, started_at);
CREATE INDEX IF NOT EXISTS ix_local_scada_event_session
    ON local_scada_event (outage_session_id, started_at);
CREATE INDEX IF NOT EXISTS ix_local_scada_event_tag_range
    ON local_scada_event (lagoon_id, tag_id, started_at);

CREATE TABLE IF NOT EXISTS edge_tag_state (
    lagoon_id varchar(64) NOT NULL,
    tag_id varchar(64) NOT NULL,
    last_value integer NOT NULL,
    open_event_id uuid REFERENCES local_scada_event(id) ON DELETE SET NULL,
    PRIMARY KEY (lagoon_id, tag_id)
);
