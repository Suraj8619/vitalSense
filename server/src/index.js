/**
 * VitalSense ingest server.
 *
 * Two WebSocket endpoints:
 *   /ingest  - devices (ESP32 firmware, or sim/replay.py) send sample frames
 *   /stream  - dashboards subscribe to processed vitals
 *
 * Devices dial out to the server rather than the reverse, so the ESP32 works behind
 * NAT with no inbound firewall rules. Dashboards are pure subscribers: they never
 * send commands, which keeps the acquisition path free of anything a browser could
 * influence.
 */

import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { WebSocketServer } from 'ws';

import { SAMPLE_RATE_HZ } from './dsp/coefficients.js';
import { NullStore } from './storage/store.js';
import { handleHistoryRequest } from './history.js';
import { makeErrorFrame, PROTOCOL_VERSION, validateSampleFrame } from './protocol.js';
import { DeviceSession } from './session.js';

const PUBLIC_DIR = join(fileURLToPath(new URL('.', import.meta.url)), '..', 'public');

/** Content types for the handful of file kinds the dashboard uses. */
const CONTENT_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
};

const PORT = Number(process.env.PORT ?? 8080);
const HOST = process.env.HOST ?? '127.0.0.1';
/** How often silent devices are re-checked. */
const TICK_MS = 1000;
/**
 * How long a disconnected device's session is kept before it is discarded.
 *
 * Kept rather than dropped immediately so a device that drops WiFi and reconnects
 * resumes the same session instead of starting a fresh one with empty filter state
 * (requirement SR-05). Discarded eventually so a finished session does not alarm
 * DEVICE_SILENT forever.
 */
const SESSION_GRACE_MS = 30000;
/** Frames larger than this are refused before parsing. */
const MAX_FRAME_BYTES = 256 * 1024;

