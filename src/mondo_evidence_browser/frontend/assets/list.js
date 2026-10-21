// The record list: tabs, filters, search and a lazily paged, sortable table, all
// derived from the view spec. State lives in the URL so every view can be linked.

import { renderWho } from "./auth.js";
import { getRecord, ident, loadSite, openDb, readMeta, siteUrl } from "./db.js";
import { el, enumEntry, renderExplainer, renderList, renderPreview, renderScalar } from "./render.js";

const PAGE_SIZE = 50;
const LIVE_TTL_MS = 30_000;

// Sign-offs made after this build, from the sign-off service: {record id: {status, …}}.
// The table shows their status at once, so curators working in parallel see each
// other's decisions without waiting for the next build (filter counts still do).
let liveCache = null;
async function liveStatuses(config) {
  if (!config.service) return {};
  if (liveCache && Date.now() - liveCache.at < LIVE_TTL_MS) return liveCache.statuses;
  const since = config.decisions?.as_of;
  const query = since ? `?since=${encodeURIComponent(since)}` : "";
  let statuses = liveCache?.statuses ?? {};
  try {
    // The service is another system; the table still works without it.
    const response = await fetch(`${config.service.url}/api/decisions/recent${query}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    statuses = (await response.json()).statuses;
  } catch (error) {
    console.warn(`Recent sign-offs are unavailable: ${error.message}`);
  }
  liveCache = { at: Date.now(), statuses };
  return statuses;
}

// The first tab that has records; tabs without records are hidden unless opened.
function defaultTab(spec, meta) {
  if (!spec.tabs) return null;
  const values = spec.tabs.values.map((t) => t.value);
  return values.find((v) => (meta.tab_counts?.[v] ?? 0) > 0) ?? values[0];
}

function readState(spec, meta) {
  const params = new URLSearchParams(location.search);
  const tabs = spec.tabs ? spec.tabs.values.map((t) => t.value) : [];
  const sortable = spec.columns.filter((c) => c.sortable).map((c) => c.name);
  const filters = {};
  for (const column of spec.columns.filter((c) => c.filter)) {
    const value = params.get(column.name);
    if (value) filters[column.name] = value;
  }
  const sort = sortable.includes(params.get("sort")) ? params.get("sort") : spec.default_sort?.column;
  const defaultDir = sort === spec.default_sort?.column ? spec.default_sort.direction : "asc";
  return {
    tab: tabs.includes(params.get("tab")) ? params.get("tab") : defaultTab(spec, meta),
    sort,
    dir: ["asc", "desc"].includes(params.get("dir")) ? params.get("dir") : defaultDir,
    q: params.get("q") || "",
    filters,
    page: Math.max(1, parseInt(params.get("page") || "1", 10) || 1),
    after: params.has("after") ? parseInt(params.get("after"), 10) : null,
    before: params.has("before") ? parseInt(params.get("before"), 10) : null,
  };
}

function writeState(state, spec, meta, push = true) {
  const params = new URLSearchParams();
  if (state.tab && state.tab !== defaultTab(spec, meta)) params.set("tab", state.tab);
  if (state.sort && state.sort !== spec.default_sort?.column) params.set("sort", state.sort);
  if (state.sort && (state.sort !== spec.default_sort?.column || state.dir !== spec.default_sort.direction)) {
    params.set("dir", state.dir);
  }
  if (state.q) params.set("q", state.q);
  for (const [name, value] of Object.entries(state.filters)) params.set(name, value);
  if (state.page > 1) params.set("page", state.page);
  if (state.after !== null && state.after !== undefined) params.set("after", state.after);
  if (state.before !== null && state.before !== undefined) params.set("before", state.before);
  const url = `${location.pathname}${params.size ? "?" + params : ""}`;
  history[push ? "pushState" : "replaceState"](null, "", url);
}

// Every word must match, as a prefix (FTS5 syntax, words quoted).
function ftsQuery(text) {
  const words = text.match(/[\p{L}\p{N}_]+/gu) || [];
  return words.length ? words.map((w) => `"${w}"*`).join(" ") : null;
}

function whereClause(spec, state) {
  const clauses = [];
  const params = [];
  if (state.tab !== null) {
    clauses.push("tab = ?");
    params.push(state.tab);
  }
  for (const [name, value] of Object.entries(state.filters)) {
    clauses.push(`${ident(name)} = ?`);
    params.push(value);
  }
  const match = ftsQuery(state.q);
  if (match && spec.columns.some((c) => c.search)) {
    clauses.push("rowid IN (SELECT rowid FROM search WHERE search MATCH ?)");
    params.push(match);
  }
  return { sql: clauses.length ? " WHERE " + clauses.join(" AND ") : "", params };
}

// The search-index expression for the tab, the filters and the search words. The index
// holds an exact token per tab and filter value (see index.py), so this needs no rows.
// Returns null when nothing can match (a filter value absent from the tab).
function matchExpression(state, meta) {
  const tokens = meta.fts_tokens;
  const parts = [];
  if (state.tab !== null) {
    const token = tokens.tab.tokens[state.tab];
    if (!token) return null;
    parts.push(`${tokens.tab.column} : ${token}`);
  }
  for (const [name, value] of Object.entries(state.filters)) {
    const filter = tokens.filters[name];
    const token = filter?.tokens[value];
    if (!token) return null;
    parts.push(`${filter.column} : ${token}`);
  }
  const words = ftsQuery(state.q);
  if (words) parts.push(`(${words})`);
  return parts.join(" AND ");
}

// Counts never read rows: per-combination counts were stored at build time, and a
// search is counted by the search index alone.
async function countRows(db, spec, state, meta) {
  const tab = state.tab ?? "";
  if (!state.q) {
    const columns = meta.filter_columns || [];
    const wanted = columns.map((name) => state.filters[name] ?? null);
    return (meta.filter_combos?.[tab] || [])
      .filter(([values]) => wanted.every((w, i) => w === null || values[i] === w))
      .reduce((total, [, n]) => total + n, 0);
  }
  const match = matchExpression(state, meta);
  if (match === null) return 0;
  const [row] = await db.query("SELECT count(*) AS n FROM search WHERE search MATCH ?", [match]);
  return row.n;
}

function isFiltered(state) {
  return Boolean(state.q) || Object.keys(state.filters).length > 0;
}

// Share of the tab the filters keep, from the counts stored at build time.
function filterShare(state, meta) {
  const tab = state.tab ?? "";
  const total = meta.tab_counts?.[tab] ?? 0;
  if (!total) return 0;
  const counts = Object.entries(state.filters).map(
    ([name, value]) => meta.filter_values?.[tab]?.[name]?.[value] ?? 0,
  );
  return Math.min(...counts) / total;
}

// For a common filter value, walk the sort rank's index and stop after a page of
// matches; for a rare one, let SQLite take the filter's index and sort the few rows.
const COMMON_FILTER_SHARE = 0.05;

function rankSource(state, meta, rankIndex) {
  const common = !state.q && Object.keys(state.filters).length && filterShare(state, meta) >= COMMON_FILTER_SHARE;
  return common && rankIndex ? `records INDEXED BY ${ident(rankIndex)}` : "records";
}

// Sorting a search by another column means reading each match's row, so it is only
// offered up to this many matches; larger searches stay in the default (stored) order.
const SORT_SEARCH_LIMIT = 500;

function isClusteredSort(state, meta) {
  return Boolean(meta.clustered) && state.sort === meta.clustered.column;
}

// The view actually shown: a search too broad to sort by another column falls back to
// the default order, with a note saying so.
function effectiveState(spec, state, meta, total) {
  if (state.q && meta.clustered && !isClusteredSort(state, meta) && total > SORT_SEARCH_LIMIT) {
    const column = spec.columns.find((c) => c.name === meta.clustered.column);
    return {
      state: { ...state, sort: meta.clustered.column, dir: meta.clustered.direction, after: null, before: null },
      notice:
        `Sorted by ${column?.title ?? "the default order"}: a search with more than ` +
        `${SORT_SEARCH_LIMIT.toLocaleString()} matches can't be sorted by another column. ` +
        "Narrow the search to sort it.",
    };
  }
  return { state, notice: null };
}

