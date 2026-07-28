-- Retira columnas constantes, duplicadas o puramente diagnósticas.
-- Se conservan todas las tablas necesarias para recuperación, eventos y sync.

DROP INDEX IF EXISTS ix_local_scada_minute_tag_range;
DROP INDEX IF EXISTS ix_edge_tag_state_observed;
DROP INDEX IF EXISTS uq_local_scada_event_open_tag;

ALTER TABLE local_outage_session
    DROP COLUMN IF EXISTS diagnostic_metadata,
    DROP COLUMN IF EXISTS created_at,
    DROP COLUMN IF EXISTS updated_at;

ALTER TABLE edge_runtime_state
    DROP COLUMN IF EXISTS updated_at;

ALTER TABLE local_scada_minute
    DROP COLUMN IF EXISTS source_node_id,
    DROP COLUMN IF EXISTS quality,
    DROP COLUMN IF EXISTS created_at;

ALTER TABLE local_scada_event
    DROP COLUMN IF EXISTS source_node_id,
    DROP COLUMN IF EXISTS alert_type,
    DROP COLUMN IF EXISTS is_open,
    DROP COLUMN IF EXISTS created_at,
    DROP COLUMN IF EXISTS updated_at;

ALTER TABLE edge_tag_state
    DROP COLUMN IF EXISTS last_changed_at,
    DROP COLUMN IF EXISTS last_observed_at,
    DROP COLUMN IF EXISTS updated_at;

CREATE UNIQUE INDEX IF NOT EXISTS uq_local_scada_event_open_tag
    ON local_scada_event (lagoon_id, tag_id)
    WHERE ended_at IS NULL;
