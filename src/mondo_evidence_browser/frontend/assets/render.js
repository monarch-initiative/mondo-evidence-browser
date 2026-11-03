// Generic, schema-driven rendering. Nothing here knows any slot by name: values are
// rendered from the view spec (slot kind, range, format and layout annotations).

import { contributionChart } from "./charts.js";

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function isEmpty(value) {
  return (
    value === null ||
    value === undefined ||
    value === "" ||
    (Array.isArray(value) && value.length === 0) ||
    (typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === 0)
  );
}

export function getPath(obj, path) {
  let value = obj;
  for (const step of path) {
    if (value === null || typeof value !== "object") return undefined;
    value = value[step];
  }
  return value;
}

export function enumEntry(spec, enumName, value) {
  return (spec.enums[enumName] || []).find((v) => v.value === value);
}

export function curieUrl(value) {
  if (typeof value !== "string") return null;
  if (/^https?:\/\//.test(value)) return value;
  if (value.startsWith("ORCID:")) return "https://orcid.org/" + value.slice(6);
  if (/^[A-Za-z][\w.-]*:[^\s/]+$/.test(value)) return "https://bioregistry.io/" + value;
  return null;
}

export function formatNumber(value, format) {
  if (value === null || value === undefined) return format === "score" ? "unscored" : "—";
  if (format === "score") return (value >= 0 ? "+" : "") + value.toFixed(3);
  return Number.isInteger(value) ? String(value) : String(Math.round(value * 1000) / 1000);
}

export function scoreMeter(value) {
  const cls = value > 0 ? "pos" : value < 0 ? "neg" : "none";
  const fill = el("span", { class: "meter-fill" });
  if (value !== null && value !== undefined) {
    const half = Math.abs(value) * 50;
    fill.style.left = value >= 0 ? "50%" : `${50 - half}%`;
    fill.style.width = `${half}%`;
  }
  return el(
    "span",
    { class: `meter ${cls}` },
    el("span", { class: "meter-track" }, fill),
    el("span", { class: "meter-num" }, formatNumber(value, "score")),
  );
}

// A scalar (non-class) value, for both table cells and the detail view.
export function renderScalar(spec, kind, value, { format = null, enumName = null, labels = null } = {}) {
  if (value === null || value === undefined) return el("span", { class: "muted" }, "—");
  if (format === "score") return scoreMeter(value);
  // Ids the schema marks with browser_labels read by name (e.g. "Disease Ontology").
  const name = labels ? spec.labels?.[labels]?.[value] : null;
  if (name) return el("span", { title: value }, name);
  if (kind === "enum") {
    const entry = enumEntry(spec, enumName, value);
    return el(
      "span",
      { class: `badge badge-${String(value).toLowerCase()}`, title: entry?.description || null },
      entry?.title || value,
    );
  }
  if (format === "badge") return el("span", { class: "badge" }, value);
  if (kind === "boolean") return document.createTextNode(value ? "yes" : "no");
  if (kind === "number" || kind === "integer") {
    return document.createTextNode(formatNumber(value, format));
  }
  // CURIE-typed values and plain URLs become links.
  const url = curieUrl(value);
  const linkable = kind === "curie" || format === "curie" || /^https?:\/\//.test(String(value));
  if (url && linkable) return el("a", { href: url, rel: "noopener", target: "_blank" }, value);
  return document.createTextNode(String(value));
}

export function renderList(spec, kind, values, opts) {
  return el(
    "span",
    { class: "chips" },
    values.map((v) => el("span", { class: "chip" }, renderScalar(spec, kind, v, opts))),
  );
}

// The class to render an object as: the more specific class named by its `type`
// designator when it is a descendant of the declared range, else the declared range;
// then the browser's overlay of that class, if the schema has one (browser_overlay).
export function resolveClass(spec, declared, obj) {
  let name = declared;
  const named = obj && typeof obj === "object" ? obj.type : null;
  if (named && named !== declared && spec.classes[named]?.ancestors.includes(declared)) name = named;
  return spec.overlays?.[name] ?? name;
}

// The record-level values renderers need (the record's Mondo term), from the spec's roles.
export function recordContext(spec, record) {
  const at = (role) => (spec.roles?.[role] ? getPath(record, spec.roles[role]) : undefined);
  return { term: at("term"), termLabel: at("term_label") };
}

function slotWithRole(cls, role) {
  return cls.slots.find((s) => s.role === role);
}

function valueWithRole(cls, obj, role) {
  const slot = slotWithRole(cls, role);
  return slot ? obj[slot.name] : undefined;
}

function linkOrText(spec, value, kind = "curie") {
  return isEmpty(value) ? null : renderScalar(spec, kind, value, { format: "curie" });
}

// A link to a file reads as its name: ".../mappings/mondo.sssom.tsv" -> "mondo.sssom.tsv".
function viaLink(value) {
  if (isEmpty(value)) return null;
  const text = String(value);
  if (!/^https?:\/\//.test(text)) return document.createTextNode(text);
  const name = text.replace(/\/+$/, "").split("/").pop() || text;
  return el("a", { href: text, rel: "noopener", target: "_blank", title: text }, name);
}

function term(spec, id, label, origin) {
  return el(
    "div",
    { class: "term" },
    el("span", { class: "term-origin" }, origin),
    el("span", { class: "term-label" }, label || "—"),
    id ? el("span", { class: "term-id" }, linkOrText(spec, id)) : null,
  );
}

// Renderers a class can ask for with browser_renderer. Each reads slots by their
// browser_role, never by name, and shows any other visible slots generically.
const RENDERERS = {
  // Concordance: the Mondo term and the source's term side by side, and the mapping.
  mapping(spec, className, item, depth, ctx) {
    const cls = spec.classes[className];
    const role = (r) => valueWithRole(cls, item, r);
    const sourceId = role("source_id");
    const source = role("source_name") || spec.labels?.resources?.[sourceId] || sourceId || "Another source";
    const predicate = role("mapping_predicate");
    const facts = [
      ["Mapping from", viaLink(role("via"))],
      ["Mapping predicate", isEmpty(predicate) ? null : document.createTextNode(String(predicate))],
      ["Confidence", isEmpty(role("confidence")) ? null : document.createTextNode(formatNumber(role("confidence")))],
      [`${source} asserts`, isEmpty(role("source_assertion")) ? null : document.createTextNode(String(role("source_assertion")))],
    ].filter(([, value]) => value);
    const used = cls.slots.filter((s) => s.role).map((s) => s.name);
    const rest = cls.slots.some((s) => !used.includes(s.name) && s.layout !== "hidden" && !isEmpty(item[s.name]))
      ? renderObject(spec, className, item, depth, { skip: used, ctx })
      : null;
    return el(
      "article",
      { class: "mapping", "data-class": className },
      el("header", {}, el("span", { class: "kind" }, "Concordance"), el("span", { class: "source" }, source)),
      el(
        "div",
        { class: "mapping-pair" },
        term(spec, ctx?.term, ctx?.termLabel, "Mondo"),
        el("span", { class: "mapping-link", "aria-label": predicate ? `mapped by ${predicate}` : "mapped to" }, "⟷"),
        term(spec, role("source_term"), role("source_term_label"), source),
      ),
      facts.length ? el("dl", { class: "facts" }, facts.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])) : null,
      rest,
    );
  },
  // An evidence line: which way and how strongly, then its evidence items.
  line(spec, className, line, depth, ctx) {
    const cls = spec.classes[className];
    const direction = valueWithRole(cls, line, "direction");
    const strength = valueWithRole(cls, line, "strength");
    const items = slotWithRole(cls, "items");
    const used = cls.slots.filter((s) => s.role).map((s) => s.name);
    const words = [direction, strength ? `${strength} evidence` : null].filter(Boolean);
    const anchor = valueWithRole(cls, line, "anchor");
    return el(
      "section",
      { class: "line", "data-class": className, id: isEmpty(anchor) ? null : `line-${anchor}` },
      words.length
        ? el(
            "p",
            { class: `line-head dir-${String(direction || "").toLowerCase()}` },
            words.map((w, i) => el("span", { class: i === 0 && direction ? "dir" : "strength" }, String(w))),
          )
        : null,
      items ? renderValue(spec, items, line[items.name], depth, ctx) : null,
      renderObject(spec, className, line, depth, { skip: used, ctx }),
    );
  },
  // A score: each line's contribution drawn as a bar, then every number as usual.
  score(spec, className, score, depth, ctx) {
    return el(
      "div",
      { class: "score-maths", "data-class": className },
      contributionChart(spec, className, score),
      renderObject(spec, className, score, depth, { ctx }),
    );
  },
};