// Rows are stored in the default order, so in that order a filtered or searched page is
// a slice of the search index's matching row ids, then one read of just those rows.
async function fetchByRowOrder(db, spec, state, meta) {
  const match = matchExpression(state, meta);
  if (match === null) return [];
  const forward = state.dir === meta.clustered.direction;
  const order = forward ? "ASC" : "DESC";
  const ids = await db.query(
    `SELECT rowid AS id FROM search WHERE search MATCH ? ORDER BY rowid ${order} LIMIT ? OFFSET ?`,
    [match, PAGE_SIZE, (state.page - 1) * PAGE_SIZE],
  );
  if (!ids.length) return [];
  const columns = ["key", ...spec.columns.map((c) => c.name)].map(ident).join(", ");
  const marks = ids.map(() => "?").join(", ");
  return db.query(
    `SELECT ${columns} FROM records WHERE rowid IN (${marks}) ORDER BY rowid ${order}`,
    ids.map((r) => r.id),
  );
}

// Pages are read through the sort column's rank (its position within the tab, see
// index.py), so no page ever skips rows to get where it is:
// - unfiltered, any page N is a range of ranks, however deep;
// - filtered, Next / Previous continue from the last / first rank shown;
// - filtered or searched in the default order, pages come from the search index.
async function fetchPage(db, spec, state, meta) {
  if (isFiltered(state) && isClusteredSort(state, meta) && meta.fts_tokens) {
    return fetchByRowOrder(db, spec, state, meta);
  }
  const where = whereClause(spec, state);
  const columns = ["key", ...spec.columns.map((c) => c.name)].map(ident).join(", ");
  const rank = state.sort ? meta.rank_columns?.[state.sort] : null;
  if (!rank) {
    return db.query(
      `SELECT ${columns} FROM records${where.sql} ORDER BY rowid LIMIT ? OFFSET ?`,
      [...where.params, PAGE_SIZE, (state.page - 1) * PAGE_SIZE],
    );
  }
  const desc = state.dir === "desc";
  const r = ident(rank);
  const and = (clause) => (where.sql ? `${where.sql} AND ${clause}` : ` WHERE ${clause}`);
  const source = rankSource(state, meta, meta.rank_indexes?.[state.sort]);
  const select = `SELECT ${columns}, ${r} AS __rank FROM ${source}`;
  if (state.after !== null && state.after !== undefined) {
    return db.query(
      `${select}${and(`${r} ${desc ? "<" : ">"} ?`)} ORDER BY ${r} ${desc ? "DESC" : "ASC"} LIMIT ?`,
      [...where.params, state.after, PAGE_SIZE],
    );
  }
  if (state.before !== null && state.before !== undefined) {
    const rows = await db.query(
      `${select}${and(`${r} ${desc ? ">" : "<"} ?`)} ORDER BY ${r} ${desc ? "ASC" : "DESC"} LIMIT ?`,
      [...where.params, state.before, PAGE_SIZE],
    );
    return rows.reverse();
  }
  if (!isFiltered(state)) {
    const total = meta.tab_counts?.[state.tab ?? ""] ?? 0;
    const start = desc ? total - state.page * PAGE_SIZE : (state.page - 1) * PAGE_SIZE;
    return db.query(
      `${select}${and(`${r} > ? AND ${r} <= ?`)} ORDER BY ${r} ${desc ? "DESC" : "ASC"}`,
      [...where.params, start, start + PAGE_SIZE],
    );
  }
  // A filtered page opened by its number alone (a pasted link) skips to it once.
  return db.query(
    `${select}${where.sql} ORDER BY ${r} ${desc ? "DESC" : "ASC"} LIMIT ? OFFSET ?`,
    [...where.params, PAGE_SIZE, (state.page - 1) * PAGE_SIZE],
  );
}

