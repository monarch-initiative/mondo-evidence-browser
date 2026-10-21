// Generic, schema-driven rendering. Nothing here knows any slot by name: values are
// rendered from the view spec (slot kind, range, format and layout annotations).

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
export function renderScalar(spec, kind, value, { format = null, enumName = null } = {}) {
  if (value === null || value === undefined) return el("span", { class: "muted" }, "—");
  if (format === "score") return scoreMeter(value);
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
// designator when it is a descendant of the declared range, else the declared range.
export function resolveClass(spec, declared, obj) {
  const named = obj && typeof obj === "object" ? obj.type : null;
  if (named && named !== declared && spec.classes[named]) {
    if (spec.classes[named].ancestors.includes(declared)) return named;
  }
  return declared;
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
  return el(
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
}

// Any value of a slot, recursively. `depth` only limits nesting of headings.
export function renderValue(spec, slot, value, depth = 0) {
  if (isEmpty(value)) return el("span", { class: "muted" }, "—");
  if (slot.layout === "hidden") return null;
  if (slot.kind === "class") {
    const items = slot.multivalued ? (Array.isArray(value) ? value : [value]) : null;
    if (items) {
      if (slot.layout === "table") return renderTable(spec, slot.class, items);
      return el(
        "ol",
        { class: "cards" },
        items.map((item) =>
          el("li", { class: "card" }, renderObject(spec, resolveClass(spec, slot.class, item), item, depth + 1)),
        ),
      );
    }
    return renderObject(spec, resolveClass(spec, slot.class, value), value, depth + 1);
  }
  const opts = { format: slot.format, enumName: slot.enum };
  if (Array.isArray(value)) return renderList(spec, slot.kind, value, opts);
  if (typeof value === "object") return rawValue(value);
  return renderScalar(spec, slot.kind, value, opts);
}

// An object as a definition list of its class's slots, in schema order. Fields the
// schema does not know (a newer source schema) are shown raw, never dropped.
export function renderObject(spec, className, obj, depth = 0, { skip = [] } = {}) {
  const cls = spec.classes[className];
  if (!cls || typeof obj !== "object" || obj === null) return rawValue(obj);
  const known = new Set(cls.slots.map((s) => s.name));
  const rows = [];
  for (const slot of cls.slots) {
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
      el("dd", { "data-slot": slot.name, class: ddClass }, renderValue(spec, slot, obj[slot.name], depth)),
    );
  }
  const unknown = Object.keys(obj).filter((k) => !known.has(k) && !isEmpty(obj[k]));
  for (const key of unknown) {
    rows.push(el("dt", { class: "unknown", title: "Not in the schema" }, key), el("dd", {}, rawValue(obj[key])));
  }
  return el("dl", { class: "object", "data-class": className }, rows);
}

// Fill a "{a.b} ... {c}" template from a record.
export function fillTemplate(template, record) {
  if (!template) return null;
  return template.replace(/\{([\w.]+)\}/g, (_, path) => {
    const value = getPath(record, path.split("."));
    return value === undefined || value === null ? "" : String(value);
  });
}

// What an expanded table row shows: the spec's preview slots, then a link onwards.
export function renderPreview(spec, record, href) {
  const items = spec.preview || [];
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
        renderValue(spec, item, value),
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
