/**
 * The storage contract.
 *
 * Three implementations satisfy it: `NullStore` (the default - the system runs with no
 * database at all, so the demo still starts with one command), `MemoryStore` (used by
 * the contract tests, so the *behaviour* is verified without needing Postgres running),
 * and `PostgresStore`.
 *
 * Splitting it this way is not ceremony. Persistence is the one part of this system that
 * cannot be exercised on a laptop without extra infrastructure, and the project's rule
 * has been that anything untestable without hardware gets a testable substitute (D-25,
 * now applied to a database instead of a board). The same contract suite runs against
 * MemoryStore always and against PostgresStore when `DATABASE_URL` is set, so the SQL is
 * the only part that needs the container.
 *
 * Every method is async, including the in-memory ones, so that swapping implementations
 * cannot change the caller's control flow.
 */

/** @typedef {{deviceId: string, startedAt: number, sampleRateHz: number}} SessionInit */

export class NullStore {
  // eslint-disable-next-line class-methods-use-this
  get enabled() {
    return false;
  }

  async init() {}
  async createSession() { return null; }
  async endSession() {}
  async recordVitals() {}
  async raiseAlarm() { return null; }
  async acknowledgeAlarm() { return 0; }
  async clearAlarm() { return 0; }
  async listSessions() { return []; }
  async getSession() { return null; }
  async getVitals() { return []; }
  async getAlarmEvents() { return []; }
  async close() {}
}

export class MemoryStore {
  constructor() {
    this.sessions = new Map();
    this.vitals = new Map();
    this.alarms = new Map();
    this.nextSessionId = 1;
    this.nextAlarmId = 1;
  }

  get enabled() {
    return true;
  }

  async init() {}

  async createSession({ deviceId, startedAt, sampleRateHz }) {
    const id = this.nextSessionId++;
    this.sessions.set(id, {
      id,
      deviceId,
      startedAt,
      endedAt: null,
      sampleRateHz,
      framesReceived: 0,
      framesDropped: 0,
    });
    this.vitals.set(id, []);
    this.alarms.set(id, []);
    return id;
  }

  async endSession(id, endedAt, stats = {}) {
    const s = this.sessions.get(id);
    if (!s) return;
    s.endedAt = endedAt;
    if (stats.framesReceived !== undefined) s.framesReceived = stats.framesReceived;
    if (stats.framesDropped !== undefined) s.framesDropped = stats.framesDropped;
  }

  async recordVitals(id, row) {
    const rows = this.vitals.get(id);
    if (!rows) return;
    // Same primary key as the SQL schema: one row per session per timestamp.
    const existing = rows.findIndex((r) => r.tServer === row.tServer);
    if (existing >= 0) rows[existing] = { ...row };
    else rows.push({ ...row });
  }

  async raiseAlarm(id, alarm) {
    const rows = this.alarms.get(id);
    if (!rows) return null;
    const open = rows.find((a) => a.code === alarm.code && a.clearedAt === null);
    if (open) return open.id; // one row per episode, not per evaluation
    const event = {
      id: this.nextAlarmId++,
      sessionId: id,
      code: alarm.code,
      severity: alarm.severity,
      message: alarm.message,
      raisedAt: alarm.raisedAt,
      acknowledgedAt: null,
      acknowledgedBy: null,
      clearedAt: null,
    };
    rows.push(event);
    return event.id;
  }

  async acknowledgeAlarm(id, code, at, by) {
    const rows = this.alarms.get(id) ?? [];
    let n = 0;
    for (const a of rows) {
      if (a.code === code && a.clearedAt === null && a.acknowledgedAt === null) {
        a.acknowledgedAt = at;
        a.acknowledgedBy = by ?? null;
        n += 1;
      }
    }
    return n;
  }

  async clearAlarm(id, code, at) {
    const rows = this.alarms.get(id) ?? [];
    let n = 0;
    for (const a of rows) {
      if (a.code === code && a.clearedAt === null) {
        a.clearedAt = at;
        n += 1;
      }
    }
    return n;
  }

  async listSessions({ limit = 50 } = {}) {
    return [...this.sessions.values()]
      // id breaks ties: two devices can connect in the same millisecond, and an order
      // that is not total makes pagination able to skip or repeat rows.
      .sort((a, b) => b.startedAt - a.startedAt || b.id - a.id)
      .slice(0, limit)
      .map((s) => ({ ...s }));
  }

  async getSession(id) {
    const s = this.sessions.get(Number(id));
    return s ? { ...s } : null;
  }

  async getVitals(id, { from = -Infinity, to = Infinity } = {}) {
    return (this.vitals.get(Number(id)) ?? [])
      .filter((r) => r.tServer >= from && r.tServer <= to)
      .sort((a, b) => a.tServer - b.tServer)
      .map((r) => ({ ...r }));
  }

  async getAlarmEvents(id) {
    return (this.alarms.get(Number(id)) ?? [])
      .slice()
      .sort((a, b) => a.raisedAt - b.raisedAt)
      .map((a) => ({ ...a }));
  }

  async close() {}
}