function cell(spec, column, value) {
  if (column.multivalued) {
    const values = value ? JSON.parse(value) : [];
    return renderList(spec, column.kind, values, { format: column.format, enumName: column.enum });
  }
  if (column.kind === "boolean" && value !== null) value = Boolean(value);
  return renderScalar(spec, column.kind, value, { format: column.format, enumName: column.enum });
}

function renderTabs(container, spec, state, meta, go) {
  container.replaceChildren();
  if (!spec.tabs) return;
  for (const tab of spec.tabs.values) {
    const count = meta.tab_counts?.[tab.value] ?? 0;
    if (count === 0 && tab.value !== state.tab) continue;
    container.append(
      el(
        "button",
        {
          type: "button",
          role: "tab",
          class: "tab",
          "aria-selected": tab.value === state.tab ? "true" : "false",
          title: tab.description || null,
          "data-tab": tab.value,
          onclick: () => go({ ...state, tab: tab.value, page: 1, filters: {} }),
        },
        tab.title,
        el("span", { class: "tab-count" }, count.toLocaleString()),
      ),
    );
  }
}

function renderControls(container, spec, state, meta, go) {
  container.replaceChildren();
  const searchable = spec.columns.filter((c) => c.search);
  if (searchable.length) {
    const input = el("input", {
      type: "search",
      name: "q",
      value: state.q,
      placeholder: "Search " + searchable.map((c) => c.title.toLowerCase()).join(", "),
      "aria-label": "Search records",
    });
    container.append(el("label", { class: "control grow" }, "Search", input));
  }
  for (const column of spec.columns.filter((c) => c.filter)) {
    const values = meta.filter_values?.[state.tab ?? ""]?.[column.name] || {};
    const select = el(
      "select",
      { name: column.name, "data-filter": column.name },
      el("option", { value: "" }, "any"),
      Object.entries(values)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([value, n]) => {
          const label = column.enum ? enumEntry(spec, column.enum, value)?.title || value : value;
          return el("option", { value, selected: state.filters[column.name] === value }, `${label} (${n.toLocaleString()})`);
        }),
    );
    container.append(el("label", { class: "control" }, column.title, select));
  }
  container.append(el("button", { class: "button", type: "submit" }, "Apply"));
  container.onsubmit = (event) => {
    event.preventDefault();
    const data = new FormData(container);
    const filters = {};
    for (const column of spec.columns.filter((c) => c.filter)) {
      const value = data.get(column.name);
      if (value) filters[column.name] = value;
    }
    go({ ...state, q: (data.get("q") || "").trim(), filters, page: 1 });
  };
}

