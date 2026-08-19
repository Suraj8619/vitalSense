/**
 * Pick a store from the environment.
 *
 * No `DATABASE_URL` means no database, and that is a supported configuration rather than
 * a broken one: the whole system still runs, the dashboard still works, and only history
 * is absent. Requiring a database to watch a patient would be the wrong dependency for a
 * bedside device (D-43).
 */
import { MemoryStore, NullStore } from './store.js';
import { PostgresStore } from './postgresStore.js';

export { MemoryStore, NullStore, PostgresStore };

export async function createStore({ connectionString = process.env.DATABASE_URL, log = console } = {}) {
  if (!connectionString) {
    log.info?.('[db] no DATABASE_URL - running without history');
    return new NullStore();
  }
  const store = new PostgresStore({ connectionString, log });
  try {
    await store.init();
    log.info?.('[db] persistence enabled');
    return store;
  } catch (err) {
    // The operator asked for persistence and cannot have it. Say so loudly, but keep
    // monitoring: a device that refuses to show a patient because a database is down
    // has confused its priorities.
    log.error?.(`[db] could not initialise persistence (${err.message}) - continuing without history`);
    await store.close();
    return new NullStore();
  }
}
