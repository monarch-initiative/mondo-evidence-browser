// Site config, view spec and the SQLite index, read lazily over HTTP range requests.
//
// The index is opened with sql.js-httpvfs (vendored by the build, loaded as a
// classic script that defines window.createDbWorker). Only the pages a query
// touches are fetched, so a 300K-record index never comes down whole.

const siteRoot = new URL(".", document.baseURI);

export function siteUrl(path) {
  return new URL(path, siteRoot).href;
}

export async function loadJSON(path) {
  const response = await fetch(siteUrl(path), { cache: "no-cache" });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

export async function loadSite() {
  const [spec, config] = await Promise.all([loadJSON("view-spec.json"), loadJSON("config.json")]);
  if (spec.schema.version !== config.schema_version) {
    throw new Error(
      `view-spec.json (schema ${spec.schema.version}) and config.json ` +
        `(schema ${config.schema_version}) come from different builds`,
    );
  }
  return { spec, config };
}

export async function openDb(config) {
  const db = { ...config.db };
  if (db.url) db.url = siteUrl(db.url);
  if (db.urlPrefix) db.urlPrefix = siteUrl(db.urlPrefix);
  const worker = await window.createDbWorker(
    [{ from: "inline", config: db }],
    siteUrl("vendor/sql.js-httpvfs/sqlite.worker.js"),
    siteUrl("vendor/sql.js-httpvfs/sql-wasm.wasm"),
  );
  const handle = {
    query: (sql, params = []) => worker.db.query(sql, params),
    bytesRead: async () => worker.worker.bytesRead,
  };
  // Exposed for the browser smoke tests, which check that reads stay lazy.
  window.__meb = handle;
  return handle;
}

export async function readMeta(db) {
  const rows = await db.query("SELECT key, value FROM meta");
  return Object.fromEntries(rows.map((r) => [r.key, JSON.parse(r.value)]));
}

// Quote an identifier that comes from the schema.
export function ident(name) {
  return '"' + String(name).replaceAll('"', '""') + '"';
}

// Records are stored as zlib-compressed JSON.
export async function inflateJSON(bytes) {
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate"));
  return JSON.parse(await new Response(stream).text());
}

export async function getRecord(db, key) {
  const rows = await db.query(
    "SELECT j.json AS json FROM records r JOIN record_json j ON j.rowid = r.rowid WHERE r.key = ?",
    [key],
  );
  return rows.length ? inflateJSON(rows[0].json) : null;
}