function rendered(spec, className, obj, depth, ctx) {
  const renderer = RENDERERS[spec.classes[className]?.renderer];
  return renderer ? renderer(spec, className, obj, depth, ctx) : renderObject(spec, className, obj, depth, { ctx });
}

export function slotTitle(slot) {
  return slot.title || slot.name.replaceAll("_", " ");
}

function rawValue(value) {
  return el("pre", { class: "raw-inline" }, JSON.stringify(value, null, 2));
}

function renderTable(spec, className, items) {
  const cls = spec.classes[className];
  const slots = cls.slots.filter((s) => items.some((item) => !isEmpty(item?.[s.name])));
  const wrap = el(
    "div",
    { class: "detail-wrap" },
    el(
      "table",
      { class: "detail-table", "data-class": className },
      el(
        "thead",
        {},
        el(
          "tr",
          {},
          slots.map((s) =>
            el("th", { title: s.description || null, class: s.description ? "has-help" : null }, slotTitle(s)),
          ),
        ),
      ),
      el(
        "tbody",
        {},
        items.map((item) =>
          el(
            "tr",
            {},
            slots.map((s) =>
              el(
                "td",
                {
                  "data-slot": s.name,
                  "data-label": slotTitle(s),
                  title: s.description ? `${slotTitle(s)}: ${s.description}` : null,
                  class: s.kind === "number" || s.kind === "integer" ? "num" : null,
                },
                renderValue(spec, s, item?.[s.name], 2),
              ),
            ),
          ),
        ),
      ),
    ),
  );
  // Stack rows into label-value pairs only when the table really does not fit, so the
  // decision holds for any font and any width (re-checked whenever the width changes).
  const fit = () => {
    wrap.classList.remove("stacked");
    if (wrap.scrollWidth > wrap.clientWidth + 1) wrap.classList.add("stacked");
  };
  new ResizeObserver(fit).observe(wrap);
  return wrap;
}

