// One evidence record at its stable URL: record.html?id=<record id>. Everything is
// rendered from the view spec: the heading from the root class's title template,
// the summary from the table columns, the body from the schema's classes, and the
// sign-off form and history from the form and history classes.

import { currentUser, login, renderWho } from "./auth.js";
import { getRecord, loadSite, openDb, readMeta } from "./db.js";
import { decisions, signOff } from "./service.js";
import { evidenceMap, recordGauge } from "./charts.js";
import {
  byRole,
  el,
  fillTemplate,
  getPath,
  isEmpty,
  recordContext,
  recordHeading,
  renderObject,
  renderValue,
  slotTitle,
  templatePaths,
} from "./render.js";

// Key facts: the table's columns the heading does not already say, plus the tab
// ("In Mondo?"). Returns the tiles and the record slots they cover.
function summary(spec, record) {
  // The heading's values, and the score when the verdict's gauge shows it.
  const inHeading = new Set(templatePaths(spec.title_template));
  if (spec.roles?.score) inHeading.add(spec.roles.score.join("."));
  const rows = [];
  const covered = new Set();
  const tile = (title, name, content) =>
    rows.push(el("div", { class: "stat" }, el("dt", {}, title), el("dd", { "data-column": name }, content)));
  for (const column of spec.columns) {
    if (inHeading.has(column.path.join("."))) continue;
    const value = getPath(record, column.path);
    if (isEmpty(value)) continue;
    const slot = { ...column, name: column.name, enum: column.enum, layout: null };
    tile(column.title, column.name, renderValue(spec, slot, value));
    if (column.path.length === 1) covered.add(column.path[0]);
  }
  const tab = spec.tabs && spec.classes[spec.root].slots.find((s) => s.name === spec.tabs.slot);
  if (tab && !isEmpty(record[tab.name])) {
    tile(slotTitle(tab), tab.name, renderValue(spec, tab, record[tab.name]));
    covered.add(tab.name);
  }
  return { tiles: el("dl", { class: "summary" }, rows), covered };
}

// Which way the score points, for the verdict's marker: the score-format column's sign.
function tone(spec, record) {
  const score = spec.columns.find((c) => c.format === "score");
  const value = score ? getPath(record, score.path) : null;
  return typeof value !== "number" || value === 0 ? "" : value > 0 ? "pos" : "neg";
}

// The record's own slots, laid out by the schema: sections (browser_section) as panels,
// folds (browser_collapsed) as closed <details>, anything else in a "Details" panel.
// The evidence section (browser_role: evidence) opens with the map of its sources.
function layout(spec, record, covered, ctx) {
  const components = spec.roles?.components ? getPath(record, spec.roles.components) : null;
  const slots = spec.classes[spec.root].slots.filter(
    (s) => s.layout !== "hidden" && !covered.has(s.name) && !isEmpty(record[s.name]),
  );
  const sections = slots
    .filter((s) => s.section)
    .map((s) =>
      el(
        "section",
        { class: `panel section-${s.section.toLowerCase().replace(/[^a-z0-9]+/g, "-")} ${s.section === "Evidence" ? "evidence" : ""}`.trim(), "data-slot": s.name },
        el("h2", {}, s.section),
        s.role === "evidence" && !isEmpty(components) ? evidenceMap(spec, components, ctx) : null,
        renderValue(spec, s, record[s.name], 0, ctx),
      ),
    );
  const folds = new Map();
  for (const s of slots.filter((x) => x.collapsed && !x.section)) {
    folds.set(s.collapsed, [...(folds.get(s.collapsed) || []), s.name]);
  }
  const folded = [...folds].map(([title, names]) =>
    el(
      "details",
      { class: "panel fold" },
      el("summary", {}, title),
      // A fold of one class-valued slot shows that object directly, without a label.
      names.length === 1 && slots.find((s) => s.name === names[0]).kind === "class"
        ? renderValue(spec, slots.find((s) => s.name === names[0]), record[names[0]], 0, ctx)
        : renderObject(spec, spec.root, record, 0, { only: names, ctx }),
    ),
  );
  const rest = slots.filter((s) => !s.section && !s.collapsed).map((s) => s.name);
  const details = rest.length
    ? el("section", { class: "panel detail" }, el("h2", {}, "Details"), renderObject(spec, spec.root, record, 0, { only: rest, ctx }))
    : null;
  return { sections, details, folded };
}