async function togglePreview(db, spec, tr, button, href) {
  const key = tr.dataset.key;
  const open = tr.nextElementSibling?.matches(`tr.preview[data-key="${CSS.escape(key)}"]`);
  if (open) {
    tr.nextElementSibling.remove();
    button.setAttribute("aria-expanded", "false");
    return;
  }
  const cell = el("td", { colspan: spec.columns.length + 1 }, el("p", { class: "muted" }, "Loading…"));
  const previewRow = el("tr", { class: "preview", "data-key": key, id: `preview-${key}` }, cell);
  tr.after(previewRow);
  button.setAttribute("aria-expanded", "true");
  // Only the expanded record is fetched, one indexed lookup.
  const record = await getRecord(db, key);
  cell.replaceChildren(record ? renderPreview(spec, record, href) : el("p", {}, "Record not found."));
}

function renderTable(container, spec, state, rows, go, db, live = {}) {
  const head = el(
    "tr",
    {},
    el("th", { class: "expand-col" }, el("span", { class: "visually-hidden" }, "Details")),
    spec.columns.map((column) => {
      const sorted = state.sort === column.name;
      const ariaSort = sorted ? (state.dir === "desc" ? "descending" : "ascending") : column.sortable ? "none" : null;
      const label = column.sortable
        ? el(
            "button",
            {
              type: "button",
              class: "sort",
              onclick: () => {
                const dir = sorted ? (state.dir === "desc" ? "asc" : "desc") : column.name === spec.default_sort?.column ? spec.default_sort.direction : "asc";
                go({ ...state, sort: column.name, dir, page: 1 });
              },
            },
            column.title,
            el("span", { class: "sort-mark", "aria-hidden": "true" }, sorted ? (state.dir === "desc" ? " ▾" : " ▴") : ""),
          )
        : column.title;
      return el("th", { scope: "col", "aria-sort": ariaSort, "data-column": column.name, title: column.description || null }, label);
    }),
  );
  const body = rows.map((row) => {
    const href = siteUrl(`record.html?id=${encodeURIComponent(row.key)}`);
    const toggle = el(
      "button",
      {
        type: "button",
        class: "expand",
        "aria-expanded": "false",
        "aria-controls": `preview-${row.key}`,
        "aria-label": "Show details",
        title: "Show details",
      },
      el("span", { class: "chevron", "aria-hidden": "true" }, "›"),
    );
    const tr = el(
      "tr",
      { class: "row-link", "data-key": row.key },
      el("td", { class: "expand-cell" }, toggle),
      spec.columns.map((column, i) => {
        const recent = column.name === spec.status ? live[row.key] : null;
        const content = recent
          ? el("span", {}, cell(spec, column, recent.status), el("span", { class: "live-mark" }, " · new"))
          : cell(spec, column, row[column.name]);
        // The first text column links to the record, so rows work without JS clicks too.
        const linkHere = i === spec.columns.findIndex((c) => c.kind === "string" && !c.multivalued && c.format !== "curie");
        return el(
          "td",
          {
            "data-column": column.name,
            "data-label": column.title,
            class: [`k-${column.kind}`, column.format ? `f-${column.format}` : ""].join(" ").trim(),
            "data-live": recent ? "true" : null,
            title: recent ? `Signed off by ${recent.curator} at ${recent.decided_at}, after this build` : null,
          },
          linkHere ? el("a", { href, class: "record-link" }, content) : content,
        );
      }),
    );
    toggle.addEventListener("click", (event) => {
      event.stopPropagation();
      togglePreview(db, spec, tr, toggle, href);
    });
    tr.addEventListener("click", (event) => {
      if (!event.target.closest("a, button")) location.assign(href);
    });
    return tr;
  });
  container.replaceChildren(
    rows.length
      ? el("table", { class: "records" }, el("thead", {}, head), el("tbody", {}, body))
      : el("p", { class: "empty" }, "No records match."),
  );
}

