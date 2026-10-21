// One evidence record at its stable URL: record.html?id=<record id>. Everything is
// rendered from the view spec: the heading from the root class's title template,
// the summary from the table columns, the body from the schema's classes, and the
// sign-off form and history from the form and history classes.

import { currentUser, login, renderWho } from "./auth.js";
import { getRecord, loadSite, openDb, readMeta } from "./db.js";
import { el, fillTemplate, getPath, isEmpty, renderObject, renderValue, slotTitle } from "./render.js";

function summary(spec, record) {
  const rows = [];
  for (const column of spec.columns) {
    const value = getPath(record, column.path);
    if (isEmpty(value)) continue;
    const slot = { ...column, name: column.name, enum: column.enum, layout: null };
    rows.push(el("dt", {}, column.title), el("dd", { "data-column": column.name }, renderValue(spec, slot, value)));
  }
  return el("dl", { class: "summary" }, rows);
}

async function service(config, path, options = {}) {
  let response;
  try {
    // The sign-off service is another system: say so plainly when it cannot be reached.
    response = await fetch(`${config.service.url}${path}`, options);
  } catch (error) {
    return { ok: false, status: 0, body: { detail: `The sign-off service could not be reached (${error.message}).` } };
  }
  const body = await response.json().catch(() => ({}));
  return { ok: response.ok, status: response.status, body };
}

function renderHistory(container, spec, decisions) {
  container.replaceChildren(el("h3", {}, "History"));
  if (!decisions.length) {
    container.append(el("p", { class: "muted" }, "No sign-offs yet."));
    return;
  }
  container.append(
    el(
      "ol",
      { class: "history" },
      [...decisions].reverse().map((d) => el("li", { class: "card" }, renderObject(spec, spec.history, d))),
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
  const score = record.inclusion_score || {};
  const message = el("p", { class: "form-message", role: "status" });
  const form = el(
    "form",
    { class: "signoff-form" },
    spec.form.fields.map((f) => formField(spec, f)),
    el(
      "p",
      { class: "muted" },
      `You are signing off version ${record.record_version}, score ${score.value?.toFixed?.(3) ?? "unscored"} ` +
        `(${score.rule_id} ${score.rule_version}), as ${user.name || user.orcid}. ` +
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
    const result = await service(config, `/api/records/${encodeURIComponent(record[spec.key])}/sign-off`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${user.token}` },
      body: JSON.stringify({
        seen_version: record.record_version,
        seen_rule_version: score.rule_version ?? null,
        seen_decision: live.seen,
        values,
      }),
    });
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
    const title = fillTemplate(spec.title_template, record) || record[spec.key];
    document.title = title;
    const signoff = el("section", { class: "panel", id: "signoff" });
    const history = el("section", { class: "panel", id: "history" });
    article.replaceChildren(
      el("p", { class: "crumbs" }, el("a", { href: "index.html" }, "Records"), " › ", el("code", {}, record[spec.key])),
      el("h1", {}, title),
      summary(spec, record),
      el("section", { class: "panel detail" }, renderObject(spec, spec.root, record)),
      signoff,
      history,
      el("details", {}, el("summary", {}, "Raw record"), el("pre", { class: "raw" }, JSON.stringify(record, null, 2))),
    );
    const live = { seen: null };
    async function loadHistory() {
      const result = await service(config, `/api/records/${encodeURIComponent(record[spec.key])}/decisions`);
      if (!result.ok) {
        history.replaceChildren(
          el("h3", {}, "History"),
          el("p", { class: "form-message error" }, `Could not load the sign-off history: ${result.body.detail || `HTTP ${result.status}`}`),
        );
        return;
      }
      const decisions = result.body.decisions;
      live.seen = decisions.length ? decisions[decisions.length - 1].decision_id : null;
      renderHistory(history, spec, decisions);
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
