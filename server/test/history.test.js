/**
 * Session history and CSV export.
 *
 * Runs against MemoryStore, so the routes are verified without a database - the SQL that
 * backs them is covered by the shared contract suite in storage.test.js.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { createVitalSenseServer } from '../src/index.js';
import { MemoryStore, NullStore } from '../src/storage/index.js';
import { toCsv } from '../src/history.js';

const T0 = Date.UTC(2026, 7, 19, 12, 0, 0);
const quiet = { info() {}, warn() {}, error() {} };

const row = (k, over = {}) => ({
  tServer: T0 + k * 1000, sampleIndex: k * 360, hr: 72 + k, hrClass: 'normal',
  spo2: 97, spo2Source: 'server', spo2Quality: 'good', pulseBpm: 71 + k,
  perfusionPct: 2.4, rateAgreement: 'agree', tempC: 36.8, signalQuality: 'good', ...over,
});

async function withServer(store, fn) {
  const server = createVitalSenseServer({ port: 0, log: quiet, store });
  const port = await server.listen();
  try {
    await fn(`http://127.0.0.1:${port}`);
  } finally {
    await server.close();
  }
}

async function seed() {
  const store = new MemoryStore();
  await store.init();
  const id = await store.createSession({ deviceId: 'bed-3', startedAt: T0, sampleRateHz: 360 });
  for (let k = 0; k < 5; k += 1) await store.recordVitals(id, row(k));
  await store.raiseAlarm(id, { code: 'SPO2_LOW', severity: 'medium', message: 'SpO2 low', raisedAt: T0 + 1000 });
  await store.acknowledgeAlarm(id, 'SPO2_LOW', T0 + 2000, 'nurse-1');
  await store.clearAlarm(id, 'SPO2_LOW', T0 + 4000);
  await store.endSession(id, T0 + 5000, { framesReceived: 56, framesDropped: 0 });
  return { store, id };
}

test('sessions can be listed', async () => {
  const { store } = await seed();
  await withServer(store, async (base) => {
    const res = await fetch(`${base}/api/sessions`);
    assert.equal(res.status, 200);
    const { sessions } = await res.json();
    assert.equal(sessions.length, 1);
    assert.equal(sessions[0].deviceId, 'bed-3');
    assert.equal(sessions[0].framesReceived, 56);
  });
});

test('a session detail carries its alarm timeline', async () => {
  const { store, id } = await seed();
  await withServer(store, async (base) => {
    const { session, alarms } = await (await fetch(`${base}/api/sessions/${id}`)).json();
    assert.equal(session.deviceId, 'bed-3');
    assert.equal(alarms.length, 1);
    // The three timestamps are what make "how long did this alarm sound before anyone
    // silenced it" answerable rather than guessed.
    assert.equal(alarms[0].raisedAt, T0 + 1000);
    assert.equal(alarms[0].acknowledgedAt, T0 + 2000);
    assert.equal(alarms[0].clearedAt, T0 + 4000);
    assert.equal(alarms[0].acknowledgedBy, 'nurse-1');
  });
});

test('vitals can be fetched and filtered by time', async () => {
  const { store, id } = await seed();
  await withServer(store, async (base) => {
    const all = await (await fetch(`${base}/api/sessions/${id}/vitals`)).json();
    assert.equal(all.vitals.length, 5);
    const some = await (await fetch(`${base}/api/sessions/${id}/vitals?from=${T0 + 2000}&to=${T0 + 3000}`)).json();
    assert.equal(some.vitals.length, 2);
  });
});

test('CSV export is downloadable and names itself usefully', async () => {
  const { store, id } = await seed();
  await withServer(store, async (base) => {
    const res = await fetch(`${base}/api/sessions/${id}/vitals.csv`);
    assert.equal(res.status, 200);
    assert.match(res.headers.get('content-type'), /text\/csv/);
    assert.match(res.headers.get('content-disposition'), /attachment; filename="vitalsense-bed-3-.*\.csv"/);
    const text = await res.text();
    const lines = text.trim().split('\n');
    assert.equal(lines.length, 6, 'header plus five rows');
    assert.match(lines[0], /^timestamp_iso,epoch_ms,sample_index,hr_bpm/);
    assert.match(lines[1], /^2026-08-19T12:00:00\.000Z,/);
    assert.ok(text.endsWith('\n'), 'the file ends with a newline');
  });
});

test('a missing measurement exports as an empty field, never as zero', async () => {
  // A spreadsheet reading 0 bpm where the monitor meant "I don't know" is the display
  // failure of D-23 surviving into every plot anyone makes from the export.
  const csv = toCsv([row(0, { hr: null, spo2: null, hrClass: 'insufficient data', signalQuality: 'unusable' })]);
  const [, data] = csv.trim().split('\n');
  const fields = data.split(',');
  assert.equal(fields[3], '', 'hr_bpm is blank, not 0');
  assert.equal(fields[5], '', 'spo2_pct is blank, not 0');
  assert.ok(!csv.includes('null'), 'the literal string "null" never appears');
});

test('fields containing commas or quotes are escaped', async () => {
  const csv = toCsv([row(0, { hrClass: 'odd, "quoted"' })]);
  assert.ok(csv.includes('"odd, ""quoted"""'), csv);
});

test('an unknown session is a 404, not an empty success', async () => {
  const { store } = await seed();
  await withServer(store, async (base) => {
    assert.equal((await fetch(`${base}/api/sessions/999999`)).status, 404);
    assert.equal((await fetch(`${base}/api/sessions/999999/vitals.csv`)).status, 404);
    assert.equal((await fetch(`${base}/api/nonsense`)).status, 404);
  });
});

test('with no database, history says so rather than pretending to be empty', async () => {
  // "No history configured" and "no such session" are different problems, and
  // conflating them sends someone hunting for a recording that never existed.
  await withServer(new NullStore(), async (base) => {
    const res = await fetch(`${base}/api/sessions`);
    assert.equal(res.status, 501);
    const body = await res.json();
    assert.match(body.detail, /DATABASE_URL/);
  });
});

test('the dashboard and health endpoint still work alongside the API', async () => {
  const { store } = await seed();
  await withServer(store, async (base) => {
    assert.equal((await fetch(`${base}/health`)).status, 200);
    const page = await fetch(`${base}/`);
    assert.equal(page.status, 200);
    assert.match(await page.text(), /VitalSense/);
  });
});
