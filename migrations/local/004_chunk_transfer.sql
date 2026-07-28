-- Replace JSON backfill with resumable CSV.gz chunks. Apply with the service
-- stopped. Any legacy JSON lease in flight is returned to pending.

CREATE TABLE IF NOT EXISTS transfer_chunk (
    chunk_id uuid PRIMARY KEY,
    transfer_id uuid NOT NULL UNIQUE,
    sequence_number bigint NOT NULL UNIQUE,
    source varchar(32) NOT NULL DEFAULT 'collector_offline',
    source_node_id varchar(128) NOT NULL,
    lagoon_id varchar(64) NOT NULL,
    data_kind varchar(16) NOT NULL CHECK (data_kind IN ('minute', 'event')),
    file_path text,
    manifest_path text,
    from_timestamp timestamptz NOT NULL,
    to_timestamp timestamptz NOT NULL,
    row_count integer NOT NULL CHECK (row_count > 0),
    uncompressed_size_bytes bigint NOT NULL DEFAULT 0,
    compressed_size_bytes bigint NOT NULL DEFAULT 0,
    sha256 varchar(64),
    part_size_bytes integer NOT NULL CHECK (part_size_bytes > 0),
    total_parts integer NOT NULL DEFAULT 0,
    confirmed_parts jsonb NOT NULL DEFAULT '[]'::jsonb,
    status varchar(16) NOT NULL DEFAULT 'CREATING' CHECK (
        status IN ('CREATING','SEALED','PENDING','UPLOADING','UPLOADED',
                   'IMPORTING','VERIFIED','FAILED','QUARANTINED')
    ),
    retry_count integer NOT NULL DEFAULT 0,
    next_retry_at timestamptz,
    last_error text,
    lease_until timestamptz,
    lease_token uuid,
    staged_rows integer,
    inserted_rows integer,
    duplicate_rows integer,
    created_at timestamptz NOT NULL,
    sealed_at timestamptz,
    uploaded_at timestamptz,
    verified_at timestamptz,
    file_deleted_at timestamptz,
    CONSTRAINT ck_transfer_chunk_source CHECK (source = 'collector_offline')
);

CREATE INDEX IF NOT EXISTS ix_transfer_chunk_status_retry
    ON transfer_chunk (status, next_retry_at, sequence_number);
CREATE INDEX IF NOT EXISTS ix_transfer_chunk_verified
    ON transfer_chunk (verified_at);

ALTER TABLE edge_runtime_state
    ADD COLUMN IF NOT EXISTS next_chunk_sequence bigint NOT NULL DEFAULT 1;
UPDATE edge_runtime_state
SET next_chunk_sequence = GREATEST(
    next_chunk_sequence,
    COALESCE((SELECT max(sequence_number) + 1 FROM transfer_chunk), 1)
);
ALTER TABLE edge_runtime_state
    DROP CONSTRAINT IF EXISTS ck_edge_runtime_chunk_sequence;
ALTER TABLE edge_runtime_state
    ADD CONSTRAINT ck_edge_runtime_chunk_sequence CHECK (next_chunk_sequence > 0);

ALTER TABLE local_scada_minute
    ADD COLUMN IF NOT EXISTS chunk_id uuid REFERENCES transfer_chunk(chunk_id) ON DELETE SET NULL;
ALTER TABLE local_scada_event
    ADD COLUMN IF NOT EXISTS chunk_id uuid REFERENCES transfer_chunk(chunk_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS source varchar(32) NOT NULL DEFAULT 'collector_offline';

UPDATE local_scada_minute SET source = 'collector_offline'
WHERE source IS DISTINCT FROM 'collector_offline';
UPDATE local_scada_event SET source = 'collector_offline'
WHERE source IS DISTINCT FROM 'collector_offline';

ALTER TABLE local_scada_event DROP CONSTRAINT IF EXISTS ck_local_scada_event_source;
ALTER TABLE local_scada_event ADD CONSTRAINT ck_local_scada_event_source
    CHECK (source = 'collector_offline');

UPDATE local_scada_minute
SET sync_status = 'pending', lease_until = NULL, lease_token = NULL,
    next_retry_at = NULL, last_sync_error = 'migrated from legacy JSON backfill'
WHERE sync_status = 'syncing';
UPDATE local_scada_event
SET sync_status = 'pending', lease_until = NULL, lease_token = NULL,
    next_retry_at = NULL, last_sync_error = 'migrated from legacy JSON backfill'
WHERE sync_status = 'syncing';

ALTER TABLE local_scada_minute
    DROP CONSTRAINT IF EXISTS local_scada_minute_sync_status_check,
    DROP CONSTRAINT IF EXISTS ck_local_scada_minute_sync_status;
ALTER TABLE local_scada_minute ADD CONSTRAINT ck_local_scada_minute_sync_status
    CHECK (sync_status IN ('pending','syncing','chunked','sent','failed'));

ALTER TABLE local_scada_event
    DROP CONSTRAINT IF EXISTS local_scada_event_sync_status_check,
    DROP CONSTRAINT IF EXISTS ck_local_scada_event_sync_status;
ALTER TABLE local_scada_event ADD CONSTRAINT ck_local_scada_event_sync_status
    CHECK (sync_status IN ('pending','syncing','chunked','sent','failed'));

CREATE INDEX IF NOT EXISTS ix_local_scada_minute_chunk ON local_scada_minute (chunk_id);
CREATE INDEX IF NOT EXISTS ix_local_scada_event_chunk ON local_scada_event (chunk_id);
CREATE INDEX IF NOT EXISTS ix_local_scada_minute_unchunked
    ON local_scada_minute (bucket, tag_id) WHERE sync_status = 'pending' AND chunk_id IS NULL;
CREATE INDEX IF NOT EXISTS ix_local_scada_event_unchunked
    ON local_scada_event (started_at, tag_id) WHERE sync_status = 'pending' AND chunk_id IS NULL;