// Each decision, newest first: a heading from the history class's title template, the
// values the curator entered (the form's fields), and the full decision on demand.
function renderHistory(container, spec, decisions) {
  container.replaceChildren(el("h2", {}, spec.history_title || "History"));
  if (!decisions.length) {
    container.append(el("p", { class: "muted" }, "No sign-offs yet."));
    return;
  }
  const template = spec.classes[spec.history]?.title_template;
  const entered = (decision) =>
    (spec.form?.fields || [])
      .filter((field) => !isEmpty(decision[field.name]))
      .map((field) =>
        el("div", { class: "stat" }, el("dt", {}, slotTitle(field)), el("dd", {}, renderValue(spec, { ...field, layout: null }, decision[field.name]))),
      );
  container.append(
    el(
      "ol",
      { class: "history" },
      [...decisions].reverse().map((d) =>
        el(
          "li",
          { class: "decision" },
          template ? el("p", { class: "decision-title" }, fillTemplate(template, d)) : null,
          el("dl", { class: "summary compact" }, entered(d)),
          el("details", { class: "more" }, el("summary", {}, "Full decision"), renderObject(spec, spec.history, d)),
        ),
      ),
    ),
  );
}

function formField(spec, field) {
  const id = `field-${field.name}`;
  const label = el("span", { class: "field-label" }, slotTitle(field), field.required ? null : el("span", { class: "muted" }, " (optional)"));
  const hint = field.description ? el("span", { class: "hint" }, field.description) : null;
  if (field.widget === "radio" && field.options) {
    return el(
      "fieldset",
      { class: "field radio-group", "data-field": field.name },
      el("legend", {}, label),
      field.options.map((o) =>
        el(
          "label",
          { class: "radio", title: o.description || null },
          el("input", { type: "radio", name: field.name, value: o.value, required: field.required }),
          " ",
          o.title || o.value,
        ),
      ),
      hint,
    );
  }
  let input;
  if (field.options) {
    input = el(
      "select",
      { id, name: field.name, required: field.required },
      el("option", { value: "" }, "—"),
      field.options.map((o) => el("option", { value: o.value }, o.title || o.value)),
    );
  } else if (field.widget === "textarea") {
    input = el("textarea", { id, name: field.name, rows: 2, required: field.required });
  } else {
    input = el("input", { id, name: field.name, required: field.required });
  }
  return el("label", { class: "field", for: id, "data-field": field.name }, label, input, hint);
}

// `live.seen` is the latest decision this page has shown; a sign-off sends it, so the
// service refuses it if another curator has decided since, and the page shows theirs.
function renderSignOff(container, spec, config, record, live, loadHistory) {
  container.replaceChildren(el("h2", {}, "Sign-off"));
  if (!spec.form) return;
  if (!config.service) {
    container.append(el("p", { class: "note" }, "Sign-off is not available on this deployment (no sign-off service is configured)."));
    return;
  }
  const user = currentUser();
  if (!user) {
    container.append(
      config.orcid
        ? el("button", { class: "button", type: "button", onclick: () => login(config) }, "Log in with ORCID to sign off")
        : el("p", { class: "note" }, "ORCID login is not configured for this deployment."),
    );
    return;
  }
  const at = (role) => byRole(spec, record, role);
  const score = at("score");
  const message = el("p", { class: "form-message", role: "status" });
  const form = el(
    "form",
    { class: "signoff-form" },
    spec.form.fields.map((f) => formField(spec, f)),
    el(
      "p",
      { class: "muted" },
      `You are signing off version ${at("version")}, score ${typeof score === "number" ? score.toFixed(3) : "unscored"} ` +
        `(${at("rule")} ${at("rule_version")}), as ${user.name || user.orcid}. ` +
        "Sign-offs are permanent; a later decision is added on top.",
    ),
    el("button", { class: "button", type: "submit" }, "Record sign-off"),
    message,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const values = {};
    for (const field of spec.form.fields) {
      const value = data.get(field.name);
      if (value) values[field.name] = String(value);
    }
    const result = await signOff(config, spec, user, record[spec.key], { record, seenDecision: live.seen, values });
    if (!result.ok) {
      message.textContent = result.body.detail || `Sign-off failed (HTTP ${result.status}).`;
      message.className = "form-message error";
      // Someone else decided meanwhile: show their decision, keep this curator's input.
      if (result.status === 409) await loadHistory();
      return;
    }
    message.textContent = "Sign-off recorded.";
    message.className = "form-message ok";
    form.reset();
    await loadHistory();
  });
  container.append(form);
}

