// The record page's pictures of the score: a gauge for the verdict, a bar per evidence
// line for the maths, and a map of where the evidence comes from. Like the rest of the
// front end they know no slot by name: they read slots by browser_role (see the schema).

import { el, getPath, isEmpty, renderScalar, slotAtPath } from "./render.js";

function roleOf(cls, role) {
  return cls?.slots.find((s) => s.role === role)?.name;
}

// A reader of an object's slots by role: at("weight") -> the weight slot's value.
function reader(cls, obj) {
  return (role) => {
    const name = roleOf(cls, role);
    return name ? obj?.[name] : undefined;
  };
}

function signed(value) {
  return (value >= 0 ? "+" : "-") + Math.abs(value).toFixed(2);
}

function sourceName(spec, id) {
  return spec.labels?.resources?.[id] || id || "Unknown source";
}

// Bands' tone, from where they sit: tone-neg-2 (strongly disputed) .. tone-pos-2.
function tone(middle) {
  const size = Math.abs(middle) < 0.2 ? 0 : Math.abs(middle) < 0.7 ? 1 : 2;
  return size === 0 ? "tone-0" : `tone-${middle > 0 ? "pos" : "neg"}-${size}`;
}

// The score on a -1 .. +1 scale shaded by the rule's bands, marked where it sits.
export function scoreGauge(spec, value, band) {
  const bands = [...(spec.score_bands || [])].sort((a, b) => a.min - b.min);
  const scored = typeof value === "number";
  const segments = bands.map((b, i) => {
    const upper = i + 1 < bands.length ? bands[i + 1].min : 1;
    return el("span", {
      class: `gauge-band ${tone((b.min + upper) / 2)} ${b.label === band ? "current" : ""}`.trim(),
      "data-band": b.label,
      title: b.label,
      style: `width: ${((upper - b.min) / 2) * 100}%`,
    });
  });
  const marker = scored ? el("span", { class: "gauge-marker", style: `left: ${(value + 1) * 50}%` }) : null;
  const label = scored
    ? `Score ${value.toFixed(2)} on a scale from -1 (disputed) to +1 (supported)${band ? `: ${band}` : ""}.`
    : "Not scored: no rule covers this claim type.";
  return el(
    "figure",
    { class: `gauge ${scored ? (value > 0 ? "pos" : value < 0 ? "neg" : "") : "unscored"}`.trim(), role: "img", "aria-label": label },
    el(
      "div",
      { class: "gauge-reading", "aria-hidden": "true" },
      el("span", { class: "gauge-value" }, scored ? signed(value) : "—"),
      el("span", { class: "gauge-band-name" }, scored ? band || "" : "unscored"),
    ),
    el("div", { class: "gauge-track", "aria-hidden": "true" }, segments, marker),
    el(
      "div",
      { class: "gauge-ends", "aria-hidden": "true" },
      el("span", {}, "-1 disputed"),
      el("span", {}, "0"),
      el("span", {}, "+1 supported"),
    ),
  );
}

// How the score adds up: the claim prior, then each line's k × w as a bar from zero
// (right supports, left disputes; dashed when the line does not count, with why), then
// the total log-odds L and the score it gives.
export function contributionChart(spec, className, score) {
  const cls = spec.classes[className];
  const at = reader(cls, score);
  const componentsSlot = cls.slots.find((s) => s.role === "components");
  const componentCls = spec.classes[componentsSlot?.class];
  const k = typeof at("scaling") === "number" ? at("scaling") : 1;
  const prior = typeof at("prior") === "number" ? at("prior") : 0;
  const lines = (at("components") || []).map((c) => {
    const line = reader(componentCls, c);
    const counted = line("counted") !== false;
    return {
      id: line("line"),
      source: line("source"),
      kind: line("kind"),
      value: k * (line("weight") || 0),
      counted,
      reason: line("reason"),
    };
  });
  const total =
    typeof at("log_odds") === "number" ? at("log_odds") : prior + lines.filter((l) => l.counted).reduce((sum, l) => sum + l.value, 0);
  const values = [prior, total, ...lines.map((l) => l.value)];
  const low = Math.min(0, ...values);
  const high = Math.max(0, ...values);
  const span = high - low || 1;
  const zero = (-low / span) * 100;
  const row = (classes, attrs, name, detail, value, note) =>
    el(
      "div",
      { class: `contribution ${classes} ${value > 0 ? "pos" : value < 0 ? "neg" : "zero"}`.trim(), ...attrs },
      el("div", { class: "contribution-label" }, el("span", { class: "name" }, name), detail ? el("span", { class: "detail" }, detail) : null),
      el(
        "div",
        { class: "track", style: `--zero: ${zero}%` },
        el("span", {
          class: "bar",
          style: `left: ${((Math.min(value, 0) - low) / span) * 100}%; width: ${(Math.abs(value) / span) * 100}%`,
        }),
      ),
      el("span", { class: "contribution-value" }, signed(value)),
      note ? el("p", { class: "contribution-note" }, note) : null,
    );
  const value = at("score");
  const band = at("band");
  return el(
    "figure",
    { class: "contributions" },
    row("prior", {}, "Starting point", "claim prior L0", prior),
    lines.map((l) =>
      row(
        l.counted ? "" : "not-counted",
        { "data-line": l.id || null },
        sourceName(spec, l.source),
        [l.kind, l.counted ? null : "not counted"].filter(Boolean).join(" · "),
        l.value,
        l.counted ? null : l.reason || "Not counted.",
      ),
    ),
    row("total", {}, "Total", "log-odds L", total),
    el(
      "figcaption",
      {},
      typeof value === "number"
        ? `Score S = tanh(L / 2) = ${value.toFixed(2)}${band ? `, ${band}` : ""}. Each bar is k × w with k = ${k}.`
        : "Not scored.",
    ),
  );
}

