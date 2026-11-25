// The record list: tabs, filters, search and a lazily paged, sortable table, all
// derived from the view spec. State lives in the URL so every view can be linked.

import { currentUser, renderWho } from "./auth.js";
import { getRecord, ident, loadJSON, loadSite, openDb, readMeta, siteUrl } from "./db.js";
import { applyLive, columnCell, el, enumEntry, readableTime, renderExplainer, renderPreview, slotTitle } from "./render.js";
import { decisions, forgetRecent, recentStatuses, signOff } from "./service.js";

const PAGE_SIZE = 50;

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

// The view first-page.json holds for each tab: the default sort, page 1, no search or
// filters. It shows without the database, which then opens in the background.
function isDefaultView(spec, state) {
  return (
    !isFiltered(state) &&
    state.page === 1 &&
    state.after == null &&
    state.before == null &&
    (state.sort ?? null) === (spec.default_sort?.column ?? null) &&
    (spec.default_sort ? state.dir === spec.default_sort.direction : true)
  );
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

// Long words (e.g. "phosphoribosylaminoimidazole") get break points, so a cell can
// wrap them instead of pushing the table wider than the page; shorter words never break.
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

// A search row (box and button), then a row of filters that apply as soon as one is
// picked; an active filter stands out, and "Clear all" resets search and filters.
function renderControls(container, spec, state, meta, go) {
  container.replaceChildren();
  const submit = el("button", { class: "button", type: "submit" }, "Search");
  const searchable = spec.columns.filter((c) => c.search);
  if (searchable.length) {
    const input = el("input", {
      type: "search",
      name: "q",
      value: state.q,
      placeholder: "Search " + searchable.map((c) => c.title.toLowerCase()).join(", "),
      "aria-label": "Search records",
    });
    // Emptying the box (its clear button, or deleting the text) undoes the search at once,
    // as "Clear all" does; a non-empty search still waits for Enter or the button.
    input.addEventListener("input", () => {
      if (!input.value.trim() && state.q) container.requestSubmit();
    });
    container.append(el("div", { class: "search-row" }, el("label", { class: "search-field" }, input), submit));
  }
  const filters = el("div", { class: "filter-row" });
  for (const column of spec.columns.filter((c) => c.filter)) {
    const values = { ...(meta.filter_values?.[state.tab ?? ""]?.[column.name] || {}) };
    // A chosen value this tab doesn't have (e.g. from a link) still shows, with 0 records.
    if (state.filters[column.name] && !(state.filters[column.name] in values)) values[state.filters[column.name]] = 0;
    const select = el(
      "select",
      { name: column.name, "data-filter": column.name, onchange: () => container.requestSubmit() },
      el("option", { value: "" }, "Any"),
      Object.entries(values)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([value, n]) => {
          const label = column.enum ? enumEntry(spec, column.enum, value)?.title || value : value;
          return el("option", { value, selected: state.filters[column.name] === value }, `${label} (${n.toLocaleString()})`);
        }),
    );
    const active = Boolean(state.filters[column.name]);
    filters.append(el("label", { class: active ? "control active" : "control" }, el("span", { class: "control-label" }, column.title), select));
  }
  if (!searchable.length) filters.append(submit);
  if (isFiltered(state)) {
    filters.append(
      el("button", { type: "button", class: "link clear", onclick: () => go({ ...state, q: "", filters: {}, page: 1 }) }, "Clear all"),
    );
  }
  if (filters.children.length) container.append(filters);
  container.onsubmit = (event) => {
    event.preventDefault();
    const data = new FormData(container);
    const chosen = {};
    for (const column of spec.columns.filter((c) => c.filter)) {
      const value = data.get(column.name);
      if (value) chosen[column.name] = value;
    }
    go({ ...state, q: (data.get("q") || "").trim(), filters: chosen, page: 1 });
  };
}

