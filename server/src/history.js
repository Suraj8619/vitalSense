/**
 * Session history: listing, detail, and CSV export.
 *
 * Read-only over the storage contract, so it works identically against Postgres and the
 * in-memory implementation - which is what lets these routes be tested without a
 * database (D-43).
 *
 * Everything here answers one question a clinician actually asks after a shift: what
 * happened, and when. Trends come from the 1 Hz vitals; the alarm timeline comes from
 * episodes with their raise, acknowledge and clear times, so "how long was this alarm
 * sounding before anyone silenced it" is answerable rather than inferred.
 */

const JSON_HEADERS = { 'content-type': 'application/json; charset=utf-8' };

/** Columns in the CSV, in order. Kept explicit so the export cannot silently change. */
const CSV_COLUMNS = [
  ['timestamp_iso', (r) => new Date(r.tServer).toISOString()],
  ['epoch_ms', (r) => r.tServer],
  ['sample_index', (r) => r.sampleIndex],
  ['hr_bpm', (r) => r.hr],
  ['hr_class', (r) => r.hrClass],
  ['spo2_pct', (r) => r.spo2],
  ['spo2_source', (r) => r.spo2Source],
  ['spo2_quality', (r) => r.spo2Quality],
  ['pulse_bpm', (r) => r.pulseBpm],
  ['perfusion_pct', (r) => r.perfusionPct],
  ['rate_agreement', (r) => r.rateAgreement],
  ['temp_c', (r) => r.tempC],
  ['signal_quality', (r) => r.signalQuality],
];

/**
 * Render one CSV field.
 *
 * A missing measurement is an **empty field**, never 0 and never "null" - a spreadsheet
 * reading 0 bpm where the monitor meant "I don't know" is the same failure the display
 * avoids by showing `--` (D-23), and it survives export into whatever anyone plots next.
 */
function csvField(value) {
  if (value === null || value === undefined) return '';
  const text = String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function toCsv(rows) {
  const header = CSV_COLUMNS.map(([name]) => name).join(',');
  const body = rows.map((row) => CSV_COLUMNS.map(([, get]) => csvField(get(row))).join(','));
  // Trailing newline: a file that does not end in one makes `wc -l` and many parsers
  // disagree about how many records it holds.
  return [header, ...body].join('\n') + '\n';
}

const send = (res, status, payload) => {
  res.writeHead(status, JSON_HEADERS);
  res.end(JSON.stringify(payload));
};

/**
 * Handle a history request. Returns true when the path was ours.
 *
 * @param {import('node:http').IncomingMessage} req
 * @param {import('node:http').ServerResponse} res
 * @param {URL} url
 * @param {object} store
 */
export async function handleHistoryRequest(req, res, url, store) {
  const { pathname } = url;
  if (!pathname.startsWith('/api/')) return false;

  if (!store.enabled) {
    // Distinguish "no history configured" from "session not found". The first is an
    // operator's choice and the second is a mistake; conflating them sends someone
    // hunting for a lost recording that was never being recorded.
    send(res, 501, { error: 'history is not enabled', detail: 'start the server with DATABASE_URL set' });
    return true;
  }

  if (pathname === '/api/sessions' && req.method === 'GET') {
    const limit = Math.min(Number(url.searchParams.get('limit') ?? 50) || 50, 500);
    send(res, 200, { sessions: await store.listSessions({ limit }) });
    return true;
  }

  const match = pathname.match(/^\/api\/sessions\/(\d+)(\/vitals\.csv|\/vitals|\/alarms)?$/);
  if (!match) {
    send(res, 404, { error: 'unknown endpoint' });
    return true;
  }

  const id = Number(match[1]);
  const session = await store.getSession(id);
  if (!session) {
    send(res, 404, { error: `no session ${id}` });
    return true;
  }

  const range = {
    from: url.searchParams.has('from') ? Number(url.searchParams.get('from')) : undefined,
    to: url.searchParams.has('to') ? Number(url.searchParams.get('to')) : undefined,
  };

  switch (match[2]) {
    case '/vitals.csv': {
      const rows = await store.getVitals(id, range);
      const stamp = new Date(session.startedAt).toISOString().replace(/[:.]/g, '-');
      res.writeHead(200, {
        'content-type': 'text/csv; charset=utf-8',
        // The filename carries the device and the session start, so a folder of exports
        // stays interpretable without opening them.
        'content-disposition': `attachment; filename="vitalsense-${session.deviceId}-${stamp}.csv"`,
      });
      res.end(toCsv(rows));
      return true;
    }
    case '/vitals':
      send(res, 200, { session, vitals: await store.getVitals(id, range) });
      return true;
    case '/alarms':
      send(res, 200, { session, alarms: await store.getAlarmEvents(id) });
      return true;
    default:
      send(res, 200, { session, alarms: await store.getAlarmEvents(id) });
      return true;
  }
}
