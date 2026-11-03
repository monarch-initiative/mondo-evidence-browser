// Calls to the sign-off service, shared by the record page and the list's quick sign-off.

// A JSON call; a service that cannot be reached comes back as a failed result, not an
// exception, so pages can say so plainly.
export async function service(config, path, options = {}) {
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

// A record's decisions, oldest first, and its live review status.
export function decisions(config, key) {
  return service(config, `/api/records/${encodeURIComponent(key)}/decisions`);
}

// Record a sign-off on what the curator saw: the record's version and score rule, and
// the latest decision they were shown (null for none). The service refuses it if any
// of those changed meanwhile.
export function signOff(config, user, key, { record, seenDecision, values }) {
  return service(config, `/api/records/${encodeURIComponent(key)}/sign-off`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${user.token}` },
    body: JSON.stringify({
      seen_version: record.record_version,
      seen_rule_version: record.inclusion_score?.rule_version ?? null,
      seen_decision: seenDecision,
      values,
    }),
  });
}