export function createVitalSenseServer({
  port = PORT,
  host = HOST,
  log = console,
  store = new NullStore(),
} = {}) {
  const httpServer = createServer(async (req, res) => {
    const { pathname } = new URL(req.url, `http://${req.headers.host ?? 'localhost'}`);

    // A plain HTTP health endpoint, so liveness can be checked without a WS client.
    if (pathname === '/health') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok', version: PROTOCOL_VERSION, fs: SAMPLE_RATE_HZ }));
      return;
    }

    // Session history and export. Read-only, and behind /api/ so it cannot collide
    // with a dashboard asset name.
    if (pathname.startsWith('/api/')) {
      try {
        await handleHistoryRequest(req, res, new URL(req.url, `http://${req.headers.host ?? 'localhost'}`), store);
      } catch (err) {
        log.error?.(`history request failed: ${err.message}`);
        if (!res.headersSent) res.writeHead(500, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: 'history request failed' }));
      }
      return;
    }

    // Static dashboard. Serving it from the same process as the WebSocket endpoint
    // means one command starts the whole demo.
    const requested = pathname === '/' ? '/index.html' : pathname;
    // normalize() collapses '..' segments; the prefix check then rejects anything
    // that still points outside the public directory.
    const target = join(PUBLIC_DIR, normalize(requested));
    if (!target.startsWith(PUBLIC_DIR + sep)) {
      res.writeHead(403).end();
      return;
    }
    try {
      const body = await readFile(target);
      res.writeHead(200, { 'content-type': CONTENT_TYPES[extname(target)] ?? 'application/octet-stream' });
      res.end(body);
    } catch {
      res.writeHead(404, { 'content-type': 'text/plain' }).end('not found');
    }
  });

  const ingestWss = new WebSocketServer({ noServer: true, maxPayload: MAX_FRAME_BYTES });
  const streamWss = new WebSocketServer({ noServer: true, maxPayload: 1024 });

  /** @type {Map<string, DeviceSession>} */
  const sessions = new Map();
  /** @type {Set<import('ws').WebSocket>} */
  const dashboards = new Set();

  const broadcast = (vitals) => {
    const payload = JSON.stringify(vitals);
    for (const client of dashboards) {
      // readyState 1 === OPEN. Sending to a closing socket throws.
      if (client.readyState === 1) client.send(payload);
    }
  };

  // --- device connections -------------------------------------------------------
  ingestWss.on('connection', (socket, request) => {
    let session = null;
    const peer = request.socket.remoteAddress;
    log.info?.(`device connected from ${peer}`);

    socket.on('message', (data) => {
      let parsed;
      try {
        parsed = JSON.parse(data.toString());
      } catch {
        socket.send(JSON.stringify(makeErrorFrame(['frame is not valid JSON'])));
        return;
      }

      const { ok, errors, warnings, frame } = validateSampleFrame(parsed);
      if (!ok) {
        if (session) session.stats.framesRejected += 1;
        log.warn?.(`rejected frame from ${peer}: ${errors.join('; ')}`);
        socket.send(JSON.stringify(makeErrorFrame(errors)));
        return;
      }
      if (warnings.length) log.warn?.(`frame ${frame.seq} from ${frame.deviceId}: ${warnings.join('; ')}`);

      if (!session) {
        session = sessions.get(frame.deviceId);
        if (!session) {
          session = new DeviceSession(frame.deviceId, { fs: frame.fs, store });
          sessions.set(frame.deviceId, session);
          log.info?.(`session started for ${frame.deviceId}`);
          // Not awaited: the first frame is already being processed, and a slow
          // database must not delay acquisition. Until the id lands, #persist is a
          // no-op, so at worst the first second of history is missing (D-43).
          void store
            .createSession({
              deviceId: frame.deviceId,
              startedAt: session.startedAt,
              sampleRateHz: frame.fs,
            })
            .then((id) => {
              session.storeSessionId = id;
            })
            .catch(() => {});
        } else if (session.disconnectedAt) {
          log.info?.(`device ${frame.deviceId} reconnected, resuming session`);
          session.disconnectedAt = null;
        }
      } else if (session.deviceId !== frame.deviceId) {
        // One connection carrying frames for two device IDs would interleave two
        // patients' signals through one filter chain.
        socket.send(
          JSON.stringify(makeErrorFrame([`connection is bound to ${session.deviceId}`])),
        );
        return;
      }

      broadcast(session.ingest(frame));
    });

    socket.on('close', () => {
      log.info?.(`device ${session?.deviceId ?? peer} disconnected`);
      // Mark rather than delete: a reconnect within the grace period resumes this
      // session, keeping filter and detector state continuous across the outage.
      if (session) session.disconnectedAt = Date.now();
    });
    socket.on('error', (err) => log.error?.(`device socket error: ${err.message}`));
  });

  // --- dashboard connections ----------------------------------------------------
  streamWss.on('connection', (socket) => {
    dashboards.add(socket);
    log.info?.(`dashboard connected (${dashboards.size} total)`);
    socket.send(
      JSON.stringify({
        v: PROTOCOL_VERSION,
        type: 'hello',
        fs: SAMPLE_RATE_HZ,
        devices: [...sessions.keys()],
      }),
    );
    // Dashboards send exactly one kind of message: an acknowledgement. Nothing here
    // can change what is measured or how alarms are evaluated - a viewer must not be
    // able to reconfigure a monitor - only silence an alarm that is already sounding.
    socket.on('message', (raw) => {
      let request;
      try {
        request = JSON.parse(raw.toString());
      } catch {
        socket.send(JSON.stringify(makeErrorFrame(['message was not valid JSON'])));
        return;
      }
      if (request?.type !== 'acknowledge') {
        socket.send(JSON.stringify(makeErrorFrame([`unsupported message type ${JSON.stringify(request?.type)}`])));
        return;
      }

      const target = sessions.get(request.deviceId);
      if (!target) {
        socket.send(JSON.stringify(makeErrorFrame([`no active session for ${request.deviceId}`])));
        return;
      }

      const now = Date.now();
      const by = typeof request.by === 'string' && request.by.length <= 64 ? request.by : null;
      const silenced = target.alarms.acknowledge(request.code, now, by);
      if (silenced && target.storeSessionId !== null) {
        // Who silenced what, and when. Without this the history can say an alarm
        // sounded for ten minutes but not whether anybody noticed.
        void store.acknowledgeAlarm(target.storeSessionId, request.code, now, by);
      }
      socket.send(JSON.stringify({
        v: PROTOCOL_VERSION,
        type: 'acknowledged',
        deviceId: request.deviceId,
        code: request.code,
        ok: silenced,
        // Refused when the alarm is not currently sounding: pre-silencing an alarm that
        // has not happened yet is the shortcut that makes acknowledgement dangerous.
        reason: silenced ? null : 'that alarm is not currently active',
      }));
    });

    socket.on('close', () => dashboards.delete(socket));
    socket.on('error', () => dashboards.delete(socket));
  });

  // --- upgrade routing ----------------------------------------------------------
  httpServer.on('upgrade', (request, socket, head) => {
    const { pathname } = new URL(request.url, `http://${request.headers.host ?? 'localhost'}`);
    const target = pathname === '/ingest' ? ingestWss : pathname === '/stream' ? streamWss : null;
    if (!target) {
      socket.destroy();
      return;
    }
    target.handleUpgrade(request, socket, head, (ws) => target.emit('connection', ws, request));
  });

  // --- silent-device watchdog ---------------------------------------------------
  const timer = setInterval(() => {
    const now = Date.now();
    for (const [deviceId, session] of sessions) {
      if (session.disconnectedAt && now - session.disconnectedAt > SESSION_GRACE_MS) {
        sessions.delete(deviceId);
        log.info?.(`session for ${deviceId} ended (disconnected > ${SESSION_GRACE_MS / 1000}s)`);
        if (session.storeSessionId !== null) {
          // Close every alarm episode still open, so a reaped session does not leave
          // alarms that look active forever in the history (the persistence twin of
          // D-20's reaping rule).
          for (const code of session.persistedAlarms.keys()) {
            void store.clearAlarm(session.storeSessionId, code, session.disconnectedAt);
          }
          session.persistedAlarms.clear();
          void store.endSession(session.storeSessionId, session.disconnectedAt, {
            framesReceived: session.framesReceived,
            framesDropped: session.framesDropped,
          });
        }
        continue;
      }
      const vitals = session.tick();
      if (vitals && vitals.alarms.length > 0) broadcast(vitals);
    }
  }, TICK_MS);
  timer.unref?.(); // never keep the process alive just for the watchdog

  return {
    sessions,
    dashboards,
    async listen() {
      await new Promise((resolve) => httpServer.listen(port, host, resolve));
      const address = httpServer.address();
      log.info?.(
        `VitalSense server listening on ws://${host}:${address.port} ` +
          `(/ingest for devices, /stream for dashboards)`,
      );
      return address.port;
    },
    async close() {
      clearInterval(timer);
      for (const c of dashboards) c.close();
      ingestWss.close();
      streamWss.close();
      await new Promise((resolve) => httpServer.close(resolve));
    },
  };
}

// Only start a server when run directly - importing this module in tests must not
// bind a port. Paths are compared resolved, because import.meta.url percent-encodes
// characters that are legal in a filesystem path.
if (process.argv[1]) {
  const { fileURLToPath } = await import('node:url');
  const { resolve } = await import('node:path');
  if (resolve(fileURLToPath(import.meta.url)) === resolve(process.argv[1])) {
    const { createStore } = await import('./storage/index.js');
    const store = await createStore({ log: console });
    const server = createVitalSenseServer({ store });
    await server.listen();
    for (const signal of ['SIGINT', 'SIGTERM']) {
      process.on(signal, async () => {
        await server.close();
        await store.close();
        process.exit(0);
      });
    }
  }
}
