/**
 * The storage contract, run against every implementation.
 *
 * `MemoryStore` always; `PostgresStore` too when `DATABASE_URL` is set. The point is
 * that persistence *behaviour* — one alarm row per episode, acknowledgement not
 * resurrecting cleared alarms, vitals keyed by second — is pinned without needing a
 * database running, so only the SQL binding depends on the container. Same discipline as
 * building the firmware for the host (D-25), applied to infrastructure.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { MemoryStore, PostgresStore } from '../src/storage/index.js';

const IMPLEMENTATIONS = [['MemoryStore', () => new MemoryStore()]];

if (process.env.DATABASE_URL) {
  IMPLEMENTATIONS.push([
    'PostgresStore',
    () => new PostgresStore({ connectionString: process.env.DATABASE_URL, log: { error() {}, info() {} } }),
  ]);
}

const T0 = Date.UTC(2026, 7, 19, 12, 0, 0);

for (const [name, make] of IMPLEMENTATIONS) {
  const wrap = (fn) => async (t) => {
    const store = make();
    await store.init();
    try {
      await fn(t, store);
    } finally {
      await store.close();
    }
  };

  test(`[${name}] a session round-trips`, wrap(async (t, store) => {
    const id = await store.createSession({ deviceId: 'dev-A', startedAt: T0, sampleRateHz: 360 });
    assert.ok(id !== null);
    const s = await store.getSession(id);
    assert.equal(s.deviceId, 'dev-A');
    assert.equal(s.startedAt, T0);
    assert.equal(s.endedAt, null, 'a live session has no end');
    assert.equal(s.sampleRateHz, 360);
  }));

  test(`[${name}] ending a session records when and how much`, wrap(async (t, store) => {
    const id = await store.createSession({ deviceId: 'dev-A', startedAt: T0, sampleRateHz: 360 });
    await store.endSession(id, T0 + 60_000, { framesReceived: 674, framesDropped: 2 });
    const s = await store.getSession(id);
    assert.equal(s.endedAt, T0 + 60_000);
    assert.equal(s.framesReceived, 674);
    assert.equal(s.framesDropped, 2);
  }));

  test(`[${name}] vitals are stored and read back in time order`, wrap(async (t, store) => {
    const id = await store.createSession({ deviceId: 'dev-A', startedAt: T0, sampleRateHz: 360 });
    for (const k of [2, 0, 1]) {
      await store.recordVitals(id, {
        tServer: T0 + k * 1000, sampleIndex: k * 360, hr: 70 + k, hrClass: 'normal',
        spo2: 97, spo2Source: 'server', spo2Quality: 'good', pulseBpm: 71 + k,
        perfusionPct: 2.5, rateAgreement: 'agree', tempC: 36.8, signalQuality: 'good',
      });
    }
    const rows = await store.getVitals(id);
    assert.equal(rows.length, 3);
    assert.deepEqual(rows.map((r) => r.tServer), [T0, T0 + 1000, T0 + 2000]);
    assert.equal(rows[1].hr, 71);
    assert.equal(rows[1].spo2Source, 'server');
  }));

  test(`[${name}] a second sample in the same second replaces, not duplicates`, wrap(async (t, store) => {
    const id = await store.createSession({ deviceId: 'dev-A', startedAt: T0, sampleRateHz: 360 });
    const row = { tServer: T0, sampleIndex: 0, hr: 70, hrClass: 'normal', spo2: null,
      spo2Source: null, spo2Quality: null, pulseBpm: null, perfusionPct: null,
      rateAgreement: null, tempC: null, signalQuality: 'good' };
    await store.recordVitals(id, row);
    await store.recordVitals(id, { ...row, hr: 88 });
    const rows = await store.getVitals(id);
    assert.equal(rows.length, 1, 'one row per session per second');
    assert.equal(rows[0].hr, 88, 'the later value wins');
  }));

  test(`[${name}] null vitals stay null rather than becoming zero`, wrap(async (t, store) => {
    const id = await store.createSession({ deviceId: 'dev-A', startedAt: T0, sampleRateHz: 360 });
    await store.recordVitals(id, {
      tServer: T0, sampleIndex: 0, hr: null, hrClass: 'insufficient data', spo2: null,
      spo2Source: null, spo2Quality: null, pulseBpm: null, perfusionPct: null,
      rateAgreement: null, tempC: null, signalQuality: 'unusable',
    });
    const [row] = await store.getVitals(id);
    assert.equal(row.hr, null, 'an unknown heart rate must not read back as 0 bpm');
    assert.equal(row.spo2, null);
  }));

  test(`[${name}] an alarm episode is one row, however often it is evaluated`, wrap(async (t, store) => {
    const id = await store.createSession({ deviceId: 'dev-A', startedAt: T0, sampleRateHz: 360 });
    const alarm = { code: 'LEADS_OFF', severity: 'high', message: 'ECG electrodes detached', raisedAt: T0 };
    const first = await store.raiseAlarm(id, alarm);
    const second = await store.raiseAlarm(id, { ...alarm, raisedAt: T0 + 1000 });
    assert.equal(first, second, 're-raising an active alarm returns the same episode');
    const events = await store.getAlarmEvents(id);
    assert.equal(events.length, 1);
    assert.equal(events[0].raisedAt, T0, 'the episode keeps its original onset time');
  }));

  test(`[${name}] acknowledging marks the open episode only`, wrap(async (t, store) => {
    const id = await store.createSession({ deviceId: 'dev-A', startedAt: T0, sampleRateHz: 360 });
    await store.raiseAlarm(id, { code: 'SPO2_LOW', severity: 'medium', message: 'low', raisedAt: T0 });
    const n = await store.acknowledgeAlarm(id, 'SPO2_LOW', T0 + 5000, 'nurse-1');
    assert.equal(n, 1);
    const [e] = await store.getAlarmEvents(id);
    assert.equal(e.acknowledgedAt, T0 + 5000);
    assert.equal(e.acknowledgedBy, 'nurse-1');
    assert.equal(await store.acknowledgeAlarm(id, 'SPO2_LOW', T0 + 9000, 'nurse-2'), 0,
      'acknowledging twice does not re-acknowledge');
  }));

  test(`[${name}] clearing closes the episode and a recurrence opens a new one`, wrap(async (t, store) => {
    const id = await store.createSession({ deviceId: 'dev-A', startedAt: T0, sampleRateHz: 360 });
    const alarm = { code: 'TACHYCARDIA', severity: 'medium', message: 'fast', raisedAt: T0 };
    await store.raiseAlarm(id, alarm);
    assert.equal(await store.clearAlarm(id, 'TACHYCARDIA', T0 + 10_000), 1);
    await store.raiseAlarm(id, { ...alarm, raisedAt: T0 + 20_000 });
    const events = await store.getAlarmEvents(id);
    assert.equal(events.length, 2, 'a condition that returns is a new episode, not a reopened one');
    assert.equal(events[0].clearedAt, T0 + 10_000);
    assert.equal(events[1].clearedAt, null);
  }));

  test(`[${name}] acknowledging a cleared alarm does nothing`, wrap(async (t, store) => {
    const id = await store.createSession({ deviceId: 'dev-A', startedAt: T0, sampleRateHz: 360 });
    await store.raiseAlarm(id, { code: 'ASYSTOLE', severity: 'high', message: 'x', raisedAt: T0 });
    await store.clearAlarm(id, 'ASYSTOLE', T0 + 1000);
    assert.equal(await store.acknowledgeAlarm(id, 'ASYSTOLE', T0 + 2000, 'nurse'), 0);
  }));

  test(`[${name}] sessions list newest first`, wrap(async (t, store) => {
    // Deliberately does not assume an empty store. A real database keeps rows from
    // earlier runs, so the contract has to hold for a store that already has history -
    // which is the normal case in production and was hidden by the in-memory
    // implementation starting clean every time (D-45).
    const tag = `order-${Math.random().toString(36).slice(2, 10)}`;
    const a = await store.createSession({ deviceId: `${tag}-A`, startedAt: T0, sampleRateHz: 360 });
    const b = await store.createSession({ deviceId: `${tag}-B`, startedAt: T0 + 5000, sampleRateHz: 360 });

    const mine = (await store.listSessions({ limit: 500 })).filter((s) => s.deviceId.startsWith(tag));
    assert.deepEqual(mine.map((s) => s.id), [b, a], 'newest first among this run\'s sessions');
    assert.equal((await store.listSessions({ limit: 1 })).length, 1, 'limit is honoured');
  }));

  test(`[${name}] sessions with identical timestamps still order deterministically`, wrap(async (t, store) => {
    // Two devices can connect in the same millisecond. Ordering by time alone leaves
    // that tie to the storage engine, and an order that is not total makes pagination
    // able to skip or repeat rows.
    const tag = `tie-${Math.random().toString(36).slice(2, 10)}`;
    const first = await store.createSession({ deviceId: `${tag}-1`, startedAt: T0, sampleRateHz: 360 });
    const second = await store.createSession({ deviceId: `${tag}-2`, startedAt: T0, sampleRateHz: 360 });

    const seen = [];
    for (let i = 0; i < 3; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      const list = await store.listSessions({ limit: 500 });
      seen.push(list.filter((s) => s.deviceId.startsWith(tag)).map((s) => s.id).join(','));
    }
    assert.equal(new Set(seen).size, 1, `order varied between reads: ${seen.join(' | ')}`);
    assert.equal(seen[0], `${second},${first}`, 'the later row wins the tie, consistently');
  }));

  test(`[${name}] an unknown session yields null and empty lists, not a throw`, wrap(async (t, store) => {
    assert.equal(await store.getSession(999_999), null);
    assert.deepEqual(await store.getVitals(999_999), []);
    assert.deepEqual(await store.getAlarmEvents(999_999), []);
  }));

  test(`[${name}] alarm events for a session come back in onset order`, wrap(async (t, store) => {
    const id = await store.createSession({ deviceId: 'dev-A', startedAt: T0, sampleRateHz: 360 });
    await store.raiseAlarm(id, { code: 'B', severity: 'low', message: 'b', raisedAt: T0 + 2000 });
    await store.raiseAlarm(id, { code: 'A', severity: 'high', message: 'a', raisedAt: T0 });
    const events = await store.getAlarmEvents(id);
    assert.deepEqual(events.map((e) => e.code), ['A', 'B']);
  }));
}

test('NullStore satisfies the contract while storing nothing', async () => {
  const { NullStore } = await import('../src/storage/store.js');
  const store = new NullStore();
  await store.init();
  assert.equal(store.enabled, false);
  const id = await store.createSession({ deviceId: 'x', startedAt: T0, sampleRateHz: 360 });
  assert.equal(id, null, 'no session id, so callers must tolerate one');
  await store.recordVitals(id, {});
  await store.raiseAlarm(id, { code: 'X' });
  assert.deepEqual(await store.listSessions(), []);
  await store.close();
});

// ------------------------------------------------- the live path writes history ---

test('a running session records vitals and alarm episodes', async () => {
  const { createVitalSenseServer } = await import('../src/index.js');
  const store = new MemoryStore();
  await store.init();
  const server = createVitalSenseServer({ port: 0, log: { info() {}, warn() {}, error() {} }, store });
  const port = await server.listen();

  const { default: WebSocket } = await import('ws');
  const ws = new WebSocket(`ws://127.0.0.1:${port}/ingest`);

  try {
    await new Promise((res, rej) => { ws.once('open', res); ws.once('error', rej); });

    // Leads off from the first frame. The alarm needs its 1.5 s debounce to elapse in
    // *wall* time (D-18), so the frames have to be spread across more than that - an
    // earlier version of this test sent 40 frames in 480 ms and then asserted an alarm
    // that could not physically have fired yet.
    for (let i = 0; i < 90; i += 1) {
      ws.send(JSON.stringify({
        v: 2, deviceId: 'hist-1', seq: i, tDevice: i * 89, fs: 360,
        ecg: new Array(32).fill(0.05), leadsOff: true, spo2: null, temp: 36.8,
      }));
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => setTimeout(r, 25));
    }
    await new Promise((r) => setTimeout(r, 250));

    const sessions = await store.listSessions();
    assert.equal(sessions.length, 1, 'the connection created exactly one session row');
    assert.equal(sessions[0].deviceId, 'hist-1');

    const vitals = await store.getVitals(sessions[0].id);
    assert.ok(vitals.length >= 1, `expected persisted vitals, got ${vitals.length}`);
    assert.ok(vitals.every((r) => r.tServer % 1000 === 0), 'history is stored at 1 Hz');

    const events = await store.getAlarmEvents(sessions[0].id);
    const leadsOff = events.filter((e) => e.code === 'LEADS_OFF');
    assert.equal(leadsOff.length, 1, 'a sustained condition is one episode, not one row per frame');
    assert.equal(leadsOff[0].clearedAt, null, 'the episode is still open while the condition holds');
  } finally {
    // In the finally, not after the assertions: a failed assertion used to skip this and
    // leave the socket open, so the test runner hung instead of reporting the failure.
    ws.close();
    await server.close();
    await store.close();
  }
});