async function main() {
  const status = document.getElementById("status");
  const article = document.getElementById("record");
  try {
    const { spec, config } = await loadSite();
    renderWho(document.getElementById("who"), config, el);
    const id = new URLSearchParams(location.search).get("id");
    const db = await openDb(config);
    const meta = await readMeta(db);
    const record = id ? await getRecord(db, id) : null;
    status.remove();
    if (!record) {
      document.title = "No evidence record";
      article.replaceChildren(
        el("h1", {}, "No evidence record"),
        el("p", {}, id ? `No evidence record ${id} is in this index (${meta.record_count.toLocaleString()} records, built ${meta.built_at}).` : "No record id was given."),
        el("p", {}, el("a", { href: "index.html" }, "Back to the records")),
      );
      return;
    }
    const heading = recordHeading(spec, record);
    document.title = heading.map((part) => (typeof part === "string" ? part : part.textContent)).join("");
    const lede = spec.lede ? getPath(record, spec.lede) : null;
    const signoff = el("section", { class: "panel", id: "signoff" });
    const history = el("section", { class: "panel", id: "history" });
    const ctx = recordContext(spec, record);
    const { tiles, covered } = summary(spec, record);
    const { sections, details, folded } = layout(spec, record, covered, ctx);
    article.replaceChildren(
      el("p", { class: "crumbs" }, el("a", { href: "index.html" }, "Records"), " › ", el("code", {}, record[spec.key])),
      el("h1", {}, heading),
      el(
        "div",
        { class: "verdict" },
        recordGauge(spec, record),
        isEmpty(lede) ? null : el("p", { class: `lede ${tone(spec, record)}`.trim() }, String(lede)),
      ),
      tiles,
      el("div", { class: "main-col" }, sections, details),
      el("aside", { class: "side-col" }, signoff, history),
      ...folded,
      el("details", { class: "raw-record" }, el("summary", {}, "Raw record"), el("pre", { class: "raw" }, JSON.stringify(record, null, 2))),
    );
    const live = { seen: null };
    async function loadHistory() {
      const result = await decisions(config, record[spec.key]);
      if (!result.ok) {
        history.replaceChildren(
          el("h2", {}, "History"),
          el("p", { class: "form-message error" }, `Could not load the sign-off history: ${result.body.detail || `HTTP ${result.status}`}`),
        );
        return;
      }
      const list = result.body.decisions;
      live.seen = list.length ? list[list.length - 1].decision_id : null;
      renderHistory(history, spec, list);
      // The status in this build may predate recent sign-offs; show the live one.
      const column = spec.columns.find((c) => c.name === spec.status);
      const cell = article.querySelector(`dd[data-column="${spec.status}"]`);
      if (column && cell && result.body.status) {
        cell.replaceChildren(renderValue(spec, { ...column, layout: null }, result.body.status));
      }
    }
    renderSignOff(signoff, spec, config, record, live, loadHistory);
    if (config.service && spec.history) {
      await loadHistory();
    } else {
      history.remove();
    }
  } catch (error) {
    status.textContent = `Could not load the record: ${error.message}`;
    status.classList.add("error");
    throw error;
  }
}

main();