// Any value of a slot, recursively. `depth` only limits nesting of headings; `ctx` is
// what renderers need from the record (see recordContext).
export function renderValue(spec, slot, value, depth = 0, ctx = null) {
  if (isEmpty(value)) return el("span", { class: "muted" }, "—");
  if (slot.layout === "hidden") return null;
  if (slot.kind === "class") {
    const items = slot.multivalued ? (Array.isArray(value) ? value : [value]) : null;
    if (items) {
      if (slot.layout === "table") return renderTable(spec, slot.class, items);
      return el(
        "ol",
        { class: "cards" },
        items.map((item) => {
          const cls = resolveClass(spec, slot.class, item);
          const plain = !spec.classes[cls]?.renderer;
          return el("li", { class: plain ? "card" : "card-rendered" }, rendered(spec, cls, item, depth + 1, ctx));
        }),
      );
    }
    return rendered(spec, resolveClass(spec, slot.class, value), value, depth + 1, ctx);
  }
  const opts = { format: slot.format, enumName: slot.enum, labels: slot.labels };
  if (Array.isArray(value)) return renderList(spec, slot.kind, value, opts);
  if (typeof value === "object") return rawValue(value);
  return renderScalar(spec, slot.kind, value, opts);
}

// An object as a definition list of its class's slots, in schema order. Fields the
// schema does not know (a newer source schema) are shown raw, never dropped.
export function renderObject(spec, className, obj, depth = 0, { skip = [], only = null, ctx = null } = {}) {
  const cls = spec.classes[className];
  if (!cls || typeof obj !== "object" || obj === null) return rawValue(obj);
  const known = new Set(cls.slots.map((s) => s.name));
  const rows = [];
  for (const slot of cls.slots) {
    if (only && !only.includes(slot.name)) continue;
    if (skip.includes(slot.name) || slot.layout === "hidden" || isEmpty(obj[slot.name])) continue;
    // Lists of objects (tables, cards) get the full width under their label.
    const wide = slot.kind === "class" && slot.multivalued;
    const ddClass = wide ? "wide" : slot.kind === "class" ? "nested" : null;
    rows.push(
      el(
        "dt",
        {
          title: slot.description || null,
          class: [wide ? "wide" : "", slot.description ? "has-help" : ""].join(" ").trim() || null,
        },
        slotTitle(slot),
      ),
      el("dd", { "data-slot": slot.name, class: ddClass }, renderValue(spec, slot, obj[slot.name], depth, ctx)),
    );
  }
  // Fields the schema does not know: shown raw, never dropped (not when picking `only`).
  const unknown = only ? [] : Object.keys(obj).filter((k) => !known.has(k) && !isEmpty(obj[k]));
  for (const key of unknown) {
    rows.push(el("dt", { class: "unknown", title: "Not in the schema" }, key), el("dd", {}, rawValue(obj[key])));
  }
  return el("dl", { class: "object", "data-class": className }, rows);
}

