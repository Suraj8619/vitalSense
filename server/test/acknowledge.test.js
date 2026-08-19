/**
 * Alarm acknowledgement and the re-alarm policy.
 *
 * The behaviour under test is clinical, not mechanical: acknowledging is a *silence*, it
 * expires, and it belongs to one episode rather than to a code forever.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { AlarmEngine, REALARM_MS } from '../src/alarms.js';

const T0 = Date.UTC(2026, 7, 19, 12, 0, 0);

/** Drive the engine to a sounding LEADS_OFF alarm and return the time it started. */
function soundLeadsOff(engine, start = T0) {
  const context = { leadsOff: true, hr: null, hrClass: 'insufficient data', spo2: null,
    signalQuality: 'unusable', secondsSinceLastBeat: 0, secondsSinceLastFrame: 0 };
  engine.update(context, start);
  const active = engine.update(context, start + 2000);
  assert.ok(active.some((a) => a.code === 'LEADS_OFF'), 'expected LEADS_OFF to be sounding');
  return context;
}

test('an alarm can be silenced once it is sounding', () => {
  const engine = new AlarmEngine();
  const context = soundLeadsOff(engine);
  assert.equal(engine.acknowledge('LEADS_OFF', T0 + 2500, 'nurse-1'), true);
  const [alarm] = engine.update(context, T0 + 3000).filter((a) => a.code === 'LEADS_OFF');
  assert.equal(alarm.acknowledged, true);
  assert.equal(alarm.acknowledgedBy, 'nurse-1');
  assert.equal(alarm.resoundsAt, T0 + 2500 + REALARM_MS.high);
});

test('an alarm that is not sounding cannot be pre-silenced', () => {
  // Otherwise a clinician could mute an alarm before the condition arises, which is the
  // shortcut that makes acknowledgement dangerous.
  const engine = new AlarmEngine();
  assert.equal(engine.acknowledge('LEADS_OFF', T0, 'nurse-1'), false);
  assert.equal(engine.acknowledge('ASYSTOLE', T0, 'nurse-1'), false);
});

test('a silence expires and the alarm sounds again if the condition holds', () => {
  const engine = new AlarmEngine();
  const context = soundLeadsOff(engine);
  engine.acknowledge('LEADS_OFF', T0 + 2500, 'nurse-1');

  const stillSilenced = engine.update(context, T0 + 2500 + REALARM_MS.high - 1000)
    .find((a) => a.code === 'LEADS_OFF');
  assert.equal(stillSilenced.acknowledged, true, 'inside the window it stays silent');

  const resounded = engine.update(context, T0 + 2500 + REALARM_MS.high + 1000)
    .find((a) => a.code === 'LEADS_OFF');
  assert.equal(resounded.acknowledged, false, 'the patient has not changed, only the silence expired');
  assert.equal(resounded.since, T0, 'and it is still the same episode');
});

test('high-severity alarms return sooner than low-severity ones', () => {
  // A silenced asystole is not something to leave silenced as long as a signal-quality
  // notice.
  assert.ok(REALARM_MS.high < REALARM_MS.medium);
  assert.ok(REALARM_MS.medium < REALARM_MS.low);
});

test('a condition that clears and returns is a new, unsilenced episode', () => {
  const engine = new AlarmEngine();
  const context = soundLeadsOff(engine);
  engine.acknowledge('LEADS_OFF', T0 + 2500, 'nurse-1');

  // Electrodes reattached: the alarm clears.
  const clear = { ...context, leadsOff: false };
  assert.equal(engine.update(clear, T0 + 5000).some((a) => a.code === 'LEADS_OFF'), false);

  // And come off again a minute later - well inside the old silence window.
  engine.update(context, T0 + 60_000);
  const again = engine.update(context, T0 + 62_000).find((a) => a.code === 'LEADS_OFF');
  assert.ok(again, 'the alarm sounds again');
  assert.equal(again.acknowledged, false, 'the clinician silenced the previous event, not this one');
  assert.equal(again.since, T0 + 60_000, 'and it is a new episode');
});

