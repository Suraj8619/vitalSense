/**
 * PostgreSQL implementation of the storage contract.
 *
 * Satisfies exactly the interface in `store.js`, and is exercised by the same contract
 * suite that runs against `MemoryStore` - so the *behaviour* is pinned without a
 * database, and only the SQL needs a container. See `test/storage.test.js`.
 *
 * Writes are on the ingest path, so they must never block it: `recordVitals` is
 * fire-and-forget from the session's point of view, and a database that is down degrades
 * the system to "no history" rather than "no monitor". A patient monitor that stops
 * displaying because a disk filled up would be a worse device than one with no database
 * at all (D-43).
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import pg from 'pg';

const HERE = path.dirname(fileURLToPath(import.meta.url));

/** Postgres returns TIMESTAMPTZ as Date; the rest of the system speaks epoch millis. */
const ms = (value) => (value === null || value === undefined ? null : new Date(value).getTime());
const ts = (value) => (value === null || value === undefined ? null : new Date(value));

export class PostgresStore {
  /**
   * @param {object} options
   * @param {string} options.connectionString
   * @param {object} [options.log]
   */
  constructor({ connectionString, log = console } = {}) {
    this.pool = new pg.Pool({ connectionString, max: 4 });
    this.log = log;
    this.degraded = false;
    // A failing pool must not take the process down with an unhandled 'error' event -
    // the monitor keeps running without history.
    this.pool.on('error', (err) => this.#degrade(err));
  }

  get enabled() {
    return !this.degraded;
  }

  #degrade(err) {
    if (!this.degraded) {
      this.degraded = true;
      this.log.error(`[db] persistence disabled after error: ${err.message}`);
    }
  }