function plural(n, one, many) {
  return n === 1 ? one : many;
}

// Where the evidence comes from: the record's Mondo term, a branch per independent
// source (supports or disputes), and sources that derive from each other boxed together
// as one. Each source links to its evidence line on the page (#line-<line id>).
export function evidenceMap(spec, components, ctx) {
  const componentsSlot = spec.roles?.components ? slotAtPath(spec, spec.roles.components) : null;
  const cls = spec.classes[componentsSlot?.class];
  const lines = (components || []).map((c) => {
    const at = reader(cls, c);
    return {
      id: at("line"),
      source: at("source"),
      kind: at("kind"),
      cluster: at("cluster") ?? at("source"),
      direction: at("direction"),
      counted: at("counted") !== false,
      reason: at("reason"),
    };
  });
  const clusters = new Map();
  for (const line of lines) clusters.set(line.cluster, [...(clusters.get(line.cluster) || []), line]);
  const groups = [...clusters.values()].map((members) => {
    const lead = members.find((m) => m.counted) || members[0];
    return { members, counted: members.some((m) => m.counted), against: lead.direction < 0 };
  });
  const node = (line) =>
    el(
      "a",
      {
        class: `map-node ${line.counted ? "" : "not-counted"}`.trim(),
        href: line.id ? `#line-${line.id}` : null,
        title: line.counted ? null : line.reason || "Not counted",
      },
      el("span", { class: "map-source" }, sourceName(spec, line.source)),
      el("span", { class: "map-kind" }, [line.kind, line.counted ? null : "not counted"].filter(Boolean).join(" · ")),
    );
  const supports = groups.filter((g) => g.counted && !g.against).length;
  const disputes = groups.filter((g) => g.counted && g.against).length;
  const caption =
    (supports
      ? `${supports} independent ${plural(supports, "source supports", "sources support")} this statement`
      : "No independent source supports this statement") +
    `; ${disputes ? `${disputes} ${plural(disputes, "disputes", "dispute")}` : "none dispute"} it.`;
  return el(
    "figure",
    { class: "evidence-map" },
    el(
      "div",
      { class: "map-hub" },
      el("span", { class: "map-origin" }, "Mondo"),
      el("span", { class: "map-label" }, ctx?.termLabel || "—"),
      isEmpty(ctx?.term) ? null : el("span", { class: "map-id" }, renderScalar(spec, "curie", ctx.term, { format: "curie" })),
    ),
    el(
      "ul",
      { class: "map-branches" },
      groups.map((g) =>
        el(
          "li",
          { class: `map-branch ${g.against ? "dir-neg" : "dir-pos"} ${g.counted ? "" : "not-counted"}`.trim() },
          el("span", { class: "map-edge" }, g.against ? "disputes" : "supports"),
          g.members.length > 1
            ? el(
                "div",
                { class: "map-cluster" },
                g.members.map(node),
                el("p", { class: "map-note" }, "These derive from each other, so they count once."),
              )
            : node(g.members[0]),
        ),
      ),
    ),
    el("figcaption", {}, caption),
  );
}

// The record's score gauge, from the record-level roles (score, band); null without them.
export function recordGauge(spec, record) {
  if (!spec.roles?.score) return null;
  return scoreGauge(spec, getPath(record, spec.roles.score), spec.roles.band ? getPath(record, spec.roles.band) : null);
}