test('acknowledging one alarm does not silence another', () => {
  const engine = new AlarmEngine();
  const context = { leadsOff: true, hr: null, hrClass: 'insufficient data', spo2: 85,
    signalQuality: 'unusable', secondsSinceLastBeat: 0, secondsSinceLastFrame: 0 };
  engine.update(context, T0);
  engine.update(context, T0 + 12_000);
  engine.acknowledge('LEADS_OFF', T0 + 12_000, 'nurse-1');
  const active = engine.update(context, T0 + 12_500);
  const byCode = Object.fromEntries(active.map((a) => [a.code, a]));
  assert.equal(byCode.LEADS_OFF.acknowledged, true);
  if (byCode.SPO2_LOW) assert.equal(byCode.SPO2_LOW.acknowledged, false);
});

test('reset clears acknowledgements along with alarm state', () => {
  const engine = new AlarmEngine();
  const context = soundLeadsOff(engine);
  engine.acknowledge('LEADS_OFF', T0 + 2500, 'nurse-1');
  engine.reset();
  engine.update(context, T0 + 100_000);
  const after = engine.update(context, T0 + 102_000).find((a) => a.code === 'LEADS_OFF');
  assert.equal(after.acknowledged, false);
});

// ------------------------------------------------------- over the dashboard link ---

import { createVitalSenseServer } from '../src/index.js';
import { MemoryStore } from '../src/storage/index.js';

const quiet = { info() {}, warn() {}, error() {} };

/**
 * Wait for a socket to be open.
 *
 * Attaching `once('open')` after the event has already fired waits forever, and a socket
 * opened before some other await can easily be connected by the time anyone listens - so
 * check readyState first rather than assuming the listener wins the race.
 */
function opened(ws) {
  if (ws.readyState === ws.OPEN) return Promise.resolve();
  return new Promise((res, rej) => { ws.once('open', res); ws.once('error', rej); });
}

/** Open a dashboard socket and collect messages by type. */
async function openDashboard(port) {
  const { default: WebSocket } = await import('ws');
  const ws = new WebSocket(`ws://127.0.0.1:${port}/stream`);
  const seen = [];
  ws.on('message', (m) => seen.push(JSON.parse(m.toString())));
  await opened(ws);
  // Bounded: an unbounded poll turns "the message never arrived" into a hung test run
  // instead of a failure, which costs far more to diagnose than it saves.
  return { ws, seen, next: (type, timeoutMs = 3000) => new Promise((res, rej) => {
    const deadline = Date.now() + timeoutMs;
    const check = () => {
      const hit = seen.find((m) => m.type === type);
      if (hit) return res(hit);
      if (Date.now() > deadline) {
        return rej(new Error(`no "${type}" message within ${timeoutMs} ms; saw: ${seen.map((m) => m.type).join(', ') || 'nothing'}`));
      }
      return setTimeout(check, 10);
    };
    check();
  }) };
}

test('a dashboard can silence a sounding alarm, and it is recorded', async () => {
  const store = new MemoryStore();
  await store.init();
  const server = createVitalSenseServer({ port: 0, log: quiet, store });
  const port = await server.listen();
  const { default: WebSocket } = await import('ws');
  const device = new WebSocket(`ws://127.0.0.1:${port}/ingest`);
  const dash = await openDashboard(port);

  try {
    await opened(device);

    // Two seconds of leads-off, so the 1.5 s debounce elapses in wall time.
    for (let i = 0; i < 80; i += 1) {
      device.send(JSON.stringify({
        v: 2, deviceId: 'bed-9', seq: i, tDevice: i * 89, fs: 360,
        ecg: new Array(32).fill(0.05), leadsOff: true, spo2: null, temp: 36.8,
      }));
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => setTimeout(r, 28));
    }

    dash.ws.send(JSON.stringify({ type: 'acknowledge', deviceId: 'bed-9', code: 'LEADS_OFF', by: 'nurse-7' }));
    const reply = await dash.next('acknowledged');
    assert.equal(reply.ok, true, reply.reason ?? '');
    assert.equal(reply.code, 'LEADS_OFF');

    await new Promise((r) => setTimeout(r, 150));
    const [session] = await store.listSessions();
    const events = await store.getAlarmEvents(session.id);
    const leadsOff = events.find((e) => e.code === 'LEADS_OFF');
    assert.ok(leadsOff, 'the episode was recorded');
    assert.ok(leadsOff.acknowledgedAt !== null, 'and so was the acknowledgement');
    assert.equal(leadsOff.acknowledgedBy, 'nurse-7');
  } finally {
    device.close();
    dash.ws.close();
    await server.close();
    await store.close();
  }
});