async function togglePreview(dbReady, spec, tr, button, href) {
  const key = tr.dataset.key;
  const open = tr.nextElementSibling?.matches(`tr.preview[data-key="${CSS.escape(key)}"]`);
  if (open) {
    tr.nextElementSibling.remove();
    button.setAttribute("aria-expanded", "false");
    return;
  }
  const cell = el("td", { colspan: spec.columns.length + (quick ? 2 : 1) }, el("p", { class: "muted" }, "Loading…"));
  const previewRow = el("tr", { class: "preview", "data-key": key, id: `preview-${key}` }, cell);
  tr.after(previewRow);
  button.setAttribute("aria-expanded", "true");
  // Only the expanded record is fetched, one indexed lookup.
  const record = await getRecord(await dbReady, key);
  cell.replaceChildren(record ? renderPreview(spec, record, href) : el("p", {}, "Record not found."));
}

// Quick sign-off: one-click buttons in each row for the outcomes the schema marks with
// browser_quick, for a logged-in user on a deployment with a sign-off service. Set by main.
let quick = null;

function quickField(spec) {
  return spec.form?.fields.find((f) => f.quick?.length) ?? null;
}

// The row's statement in words, from the title template and the row's columns.
function rowTitle(spec, row) {
  return (spec.title_template || "").replace(/\{([\w.]+)\}/g, (_, path) => {
    const value = row[path.split(".").join("__")];
    return value === undefined || value === null ? "" : String(value);
  });
}

function quickButtons(spec, row, href) {
  const field = quickField(spec);
  return el(
    "div",
    { class: "quick-actions" },
    field.quick.map((value) => {
      const option = field.options.find((o) => o.value === value);
      return el(
        "button",
        {
          type: "button",
          class: `quick-btn quick-${value}`,
          title: option?.description || null,
          onclick: (event) => {
            event.stopPropagation();
            openQuick(spec, event.target.closest("tr"), row, option);
          },
        },
        option?.title || value,
      );
    }),
    el("a", { href: `${href}#signoff`, class: "quick-more", "aria-label": "More sign-off options", title: "More sign-off options" }, "⋯"),
  );
}

// The confirm strip under a row: what is being signed off, the latest decision, the
// form's free-text fields, Confirm and Cancel. Sign-offs are permanent, so one click
// never records one.
function openQuick(spec, tr, row, option) {
  const { config, dbReady, table } = quick;
  const field = quickField(spec);
  const key = row.key;
  tr.parentElement.querySelector(`tr.quick[data-key="${CSS.escape(key)}"]`)?.remove();
  const latest = el("p", { class: "quick-latest muted" }, "Checking for earlier sign-offs…");
  const message = el("p", { class: "form-message", role: "status" });
  const texts = spec.form.fields
    .filter((f) => f.widget === "textarea")
    .map((f) => el("label", { class: "quick-field" }, el("span", {}, slotTitle(f)), el("textarea", { name: f.name, rows: 2 })));
  const confirm = el("button", { type: "button", class: "button small", disabled: true }, `Confirm`);
  const cancel = el("button", { type: "button", class: "link" }, "Cancel");
  const strip = el(
    "tr",
    { class: "quick", "data-key": key },
    el(
      "td",
      { colspan: spec.columns.length + 2 },
      el("p", { class: "quick-title" }, el("strong", {}, `${option.title}: `), rowTitle(spec, row)),
      latest,
      texts,
      el("div", { class: "quick-buttons" }, confirm, cancel, message),
    ),
  );
  tr.after(strip);
  let seen = null;
  const outcomeTitle = (value) => field.options.find((o) => o.value === value)?.title || value;
  async function loadLatest() {
    const result = await decisions(config, key);
    if (!result.ok) {
      latest.textContent = result.body.detail || `Could not check earlier sign-offs (HTTP ${result.status}).`;
      return false;
    }
    const last = result.body.decisions.at(-1);
    seen = last ? last.decision_id : null;
    if (!last) {
      latest.textContent = "No sign-offs yet.";
      return true;
    }
    const who = last.curator?.name || last.curator?.orcid || "a curator";
    latest.textContent = `Latest: ${outcomeTitle(last[field.name])} by ${who}, ${readableTime(last.decided_at)}.`;
    if (result.body.status) {
      applyLive(table, spec, { [key]: { status: result.body.status, curator: who, decided_at: last.decided_at } });
    }
    return true;
  }
  cancel.addEventListener("click", () => strip.remove());
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    message.className = "form-message";
    message.textContent = "Saving…";
    const user = currentUser();
    const record = user ? await getRecord(await dbReady, key) : null;
    if (!user || !record) {
      message.textContent = user ? "This record is not in the index." : "Your ORCID login has expired; log in again.";
      message.className = "form-message error";
      return;
    }
    const values = { [field.name]: option.value };
    for (const area of strip.querySelectorAll("textarea")) if (area.value.trim()) values[area.name] = area.value.trim();
    const result = await signOff(config, spec, user, key, { record, seenDecision: seen, values });
    if (result.ok) {
      const decision = result.body.decision;
      const who = decision.curator?.name || decision.curator?.orcid;
      applyLive(table, spec, { [key]: { status: decision[field.name], curator: who, decided_at: decision.decided_at } });
      forgetRecent();
      strip.remove();
      return;
    }
    message.textContent = result.body.detail || `Sign-off failed (HTTP ${result.status}).`;
    message.className = "form-message error";
    // Someone else decided meanwhile: show it; Confirm then records this on top.
    if (result.status === 409) await loadLatest();
    confirm.disabled = false;
  });
  loadLatest().then(() => {
    confirm.disabled = false;
  });
}

