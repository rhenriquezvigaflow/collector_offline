ALTER TABLE local_scada_minute
    ADD COLUMN IF NOT EXISTS source varchar(32)
    NOT NULL
    DEFAULT 'collector_offline';

ALTER TABLE local_scada_minute
    DROP CONSTRAINT IF EXISTS ck_local_scada_minute_source;

ALTER TABLE local_scada_minute
    ADD CONSTRAINT ck_local_scada_minute_source
    CHECK (source = 'collector_offline');