test('a dashboard cannot acknowledge an alarm on a device that is not there', async () => {
  const server = createVitalSenseServer({ port: 0, log: quiet, store: new MemoryStore() });
  const port = await server.listen();
  const dash = await openDashboard(port);
  try {
    dash.ws.send(JSON.stringify({ type: 'acknowledge', deviceId: 'ghost', code: 'LEADS_OFF' }));
    const reply = await dash.next('error');
    assert.match(reply.errors.join(' '), /no active session for ghost/);
  } finally {
    dash.ws.close();
    await server.close();
  }
});

test('a dashboard cannot send anything but an acknowledgement', async () => {
  // A viewer must not be able to reconfigure a monitor.
  const server = createVitalSenseServer({ port: 0, log: quiet, store: new MemoryStore() });
  const port = await server.listen();
  const dash = await openDashboard(port);
  try {
    dash.ws.send(JSON.stringify({ type: 'setThreshold', code: 'TACHYCARDIA', value: 999 }));
    const reply = await dash.next('error');
    assert.match(reply.errors.join(' '), /unsupported message type/);
  } finally {
    dash.ws.close();
    await server.close();
  }
});

test('malformed dashboard input is rejected, not crashed on', async () => {
  const server = createVitalSenseServer({ port: 0, log: quiet, store: new MemoryStore() });
  const port = await server.listen();
  const dash = await openDashboard(port);
  try {
    dash.ws.send('{not json');
    const reply = await dash.next('error');
    assert.match(reply.errors.join(' '), /not valid JSON/);
  } finally {
    dash.ws.close();
    await server.close();
  }
});

test('a device that goes silent keeps producing vitals frames instead of crashing', async () => {
  // The watchdog path builds a vitals frame with no incoming frame to draw on. An edit
  // that assumed a frame was always present crashed the whole server here, three
  // seconds after any device stopped sending - and every existing test kept its device
  // talking, so nothing caught it until the system was actually run.
  const store = new MemoryStore();
  await store.init();
  const server = createVitalSenseServer({ port: 0, log: quiet, store });
  const port = await server.listen();
  const { default: WebSocket } = await import('ws');
  const device = new WebSocket(`ws://127.0.0.1:${port}/ingest`);
  const dash = await openDashboard(port);

  try {
    await opened(device);
    for (let i = 0; i < 10; i += 1) {
      device.send(JSON.stringify({
        v: 2, deviceId: 'quiet-1', seq: i, tDevice: i * 89, fs: 360,
        ecg: new Array(32).fill(0.05), leadsOff: false, spo2: null, temp: 36.8,
      }));
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => setTimeout(r, 20));
    }

    // Now go quiet and let the watchdog run past its 5 s silence threshold.
    device.close();
    const silent = await dash.next('vitals', 12_000);
    assert.ok(silent, 'the watchdog still emits');

    await new Promise((r) => setTimeout(r, 6500));
    const late = dash.seen.filter((m) => m.type === 'vitals').pop();
    assert.equal(late.hr, null, 'no heart rate is claimed for a device that stopped sending');
    assert.equal(late.spo2, null);
    assert.ok(late.alarms.some((a) => a.code === 'DEVICE_SILENT'), 'and the silence is alarmed');
  } finally {
    dash.ws.close();
    await server.close();
    await store.close();
  }
});
