-- VitalSense persistence schema.
--
-- What is stored, and what deliberately is not:
--
--   sessions      one row per device connection, with the reap/resume behaviour of D-20
--   vitals        derived numbers at 1 Hz, not per frame
--   alarm_events  one row per alarm episode, with raise / acknowledge / clear times
--
-- The **waveform is not stored**. At 360 Hz, one patient generates ~250 MB of raw ECG
-- per day as float64, and a ward of twenty makes that 5 GB/day before indexes. Real
-- central stations do keep waveform, in compressed fixed-window buffers with a retention
-- policy and tiered storage - which is a project of its own and is not what this one is
-- demonstrating. Storing 1 Hz numerics keeps trends, export and alarm review working at
-- roughly 1/3000th of the volume, and the omission is recorded rather than discovered
-- later by someone wondering where the waveform went (D-42).

CREATE TABLE IF NOT EXISTS sessions (
    id              BIGSERIAL PRIMARY KEY,
    device_id       TEXT        NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    sample_rate_hz  REAL        NOT NULL,
    frames_received BIGINT      NOT NULL DEFAULT 0,
    frames_dropped  BIGINT      NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS sessions_device_started ON sessions (device_id, started_at DESC);

CREATE TABLE IF NOT EXISTS vitals (
    session_id      BIGINT      NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    t_server        TIMESTAMPTZ NOT NULL,
    sample_index    BIGINT      NOT NULL,
    hr              REAL,
    hr_class        TEXT,
    spo2            REAL,
    spo2_source     TEXT,
    spo2_quality    TEXT,
    pulse_bpm       REAL,
    perfusion_pct   REAL,
    rate_agreement  TEXT,
    temp_c          REAL,
    signal_quality  TEXT,
    PRIMARY KEY (session_id, t_server)
);

-- One row per alarm *episode*, not per evaluation. An alarm that holds for ten minutes
-- is one row whose cleared_at is filled in when it ends - otherwise reviewing a session
-- means counting duplicates, and the count depends on the evaluation rate rather than on
-- what happened to the patient.
CREATE TABLE IF NOT EXISTS alarm_events (
    id              BIGSERIAL PRIMARY KEY,
    session_id      BIGINT      NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    code            TEXT        NOT NULL,
    severity        TEXT        NOT NULL,
    message         TEXT        NOT NULL,
    raised_at       TIMESTAMPTZ NOT NULL,
    acknowledged_at TIMESTAMPTZ,
    acknowledged_by TEXT,
    cleared_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS alarm_events_session ON alarm_events (session_id, raised_at DESC);
-- Finding the open episode for a code is the hot path when a frame arrives.
CREATE INDEX IF NOT EXISTS alarm_events_open ON alarm_events (session_id, code) WHERE cleared_at IS NULL;