function renderPager(container, state, total, rows, go) {
  container.replaceChildren();
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  // Filtered views continue from the page shown; unfiltered ones go by page number.
  const cursors = isFiltered(state) && rows.length && rows[0].__rank !== undefined;
  const previous = () =>
    go({ ...state, page: state.page - 1, before: cursors ? rows[0].__rank : null, after: null });
  const next = () =>
    go({ ...state, page: state.page + 1, after: cursors ? rows.at(-1).__rank : null, before: null });
  if (state.page > 1) container.append(el("button", { type: "button", class: "link", onclick: previous }, "← Previous"));
  if (state.page < pages) container.append(el("button", { type: "button", class: "link", onclick: next }, "Next →"));
}

async function main() {
  const status = document.getElementById("status");
  try {
    const { spec, config } = await loadSite();
    renderWho(document.getElementById("who"), config, el);
    const db = await openDb(config);
    const meta = await readMeta(db);
    document.getElementById("foot").textContent =
      `${meta.record_count.toLocaleString()} records · schema ${spec.schema.name} ${spec.schema.version} · ` +
      `Mondo ${meta.mondo_release} · scores ${meta.rule_id} ${meta.rule_version} · built ${meta.built_at}`;

    if (spec.explain) {
      const uncalibrated = String(meta.rule_version).includes("uncalibrated")
        ? " All strengths, priors and the scaling are placeholders until calibration (D-063)."
        : "";
      const note = `Scores in this build: ${meta.rule_id} ${meta.rule_version}, levels as of ${meta.as_of}.${uncalibrated}`;
      document.getElementById("explainer").replaceChildren(renderExplainer(spec, spec.explain, note) || "");
    }

    const tabs = document.getElementById("tabs");
    const controls = document.getElementById("controls");
    const table = document.getElementById("table");
    const count = document.getElementById("count");
    const notice = document.getElementById("notice");
    const pager = document.getElementById("pager");

    async function show(state) {
      const live = liveStatuses(config);
      renderTabs(tabs, spec, state, meta, go);
      renderControls(controls, spec, state, meta, go);
      document.body.dataset.loading = "true";
      const total = await countRows(db, spec, state, meta);
      const shown = effectiveState(spec, state, meta, total);
      const rows = await fetchPage(db, spec, shown.state, meta);
      const first = total ? (state.page - 1) * PAGE_SIZE + 1 : 0;
      count.textContent = `${first.toLocaleString()}–${((state.page - 1) * PAGE_SIZE + rows.length).toLocaleString()} of ${total.toLocaleString()} records`;
      notice.textContent = shown.notice || "";
      notice.hidden = !shown.notice;
      renderTable(table, spec, shown.state, rows, go, db, await live);
      renderPager(pager, shown.state, total, rows, go);
      document.body.dataset.loading = "false";
    }

    async function go(state) {
      // A new view (tab, sort, filter, search) starts afresh from its first page.
      if (state.page === 1) state = { ...state, after: null, before: null };
      writeState(state, spec, meta);
      await show(state);
    }

    window.addEventListener("popstate", () => show(readState(spec, meta)));
    // Back from a record or another tab: pick up sign-offs made meanwhile.
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && config.service) show(readState(spec, meta));
    });
    await show(readState(spec, meta));
    status.remove();
  } catch (error) {
    status.textContent = `Could not load the evidence index: ${error.message}`;
    status.classList.add("error");
    throw error;
  }
}

main();
