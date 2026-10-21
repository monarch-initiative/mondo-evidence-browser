// ORCID login from a static page: the OpenID Connect implicit flow. ORCID returns an
// id_token (a JWT signed by ORCID) in the redirect fragment. The page keeps it for
// the tab's session and sends it as a bearer token to the sign-off service, which
// verifies the signature; nothing here is trusted by the service.

import { siteUrl } from "./db.js";

const TOKEN = "meb.id_token";
const PENDING = "meb.login";

function decodePayload(token) {
  const part = token.split(".")[1];
  const json = atob(part.replaceAll("-", "+").replaceAll("_", "/"));
  return JSON.parse(decodeURIComponent(escape(json)));
}

function randomString() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

// The logged-in curator, or null. The expiry check only hides stale sessions.
export function currentUser() {
  const token = sessionStorage.getItem(TOKEN);
  if (!token) return null;
  const claims = decodePayload(token);
  if (claims.exp && claims.exp * 1000 < Date.now()) {
    sessionStorage.removeItem(TOKEN);
    return null;
  }
  const name = [claims.given_name, claims.family_name].filter(Boolean).join(" ") || null;
  return { orcid: claims.sub, name, token };
}

export function login(config) {
  const state = randomString();
  const nonce = randomString();
  sessionStorage.setItem(
    PENDING,
    JSON.stringify({ state, nonce, back: location.pathname + location.search }),
  );
  const params = new URLSearchParams({
    client_id: config.orcid.clientId,
    response_type: "token",
    scope: "openid",
    redirect_uri: siteUrl("auth.html"),
    state,
    nonce,
  });
  location.assign(`${config.orcid.baseUrl}/oauth/authorize?${params}`);
}

export function logout() {
  sessionStorage.removeItem(TOKEN);
  location.reload();
}

// auth.html: take the id_token from the fragment, check state and nonce, go back.
export function completeLogin() {
  const params = new URLSearchParams(location.hash.slice(1));
  const pending = JSON.parse(sessionStorage.getItem(PENDING) || "null");
  sessionStorage.removeItem(PENDING);
  if (params.get("error")) return { error: `ORCID login was cancelled (${params.get("error")}).` };
  const token = params.get("id_token");
  if (!pending || params.get("state") !== pending.state) {
    return { error: "This login could not be verified. Start it again from the browser." };
  }
  if (!token) return { error: "ORCID did not return an id_token (is the client set up for OpenID?)." };
  if (decodePayload(token).nonce !== pending.nonce) {
    return { error: "This login could not be verified. Start it again from the browser." };
  }
  sessionStorage.setItem(TOKEN, token);
  return { back: pending.back };
}

// The header's login area.
export function renderWho(container, config, el) {
  container.replaceChildren();
  const user = currentUser();
  if (user) {
    container.append(
      el("span", { class: "who-name" }, user.name || user.orcid),
      el("a", { class: "orcid", href: `https://orcid.org/${user.orcid}` }, user.orcid),
      el("button", { class: "link", type: "button", onclick: logout }, "Log out"),
    );
  } else if (config.orcid) {
    container.append(
      el("button", { class: "button small", type: "button", onclick: () => login(config) }, "Log in with ORCID"),
    );
  }
}
