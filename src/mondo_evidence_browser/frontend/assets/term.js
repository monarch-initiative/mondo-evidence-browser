// A Mondo term's page: every evidence record about one term (the slot with
// browser_role: term), grouped by tab, at a stable URL (term.html?id=MONDO:…) that
// Mondo or OLS can link to. Rows come from the index's columns only, through the
// term index, so a term with thousands of records still reads a few pages.

import { renderWho } from "./auth.js";
import { ident, loadSite, openDb, readMeta, siteUrl } from "./db.js";
import { applyLive, columnCell, curieUrl, el } from "./render.js";
import { recentStatuses } from "./service.js";

const LIMIT = 200; // rows per tab; the list's search shows the rest

function samePath(a, b) {
  return Boolean(a && b) && a.length === b.length && a.every((step, i) => step === b[i]);
}

function table(spec, columns, rows) {
  const linkAt = columns.findIndex((c) => c.kind === "string" && !c.multivalued && c.format !== "curie");
  const head = el(
    "tr",
    {},
    columns.map((c) => el("th", { scope: "col", "data-column": c.name, title: c.description || null }, c.title)),
  );
  const body = rows.map((row) => {
    const href = `record.html?id=${encodeURIComponent(row.key)}`;
    const tr = el(
      "tr",
      { class: "row-link", "data-key": row.key },
      columns.map((c, i) => {
        const content = columnCell(spec, c, row[c.name]);
        return el(
          "td",
          {
            "data-column": c.name,
            "data-label": c.title,
            class: [`k-${c.kind}`, c.format ? `f-${c.format}` : ""].join(" ").trim(),
          },
          i === linkAt ? el("a", { href, class: "record-link" }, content) : content,
        );
      }),
    );
    tr.addEventListener("click", (event) => {
      if (!event.target.closest("a, button")) location.assign(siteUrl(href));
    });
    return tr;
  });
  return el("table", { class: "records" }, el("thead", {}, head), el("tbody", {}, body));
}

async function main() {
  const status = document.getElementById("status");
  const article = document.getElementById("term");
  try {
    const { spec, config } = await loadSite();
    renderWho(document.getElementById("who"), config, el);
    const id = (new URLSearchParams(location.search).get("id") || "").trim();
    const db = await openDb(config);
    const meta = await readMeta(db);
    const termColumn = spec.columns.find((c) => samePath(c.path, spec.roles?.term));
    if (!termColumn || meta.term_column !== termColumn.name) {
      throw new Error("this index has no term lookup (a column with browser_role: term)");
    }
    const labelColumn = spec.columns.find((c) => samePath(c.path, spec.roles?.term_label));
    const shown = spec.columns.filter((c) => c !== termColumn && c !== labelColumn);
    const names = ["key", ...shown.map((c) => c.name), labelColumn?.name].filter(Boolean);
    const sort = spec.default_sort;
    const order = sort ? `${ident(sort.column)} ${sort.direction === "desc" ? "DESC" : "ASC"}, rowid ${sort.direction === "desc" ? "DESC" : "ASC"}` : "rowid";
    const where = `${ident(termColumn.name)} = ? AND tab = ?`;

    // Each tab with records in this build: its top rows, and how many there are.
    const tabs = spec.tabs.values.filter((t) => meta.tab_counts?.[t.value]);
    const found = [];
    for (const tab of tabs) {
      const rows = await db.query(
        `SELECT ${names.map(ident).join(", ")} FROM records WHERE ${where} ORDER BY ${order} LIMIT ${LIMIT}`,
        [id, tab.value],
      );
      const count = rows.length < LIMIT ? rows.length : (await db.query(`SELECT count(*) AS n FROM records WHERE ${where}`, [id, tab.value]))[0].n;
      found.push({ tab, rows, count });
    }
    status.remove();
    const total = found.reduce((sum, f) => sum + f.count, 0);
    const label = labelColumn ? found.flatMap((f) => f.rows).find((r) => r[labelColumn.name])?.[labelColumn.name] : null;
    const url = curieUrl(id);
    const idPart = url ? el("a", { href: url, rel: "noopener", target: "_blank", class: "term-id" }, id) : el("span", { class: "term-id" }, id);
    document.title = label ? `${label} (${id})` : id || "Mondo term";
    const crumbs = el("p", { class: "crumbs" }, el("a", { href: "index.html" }, "Records"), " › ", el("code", {}, id || "—"));
    if (!total) {
      article.replaceChildren(
        crumbs,
        el("h1", {}, idPart),
        el("p", {}, id ? `No evidence records for ${id} in this index (${meta.record_count.toLocaleString()} records, built ${meta.built_at}).` : "No Mondo term was given."),
        el("p", {}, el("a", { href: "index.html" }, "Back to the records")),
      );
      return;
    }
    const counts = found.filter((f) => f.count).map((f) => `${f.tab.title} ${f.count.toLocaleString()}`);
    article.replaceChildren(
      crumbs,
      el("h1", {}, label ? `${label} ` : "", "(", idPart, ")"),
      el("p", { class: "lede" }, `${total.toLocaleString()} evidence ${total === 1 ? "record" : "records"} about this term (${counts.join(" · ")}).`),
      ...found.map(({ tab, rows, count }) =>
        el(
          "section",
          { class: "panel term-tab", "data-tab": tab.value },
          el("h2", {}, tab.title, " ", el("span", { class: "count" }, `(${count.toLocaleString()})`)),
          tab.description ? el("p", { class: "muted" }, tab.description) : null,
          rows.length ? el("div", { class: "table-scroll" }, table(spec, shown, rows)) : el("p", { class: "empty" }, "None."),
          count > rows.length
            ? el(
                "a",
                { class: "term-more", href: `index.html?tab=${encodeURIComponent(tab.value)}&q=${encodeURIComponent(id)}` },
                `Showing the top ${rows.length.toLocaleString()} of ${count.toLocaleString()}: see them all in the list`,
              )
            : null,
        ),
      ),
    );
    recentStatuses(config).then((live) => applyLive(article, spec, live));
  } catch (error) {
    status.textContent = `Could not load the term: ${error.message}`;
    status.classList.add("error");
    throw error;
  } finally {
    document.body.dataset.loading = "false";
  }
}

main();