  async #run(text, values = []) {
    if (this.degraded) return { rows: [], rowCount: 0 };
    try {
      return await this.pool.query(text, values);
    } catch (err) {
      this.#degrade(err);
      return { rows: [], rowCount: 0 };
    }
  }

  async init() {
    const schema = readFileSync(path.join(HERE, 'schema.sql'), 'utf8');
    // Throws on a genuinely unreachable database at startup, which is the one moment it
    // is right to fail loudly: the operator asked for persistence and cannot have it.
    await this.pool.query(schema);
  }

  async createSession({ deviceId, startedAt, sampleRateHz }) {
    const { rows } = await this.#run(
      `INSERT INTO sessions (device_id, started_at, sample_rate_hz)
       VALUES ($1, $2, $3) RETURNING id`,
      [deviceId, ts(startedAt), sampleRateHz],
    );
    return rows.length ? Number(rows[0].id) : null;
  }

  async endSession(id, endedAt, stats = {}) {
    if (id === null) return;
    await this.#run(
      `UPDATE sessions
          SET ended_at = $2,
              frames_received = COALESCE($3, frames_received),
              frames_dropped  = COALESCE($4, frames_dropped)
        WHERE id = $1`,
      [id, ts(endedAt), stats.framesReceived ?? null, stats.framesDropped ?? null],
    );
  }

  async recordVitals(id, row) {
    if (id === null) return;
    // ON CONFLICT rather than a pre-read: the primary key already says one row per
    // session per second, so let the database enforce it in one round trip.
    await this.#run(
      `INSERT INTO vitals (session_id, t_server, sample_index, hr, hr_class, spo2,
                           spo2_source, spo2_quality, pulse_bpm, perfusion_pct,
                           rate_agreement, temp_c, signal_quality)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
       ON CONFLICT (session_id, t_server) DO UPDATE SET
         sample_index = EXCLUDED.sample_index, hr = EXCLUDED.hr, hr_class = EXCLUDED.hr_class,
         spo2 = EXCLUDED.spo2, spo2_source = EXCLUDED.spo2_source,
         spo2_quality = EXCLUDED.spo2_quality, pulse_bpm = EXCLUDED.pulse_bpm,
         perfusion_pct = EXCLUDED.perfusion_pct, rate_agreement = EXCLUDED.rate_agreement,
         temp_c = EXCLUDED.temp_c, signal_quality = EXCLUDED.signal_quality`,
      [id, ts(row.tServer), row.sampleIndex, row.hr, row.hrClass, row.spo2, row.spo2Source,
       row.spo2Quality, row.pulseBpm, row.perfusionPct, row.rateAgreement, row.tempC,
       row.signalQuality],
    );
  }

  async raiseAlarm(id, alarm) {
    if (id === null) return null;
    // One row per episode: only insert when no open row exists for this code.
    const { rows } = await this.#run(
      `INSERT INTO alarm_events (session_id, code, severity, message, raised_at)
       SELECT $1, $2, $3, $4, $5
        WHERE NOT EXISTS (
          SELECT 1 FROM alarm_events
           WHERE session_id = $1 AND code = $2 AND cleared_at IS NULL
        )
       RETURNING id`,
      [id, alarm.code, alarm.severity, alarm.message, ts(alarm.raisedAt)],
    );
    if (rows.length) return Number(rows[0].id);
    const open = await this.#run(
      `SELECT id FROM alarm_events WHERE session_id = $1 AND code = $2 AND cleared_at IS NULL`,
      [id, alarm.code],
    );
    return open.rows.length ? Number(open.rows[0].id) : null;
  }

  async acknowledgeAlarm(id, code, at, by) {
    if (id === null) return 0;
    const { rowCount } = await this.#run(
      `UPDATE alarm_events SET acknowledged_at = $3, acknowledged_by = $4
        WHERE session_id = $1 AND code = $2 AND cleared_at IS NULL AND acknowledged_at IS NULL`,
      [id, code, ts(at), by ?? null],
    );
    return rowCount ?? 0;
  }

  async clearAlarm(id, code, at) {
    if (id === null) return 0;
    const { rowCount } = await this.#run(
      `UPDATE alarm_events SET cleared_at = $3
        WHERE session_id = $1 AND code = $2 AND cleared_at IS NULL`,
      [id, code, ts(at)],
    );
    return rowCount ?? 0;
  }

  async listSessions({ limit = 50 } = {}) {
    const { rows } = await this.#run(
      `SELECT id, device_id, started_at, ended_at, sample_rate_hz, frames_received, frames_dropped
         FROM sessions ORDER BY started_at DESC, id DESC LIMIT $1`,
      [limit],
    );
    return rows.map((r) => ({
      id: Number(r.id),
      deviceId: r.device_id,
      startedAt: ms(r.started_at),
      endedAt: ms(r.ended_at),
      sampleRateHz: r.sample_rate_hz,
      framesReceived: Number(r.frames_received),
      framesDropped: Number(r.frames_dropped),
    }));
  }

  async getSession(id) {
    const list = await this.#run(
      `SELECT id, device_id, started_at, ended_at, sample_rate_hz, frames_received, frames_dropped
         FROM sessions WHERE id = $1`,
      [id],
    );
    if (!list.rows.length) return null;
    const r = list.rows[0];
    return {
      id: Number(r.id),
      deviceId: r.device_id,
      startedAt: ms(r.started_at),
      endedAt: ms(r.ended_at),
      sampleRateHz: r.sample_rate_hz,
      framesReceived: Number(r.frames_received),
      framesDropped: Number(r.frames_dropped),
    };
  }

  async getVitals(id, { from = null, to = null } = {}) {
    const { rows } = await this.#run(
      `SELECT * FROM vitals
        WHERE session_id = $1
          AND ($2::timestamptz IS NULL OR t_server >= $2)
          AND ($3::timestamptz IS NULL OR t_server <= $3)
        ORDER BY t_server`,
      [id, Number.isFinite(from) ? ts(from) : null, Number.isFinite(to) ? ts(to) : null],
    );
    return rows.map((r) => ({
      tServer: ms(r.t_server),
      sampleIndex: Number(r.sample_index),
      hr: r.hr,
      hrClass: r.hr_class,
      spo2: r.spo2,
      spo2Source: r.spo2_source,
      spo2Quality: r.spo2_quality,
      pulseBpm: r.pulse_bpm,
      perfusionPct: r.perfusion_pct,
      rateAgreement: r.rate_agreement,
      tempC: r.temp_c,
      signalQuality: r.signal_quality,
    }));
  }

  async getAlarmEvents(id) {
    const { rows } = await this.#run(
      `SELECT * FROM alarm_events WHERE session_id = $1 ORDER BY raised_at`,
      [id],
    );
    return rows.map((r) => ({
      id: Number(r.id),
      sessionId: Number(r.session_id),
      code: r.code,
      severity: r.severity,
      message: r.message,
      raisedAt: ms(r.raised_at),
      acknowledgedAt: ms(r.acknowledged_at),
      acknowledgedBy: r.acknowledged_by,
      clearedAt: ms(r.cleared_at),
    }));
  }

  async close() {
    await this.pool.end().catch(() => {});
  }
}
