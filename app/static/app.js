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
  if (roles.includes("operator")) items.push(link("queue", "/", "Ticket"), link("call", "/call", "Nuova chiamata"));
  if (roles.includes("manager")) items.push(link("stats", "/stats", "Statistiche"));
  return items;
}
function themeButton() {
  const b = h("button", { class: "btn sm ghost theme-btn", type: "button" });
  const sync = () => {
    const dark = crTheme() === "dark";
    b.textContent = dark ? "☀" : "☾";
    b.setAttribute("aria-label", dark ? "Passa al tema chiaro" : "Passa al tema scuro");
    b.title = b.getAttribute("aria-label");
  };
  b.addEventListener("click", () => { crSetTheme(crTheme() === "dark" ? "light" : "dark"); sync(); });
  sync();
  return b;
}
async function header(active) {
  const me = await api("/api/me");
  const hd = h("header", {},
    h("div", { class: "brand" },
      h("img", { src: "/static/img/logo/siciliacque_bianco.svg", alt: "Siciliacque" }),
      h("h1", {}, "Control Room")),
    ...nav(me.roles || [], active),
    themeButton(),
    h("span", { class: "who" }, me.name),
    h("form", { method: "post", action: "/logout" }, h("button", { class: "btn sm ghost" }, "Esci")));
  document.body.prepend(hd);
  return me;
}

// Menu a tendina con ricerca: si apre al clic, filtra mentre si scrive (ricerca su GLPI),
// frecce + Invio per scegliere, Esc per chiudere. Modificare il testo annulla la scelta.
function combo(inputSel, url, key, onPick) {
  const input = $(inputSel), list = $(inputSel + "-list");
  let items = [], active = -1, timer, seq = 0, picked = null;
  const open = on => { list.style.display = on ? "block" : "none"; input.setAttribute("aria-expanded", on); };
  const render = (msg) => {
    list.replaceChildren(...(msg ? [h("li", { class: "combo-msg" }, msg)] : items.map((it, i) =>
      h("li", { role: "option", class: i === active ? "act" : "", onmousedown: ev => { ev.preventDefault(); choose(i); } }, it.name))));
    const a = list.querySelector(".act"); if (a) a.scrollIntoView({ block: "nearest" });
    open(true);
  };
  const load = async () => {
    const my = ++seq; render("Ricerca…");
    try {
      const r = await api(url + encodeURIComponent(picked ? "" : input.value.trim()));
      if (my !== seq) return;
      items = r[key]; active = items.length ? 0 : -1;
      render(items.length ? null : "Nessun risultato");
    } catch (e) { if (my === seq) render("Errore: " + e.message); }
  };
  const choose = i => {
    picked = items[i]; input.value = picked.name; input.classList.add("picked");
    onPick(picked); open(false);
  };
  input.addEventListener("focus", () => { input.select(); load(); });
  input.addEventListener("input", () => {
    if (picked) { picked = null; input.classList.remove("picked"); onPick(null); }
    clearTimeout(timer); timer = setTimeout(load, 250);
  });
  input.addEventListener("keydown", ev => {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      if (list.style.display !== "block") return load();
      if (!items.length) return;
      active = (active + (ev.key === "ArrowDown" ? 1 : -1) + items.length) % items.length; render();
    } else if (ev.key === "Enter" && list.style.display === "block" && active >= 0) { ev.preventDefault(); choose(active); }
    else if (ev.key === "Escape") open(false);
  });
  input.addEventListener("blur", () => setTimeout(() => open(false), 150));
}
