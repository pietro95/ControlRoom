// Utility condivise. Nessun innerHTML con dati dinamici: tutto via textContent (anti-XSS).
const $ = (s, el = document) => el.querySelector(s);
function h(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const k of kids.flat()) e.append(k instanceof Node ? k : document.createTextNode(k ?? ""));
  return e;
}
async function api(path, opts = {}) {
  const o = { credentials: "same-origin", headers: {}, ...opts };
  if (opts.body) { o.headers["Content-Type"] = "application/json"; o.headers["X-CR"] = "1"; o.body = JSON.stringify(opts.body); o.method = "POST"; }
  const r = await fetch(path, o);
  if (r.status === 401) { location.href = "/login"; throw new Error("auth"); }
  let data = {}; try { data = await r.json(); } catch (_) {}
  if (!r.ok) { const e = new Error(data.detail || data.error || ("Errore " + r.status)); e.status = r.status; throw e; }
  return data;
}
function toast(msg, ms = 3500) {
  let t = $(".toast"); if (!t) { t = h("div", { class: "toast" }); document.body.append(t); }
  t.textContent = msg; t.style.display = "block"; clearTimeout(t._t); t._t = setTimeout(() => t.style.display = "none", ms);
}
function fmtTime(s) { // "2026-11-17 09:12:00" -> "09:12"
  return s ? s.slice(11, 16) : "";
}
// Voci di menu per ruolo: il server applica comunque i permessi, questo è solo comodità.
function nav(roles, active) {
  const items = [];
  const link = (key, href, label) => h("a", { class: "btn sm " + (active === key ? "" : "ghost"), href }, label);
  if (roles.includes("operator")) items.push(link("queue", "/", "Allarmi"), link("call", "/call", "Nuova chiamata"));
  if (roles.includes("manager")) items.push(link("stats", "/stats", "Statistiche"));
  return items;
}
async function header(active) {
  const me = await api("/api/me");
  const hd = h("header", {},
    h("h1", {}, "Control Room"),
    ...nav(me.roles || [], active),
    h("span", { class: "who" }, me.name),
    h("form", { method: "post", action: "/logout" }, h("button", { class: "btn sm ghost" }, "Esci")));
  document.body.prepend(hd);
  return me;
}