// Fill a "{a.b} ... {c}" template from a record.
// "2026-10-21T18:49:55+00:00" reads as "21 Oct 2026, 18:49 UTC" in headings.
export function readableTime(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(value)) return value;
  const time = new Date(value);
  if (Number.isNaN(time.getTime())) return value;
  const text = time.toLocaleString("en-GB", {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC",
  });
  return `${text} UTC`;
}

export function fillTemplate(template, record) {
  if (!template) return null;
  return template.replace(/\{([\w.]+)\}/g, (_, path) => {
    const value = getPath(record, path.split("."));
    return value === undefined || value === null ? "" : String(readableTime(value));
  });
}

// The slot at a path from the root class, or null.
export function slotAtPath(spec, path) {
  let className = spec.root;
  let slot = null;
  for (const step of path) {
    slot = spec.classes[className]?.slots.find((s) => s.name === step) ?? null;
    if (!slot) return null;
    className = spec.overlays?.[slot.class] ?? slot.class;
  }
  return slot;
}

// The paths a template uses, e.g. ["statement.subject", ...].
export function templatePaths(template) {
  return [...(template || "").matchAll(/\{([\w.]+)\}/g)].map((m) => m[1]);
}

// A template as page content: identifiers (curie slots) become links.
export function fillHeading(spec, template, record) {
  if (!template) return [];
  const parts = [];
  let last = 0;
  for (const match of template.matchAll(/\{([\w.]+)\}/g)) {
    parts.push(template.slice(last, match.index));
    const path = match[1].split(".");
    const value = getPath(record, path);
    const slot = slotAtPath(spec, path);
    const curie = slot && (slot.kind === "curie" || slot.format === "curie");
    if (value !== undefined && value !== null) {
      parts.push(curie ? renderScalar(spec, "curie", value, { format: "curie" }) : String(readableTime(value)));
    }
    last = match.index + match[0].length;
  }
  parts.push(template.slice(last));
  return parts;
}

// A record's heading: the title template when every value it names is there (its
// identifiers as links), else the record's own sentence (browser_role: statement_text),
// else its id.
export function recordHeading(spec, record) {
  const paths = templatePaths(spec.title_template);
  if (paths.length && paths.every((p) => !isEmpty(getPath(record, p.split("."))))) {
    return fillHeading(spec, spec.title_template, record);
  }
  const text = spec.roles?.statement_text ? getPath(record, spec.roles.statement_text) : null;
  return [isEmpty(text) ? String(record[spec.key]) : String(text)];
}

// What an expanded table row shows: the spec's preview slots, then a link onwards.
export function renderPreview(spec, record, href) {
  const items = spec.preview || [];
  const ctx = recordContext(spec, record);
  return el(
    "div",
    { class: "preview-body" },
    items.map((item) => {
      const value = getPath(record, item.path);
      if (isEmpty(value)) return null;
      return el(
        "section",
        { class: "preview-item", "data-preview": item.name },
        el("h4", { title: item.description || null }, item.title),
        renderValue(spec, item, value, 0, ctx),
      );
    }),
    el("p", { class: "preview-open" }, el("a", { href, class: "button small" }, "Open the full record")),
  );
}

// The list page's explanation of a class (the score): its description, then what each
// of its slots means, including the slots of the classes it lists (the components).
export function renderExplainer(spec, className, ruleNote) {
  const cls = spec.classes[className];
  if (!cls) return null;
  const definitions = (c) =>
    el(
      "dl",
      { class: "definitions", "data-class": c.name },
      c.slots
        .filter((s) => s.description)
        .flatMap((s) => [
          el("dt", { "data-slot": s.name }, slotTitle(s)),
          el("dd", { "data-slot": s.name }, s.description),
        ]),
    );
  const nested = cls.slots
    .filter((s) => s.kind === "class" && s.multivalued && spec.classes[s.class])
    .map((s) => el("section", {}, el("h4", {}, slotTitle(s)), definitions(spec.classes[s.class])));
  return el(
    "details",
    { class: "explainer panel" },
    el("summary", {}, `How the ${(cls.slots.find((s) => s.format === "score") ? "score" : cls.title).toLowerCase()} is calculated`),
    cls.description ? el("p", {}, cls.description) : null,
    ruleNote ? el("p", { class: "muted" }, ruleNote) : null,
    definitions(cls),
    nested,
  );
}
