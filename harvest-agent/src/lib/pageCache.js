/* In-memory cache of page data, kept while the app is open (not across a reload).
 *
 * List pages unmount when the user opens another page. On Back they would start
 * empty and refetch, and the scroll position could only be restored once the rows
 * arrived. Seeding the page from this cache renders the previous rows at once, so
 * ScrollRestoration lands on the saved position immediately; the page still
 * refetches quietly to pick up any changes. */

const MAX_ENTRIES = 60;
const store = new Map();

/** The cached value for `key`, or undefined. */
export function getCached(key) {
  return store.get(key);
}

/** Cache `value` under `key` as the most recent entry (oldest evicted past MAX_ENTRIES). */
export function setCached(key, value) {
  store.delete(key);
  store.set(key, value);
  while (store.size > MAX_ENTRIES) store.delete(store.keys().next().value);
}

const refs = new Map();

/** A named ref-like holder ({current}) that outlives page unmounts — for pages that
 * keep a whole view object (e.g. My Workspace's rows + stats) rather than keyed data. */
export function cacheRef(name) {
  if (!refs.has(name)) refs.set(name, { current: null });
  return refs.get(name);
}

/** Drop every cached entry (on sign-out, so the next user never sees them). */
export function clearPageCache() {
  store.clear();
  refs.forEach((ref) => { ref.current = null; });
}