function renderTable(container, spec, state, rows, go, db) {
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
    quick ? el("th", { scope: "col", class: "actions-col" }, "Sign off") : null,
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
        const content = columnCell(spec, column, row[column.name]);
        // The first text column links to the record, so rows work without JS clicks too.
        const linkHere = i === spec.columns.findIndex((c) => c.kind === "string" && !c.multivalued && c.format !== "curie");
        return el(
          "td",
          {
            "data-column": column.name,
            "data-label": column.title,
            class: [`k-${column.kind}`, column.format ? `f-${column.format}` : ""].join(" ").trim(),
          },
          linkHere ? el("a", { href, class: "record-link" }, content) : content,
        );
      }),
      quick ? el("td", { class: "actions-cell", "data-label": "Sign off" }, quickButtons(spec, row, href)) : null,
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
  if (pages > 1) container.append(el("span", { class: "muted" }, `Page ${state.page.toLocaleString()} of ${pages.toLocaleString()}`));
  if (state.page < pages) container.append(el("button", { type: "button", class: "link", onclick: next }, "Next →"));
}

async function main() {
  const status = document.getElementById("status");
  try {
    // The first page is fetched alongside the spec and config; it is used if the build made it.
    const firstPageFetch = loadJSON("first-page.json").catch(() => null);
    const { spec, config } = await loadSite();
    renderWho(document.getElementById("who"), config, el);
    // Open the database in the background; the default view does not need it.
    const dbReady = openDb(config);
    dbReady.catch(() => {}); // a failure surfaces where a view awaits it
    const firstPage = config.first_page === "first-page.json" ? await firstPageFetch : null;
    const meta = firstPage ? firstPage.meta : await readMeta(await dbReady);
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
    if (config.service && quickField(spec) && currentUser()) quick = { config, dbReady, table };
    const count = document.getElementById("count");
    const notice = document.getElementById("notice");
    const pager = document.getElementById("pager");

    async function show(state) {
      const live = recentStatuses(config);
      renderTabs(tabs, spec, state, meta, go);
      renderControls(controls, spec, state, meta, go);
      document.body.dataset.loading = "true";
      const instant = firstPage && isDefaultView(spec, state) ? firstPage.pages[state.tab ?? ""] : null;
      const db = instant ? null : await dbReady;
      const total = await countRows(db, spec, state, meta);
      const shown = effectiveState(spec, state, meta, total);
      const rows = instant ?? (await fetchPage(db, spec, shown.state, meta));
      const first = total ? (state.page - 1) * PAGE_SIZE + 1 : 0;
      count.textContent = `${first.toLocaleString()}–${((state.page - 1) * PAGE_SIZE + rows.length).toLocaleString()} of ${total.toLocaleString()} records`;
      notice.textContent = shown.notice || "";
      notice.hidden = !shown.notice;
      renderTable(table, spec, shown.state, rows, go, dbReady);
      renderPager(pager, shown.state, total, rows, go);
      document.body.dataset.loading = "false";
      live.then((statuses) => applyLive(table, spec, statuses));
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
