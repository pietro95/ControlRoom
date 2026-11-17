// Tema chiaro/scuro: applicato prima del rendering (script nel <head>) per evitare il lampo di colore.
// Scelta salvata nel browser; senza scelta vale il tema scuro (sala controllo).
(function () {
  var t = null;
  try { t = localStorage.getItem("cr-theme"); } catch (e) {}
  document.documentElement.dataset.theme = t === "light" ? "light" : "dark";
})();
function crTheme() { return document.documentElement.dataset.theme; }
function crSetTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("cr-theme", t); } catch (e) {}
  document.dispatchEvent(new CustomEvent("cr-theme", { detail: t }));
}
