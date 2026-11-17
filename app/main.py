"""App operatore Control Room: coda alert a pulsanti + nuova chiamata, su GLPI via API."""
import html
import logging
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import store
from .auth import MANAGER, OPERATOR, ROLES, AuthError, RateLimiter, authenticate, check_ldap_tls_config
from .config import settings
from .glpi import GlpiClient, GlpiError
from .grafana_proxy import GrafanaProxy
from .parsing import clean, parse_alert_body, parse_alert_title, parse_call

log = logging.getLogger("controlroom")
STATIC = Path(__file__).parent / "static"
limiter = RateLimiter()

def _taken(r: dict) -> bool:
    """Preso in carico = stato oltre 'Nuovo' (assegnato, pianificato, in attesa) o tecnico già assegnato."""
    return (r.get("status") or 1) > 1 or bool(r.get("tech"))


OUTCOMES = {  # esito -> (id tipo soluzione, nota obbligatoria, testo)
    "rientrato": ("sol_rientrato", False, "Allarme rientrato"),
    "manovra": ("sol_manovra", True, "Manovra eseguita"),
    "altro": ("sol_altro", True, "Altro"),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.glpi = GlpiClient()
    app.state.grafana = GrafanaProxy()  # nessuna connessione all'avvio: il portale parte anche senza Grafana
    yield
    await app.state.glpi.aclose()
    await app.state.grafana.aclose()


def glpi_reason(e: Exception) -> str:
    """Messaggio breve per l'operatore: distingue GLPI irraggiungibile da richiesta rifiutata."""
    m = str(e)
    if "non raggiungibile" in m:
        return "GLPI non raggiungibile"
    code = re.search(r"-> (\d{3})", m)
    if code and code.group(1) in ("401", "403"):
        return f"GLPI ha rifiutato la ricerca ({code.group(1)}): permessi dell'account di servizio insufficienti"
    return f"GLPI ha rifiutato la ricerca: {m[:160]}"


def glpi_write_reason(e: Exception) -> str:
    """Motivo di un rifiuto in scrittura: il messaggio di GLPI (es. ["ERROR_GLPI_ADD","Non hai diritti..."])."""
    m = str(e)
    if "non raggiungibile" in m:
        return "GLPI non raggiungibile"
    msg = re.findall(r'"([^"]{8,})"', m)
    return "GLPI: " + (msg[-1] if msg else m[:160])


def is_waiting(status) -> bool:
    """Stato GLPI 'In attesa': la ricerca può restituire il codice (4) o l'etichetta tradotta."""
    v = str(status if status is not None else "").strip().lower()
    return v == str(settings.status_waiting) or "attesa" in v or v in ("pending", "waiting")


def create_app() -> FastAPI:
    check_ldap_tls_config()
    secret = settings.session_secret
    if not secret:
        if not settings.dev_auth:
            raise RuntimeError("SESSION_SECRET obbligatorio (openssl rand -hex 32)")
        secret = secrets.token_hex(16)
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware, secret_key=secret, max_age=settings.session_hours * 3600,
        same_site="strict", https_only=settings.cookie_secure and not settings.dev_auth,
        session_cookie="cr_session",
    )
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---------- dipendenze ----------
    def glpi_dep(request: Request) -> GlpiClient:
        return request.app.state.glpi

    def session_user(request: Request) -> dict | None:
        u = request.session.get("user")
        # sessioni create prima dei ruoli (senza "roles") non valgono: nuovo login
        if not u or not isinstance(u.get("roles"), list) or not u["roles"]:
            return None
        return u

    def user_dep(request: Request) -> dict:
        u = session_user(request)
        if not u:
            raise HTTPException(401, "non autenticato")
        return u

    def require_role(role: str):
        def dep(user: dict = Depends(user_dep)) -> dict:
            if role not in user["roles"]:
                raise HTTPException(403, "ruolo non autorizzato")
            return user
        return dep

    operator_dep = require_role(OPERATOR)
    manager_dep = require_role(MANAGER)

    def home_for(user: dict) -> str:
        return "/" if OPERATOR in user["roles"] else "/stats"

    def write_guard(request: Request):
        # difesa CSRF aggiuntiva alla cookie SameSite=Strict: le scritture richiedono un header custom
        if request.headers.get("x-cr") != "1":
            raise HTTPException(403, "richiesta non valida")

    # ---------- pagine ----------
    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    def page(name: str, request: Request, role: str):
        u = session_user(request)
        if not u:
            return RedirectResponse("/login", 303)
        if role not in u["roles"]:
            # pagina di un altro ruolo: rimanda alla propria home (se è diversa), altrimenti 403
            home = home_for(u)
            if home != request.url.path:
                return RedirectResponse(home, 303)
            raise HTTPException(403, "ruolo non autorizzato")
        return FileResponse(STATIC / name, headers={"Cache-Control": "no-store"})

    @app.get("/")
    async def index(request: Request):
        return page("index.html", request, OPERATOR)

    @app.get("/call")
    async def call_page(request: Request):
        return page("call.html", request, OPERATOR)

    @app.get("/stats")
    async def stats_page(request: Request):
        return page("stats.html", request, MANAGER)

    @app.get("/grafana")
    async def grafana_root(user: dict = Depends(manager_dep)):
        return RedirectResponse("/grafana/", 307)

    # POST serve alle query dei pannelli (/api/ds/query). Il ruolo Grafana è Viewer: niente modifiche.
    @app.api_route("/grafana/{path:path}", methods=["GET", "HEAD", "POST"])
    async def grafana(path: str, request: Request, user: dict = Depends(manager_dep)):
        return await request.app.state.grafana.forward(request, path, user)

    def login_html(error: str = "") -> HTMLResponse:
        tpl = (STATIC / "login.html").read_text(encoding="utf-8")
        return HTMLResponse(tpl.replace("{{error}}", html.escape(error)), headers={"Cache-Control": "no-store"})

    @app.get("/login")
    async def login_form():
        return login_html()

    @app.post("/login")
    async def login(request: Request, username: str = Form(...), password: str = Form(...)):
        key = f"{request.client.host if request.client else '?'}|{username.lower()[:64]}"
        if limiter.blocked(key):
            return login_html("Troppi tentativi. Riprova tra qualche minuto.")
        try:
            u = authenticate(username, password)
        except AuthError as e:
            limiter.fail(key)
            store.log(username[:64], "", "login_fail", {"reason": str(e)})
            return login_html(f"Accesso negato: {e}")
        limiter.reset(key)
        request.session.clear()
        user = {"username": u.username, "name": u.display_name, "roles": [r for r in ROLES if r in u.roles]}
        request.session["user"] = user
        store.log(u.username, u.display_name, "login", {"roles": user["roles"]})
        return RedirectResponse(home_for(user), 303)

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)

    # ---------- API ----------
    @app.get("/api/me")
    async def me(user: dict = Depends(user_dep)):
        return {
            "name": user["name"], "roles": user["roles"], "refresh": settings.refresh_seconds,
            "glpi_url": settings.glpi_public_url,
        }

    async def build_queue(glpi: GlpiClient) -> list[dict]:
        """Raggruppa i ticket aperti per sito + tipo di allarme + oggetto interessato.

        Se il corpo non indica né tipo né oggetto, la descrizione fa da discriminante: allarmi diversi
        sullo stesso sito non finiscono mai nello stesso gruppo (e quindi non si chiudono insieme)."""
        rows = await glpi.open_alerts()
        groups: dict[str, dict] = {}
        for r in rows:
            s = parse_alert_title(r["title"])
            a = parse_alert_body(r.get("content", ""))
            if s:
                disc = (a.tipo, a.oggetto) if (a.tipo or a.oggetto) else ("", "", a.descrizione.lower())
                key = s.code + "|" + "|".join(disc)
            else:
                key = f"#{r['id']}"  # titolo non riconosciuto: mai raggruppato
            g = groups.setdefault(key, {
                "key": key, "kind": "alert", "code": s.code if s else f"#{r['id']}",
                "name": s.name if s else clean(r["title"])[:80], "acquedotto": s.acquedotto if s else "—",
                "tipo": a.tipo, "oggetto": a.oggetto, "descrizione": a.descrizione,
                "ids": [], "items": [], "first": r["opened"], "last": r["opened"],
            })
            g["ids"].append(r["id"])
            g["items"].append({"id": r["id"], "opened": r["opened"], "descrizione": a.descrizione,
                               "taken": _taken(r), "tech": r.get("tech", []),
                               "waiting": is_waiting(r.get("status"))})
            if r["opened"] and (not g["first"] or r["opened"] < g["first"]):
                g["first"] = r["opened"]
            if r["opened"] and (not g["last"] or r["opened"] >= g["last"]):
                g["last"] = r["opened"]
                g["descrizione"] = a.descrizione or g["descrizione"]  # la più recente
        for r in await glpi.open_calls():  # chiamate dal campo: una riga per ticket, mai raggruppate
            c = parse_call(r["title"], r.get("content", ""))
            if not c:
                continue
            descr = f"{c.descrizione} — chiamante: {c.chiamante}" if c.chiamante else c.descrizione
            groups[f"call|{r['id']}"] = {
                "key": f"call|{r['id']}", "kind": "call", "code": c.code or f"#{r['id']}",
                "name": c.sito or clean(r["title"])[:80], "acquedotto": c.acquedotto,
                "tipo": "Chiamata", "oggetto": "", "descrizione": descr,
                "ids": [r["id"]], "items": [{"id": r["id"], "opened": r["opened"], "descrizione": descr,
                                              "taken": _taken(r), "tech": r.get("tech", []),
                                              "waiting": is_waiting(r.get("status"))}],
                "first": r["opened"], "last": r["opened"],
            }
        out = sorted(groups.values(), key=lambda g: g["last"] or "", reverse=True)
        for g in out:
            g["items"].sort(key=lambda i: i["opened"] or "", reverse=True)
            g["count"] = len(g["ids"])
            g["taken"] = all(i["taken"] for i in g["items"])  # chiudibile solo se TUTTI presi in carico
            g["tech"] = sorted({t for i in g["items"] for t in i["tech"]})
            g["flapping"] = g["count"] >= settings.flapping_threshold
            g["waiting_count"] = sum(i["waiting"] for i in g["items"])
            g["waiting"] = g["waiting_count"] == g["count"]  # gruppo "in attesa" solo se lo sono tutti
        return out

    @app.get("/api/queue")
    async def queue(user: dict = Depends(operator_dep), glpi: GlpiClient = Depends(glpi_dep)):
        try:
            groups = await build_queue(glpi)
        except GlpiError as e:
            log.warning("queue: %s", e)
            return JSONResponse({"ok": False, "error": "GLPI non raggiungibile"}, status_code=502)
        return {"ok": True, "groups": groups, "total": sum(g["count"] for g in groups)}

    class ResolveIn(BaseModel):
        ticket_ids: list[int] = Field(min_length=1, max_length=200)
        outcome: str
        note: str = Field(default="", max_length=500)

    @app.post("/api/resolve", dependencies=[Depends(write_guard)])
    async def resolve(body: ResolveIn, user: dict = Depends(operator_dep), glpi: GlpiClient = Depends(glpi_dep)):
        if body.outcome not in OUTCOMES:
            raise HTTPException(422, "esito non valido")
        attr, note_required, label = OUTCOMES[body.outcome]
        note = "\n".join(x for x in (clean(r) for r in body.note.splitlines()) if x)  # a capo preservati
        if note_required and len(note) < 3:
            raise HTTPException(422, "nota obbligatoria per questo esito")
        try:  # il token di servizio può chiudere SOLO ticket attualmente nella coda alert
            items = {i["id"]: i for g in await build_queue(glpi) for i in g["items"]}
        except GlpiError:
            raise HTTPException(502, "GLPI non raggiungibile")
        req = list(dict.fromkeys(body.ticket_ids))
        not_taken = [i for i in req if i in items and not items[i]["taken"]]
        if not_taken:
            raise HTTPException(409, f"prendi prima in carico: #{', #'.join(map(str, not_taken))}")
        ids = [i for i in req if i in items]
        skipped = [i for i in req if i not in items]
        content = label + (f": {note}" if note else ".") + f"\n[Control Room app — operatore: {user['name']} ({user['username']})]"
        done, failed, error = [], [], ""
        for tid in ids:
            try:
                await glpi.add_solution(tid, getattr(settings, attr), content)
                done.append(tid)
            except GlpiError as e:
                log.warning("resolve %s: %s", tid, e)
                failed.append(tid)
                error = error or glpi_write_reason(e)
        store.log(user["username"], user["name"], "resolve",
                  {"outcome": body.outcome, "note": note, "done": done, "failed": failed, "skipped": skipped})
        return {"ok": not failed, "done": done, "failed": failed, "skipped": skipped, "error": error}

    class TakeIn(BaseModel):
        ticket_ids: list[int] = Field(min_length=1, max_length=200)

    @app.post("/api/take", dependencies=[Depends(write_guard)])
    async def take(body: TakeIn, user: dict = Depends(operator_dep), glpi: GlpiClient = Depends(glpi_dep)):
        """Presa in carico: l'operatore (utente GLPI con lo stesso login AD) diventa tecnico assegnato."""
        try:
            items = {i["id"]: i for g in await build_queue(glpi) for i in g["items"]}
            uid = await glpi.find_user_by_login(user["username"])
        except GlpiError:
            raise HTTPException(502, "GLPI non raggiungibile")
        if not uid:
            raise HTTPException(422, f"utente {user['username']} non presente o non attivo in GLPI")
        req = list(dict.fromkeys(body.ticket_ids))
        ids = [i for i in req if i in items and not items[i]["taken"]]
        skipped = [i for i in req if i not in ids]
        done, failed, error = [], [], ""
        for tid in ids:
            try:
                await glpi.take_ticket(tid, uid)
                done.append(tid)
            except GlpiError as e:
                log.warning("take %s: %s", tid, e)
                failed.append(tid)
                error = error or glpi_write_reason(e)
        store.log(user["username"], user["name"], "take", {"done": done, "failed": failed, "skipped": skipped})
        return {"ok": not failed, "done": done, "failed": failed, "skipped": skipped, "error": error}

    async def queue_ids(glpi: GlpiClient) -> set[int]:
        try:
            return {i for g in await build_queue(glpi) for i in g["ids"]}
        except GlpiError:
            raise HTTPException(502, "GLPI non raggiungibile")

    class ProgressIn(BaseModel):
        ticket_ids: list[int] = Field(min_length=1, max_length=200)
        note: str = Field(default="", max_length=1000)
        wait: str = "keep"  # "set" = metti in attesa, "resume" = togli dall'attesa, "keep" = stato invariato

    @app.post("/api/progress", dependencies=[Depends(write_guard)])
    async def progress(body: ProgressIn, user: dict = Depends(operator_dep), glpi: GlpiClient = Depends(glpi_dep)):
        """Manovra che NON chiude il ticket: aggiunge un avanzamento (followup GLPI) e opzionalmente
        mette o toglie il ticket dallo stato 'In attesa'. Il ticket resta nella coda."""
        if body.wait not in ("keep", "set", "resume"):
            raise HTTPException(422, "azione attesa non valida")
        note = "\n".join(x for x in (clean(r) for r in body.note.splitlines()) if x)
        if len(note) < 3:
            raise HTTPException(422, "nota obbligatoria")
        try:
            items = {i["id"]: i for g in await build_queue(glpi) for i in g["items"]}
        except GlpiError:
            raise HTTPException(502, "GLPI non raggiungibile")
        req = list(dict.fromkeys(body.ticket_ids))
        not_taken = [i for i in req if i in items and not items[i]["taken"]]
        if not_taken:  # come per la chiusura: prima la presa in carico (chi), poi le manovre (cosa)
            raise HTTPException(409, f"prendi prima in carico: #{', #'.join(map(str, not_taken))}")
        ids = [i for i in req if i in items]
        skipped = [i for i in req if i not in items]
        tag = {"set": " [messo in attesa]", "resume": " [ripreso dall'attesa]", "keep": ""}[body.wait]
        content = f"Manovra/avanzamento{tag}: {note}\n[Control Room app — operatore: {user['name']} ({user['username']})]"
        done, failed, error = [], [], ""
        for tid in ids:
            try:
                await glpi.add_followup(tid, content)
                if body.wait == "set":
                    await glpi.set_status(tid, settings.status_waiting)
                elif body.wait == "resume":
                    await glpi.set_status(tid, settings.status_resume)
                done.append(tid)
            except GlpiError as e:
                log.warning("progress %s: %s", tid, e)
                failed.append(tid)
                error = error or glpi_write_reason(e)
        store.log(user["username"], user["name"], "progress",
                  {"wait": body.wait, "note": note, "done": done, "failed": failed, "skipped": skipped})
        return {"ok": not failed, "done": done, "failed": failed, "skipped": skipped, "error": error}

    @app.get("/api/history")
    async def history(ids: str, user: dict = Depends(operator_dep), glpi: GlpiClient = Depends(glpi_dep)):
        """Storico avanzamenti dei ticket in coda (max 20), dal più recente."""
        want = [int(x) for x in dict.fromkeys(re.findall(r"\d+", ids))][:20]
        allowed = await queue_ids(glpi)
        rows = []
        try:
            for tid in (i for i in want if i in allowed):
                for f in await glpi.followups(tid):
                    text = html.unescape(html.unescape(f["content"]))
                    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
                    text = re.sub(r"<[^>]+>", "", text).strip()
                    rows.append({"ticket": tid, "id": f["id"], "date": f["date"], "text": text})
        except GlpiError as e:
            log.warning("history: %s", e)
            raise HTTPException(502, "GLPI non raggiungibile")
        rows.sort(key=lambda r: (r["date"] or "", r["id"]), reverse=True)
        return {"rows": rows}

    @app.get("/api/sites")
    async def sites(q: str = "", user: dict = Depends(operator_dep), glpi: GlpiClient = Depends(glpi_dep)):
        q = clean(q)[:60]
        try:
            return {"sites": await glpi.search_locations(q)}
        except GlpiError as e:
            log.warning("search_locations: %s", e)
            return JSONResponse({"sites": [], "error": glpi_reason(e)}, status_code=502)

    @app.get("/api/users")
    async def users(q: str = "", user: dict = Depends(operator_dep), glpi: GlpiClient = Depends(glpi_dep)):
        q = clean(q)[:60]
        try:
            return {"users": await glpi.search_users(q)}
        except GlpiError as e:
            log.warning("search_users: %s", e)
            return JSONResponse({"users": [], "error": glpi_reason(e)}, status_code=502)

    class CallIn(BaseModel):
        location_id: int | None = None
        location_name: str = Field(default="", max_length=200)
        caller_id: int | None = None
        note: str = Field(min_length=3, max_length=1500)

    @app.post("/api/call", dependencies=[Depends(write_guard)])
    async def new_call(body: CallIn, user: dict = Depends(operator_dep), glpi: GlpiClient = Depends(glpi_dep)):
        loc = clean(body.location_name)
        if not body.location_id or not loc:
            raise HTTPException(422, "sito obbligatorio")
        m = re.search(r"[A-Z]{3}-[A-Z0-9]+", loc.upper())
        site_tag = m.group(0) if m else loc[:50]
        note = "\n".join(x for x in (clean(r) for r in body.note.splitlines()) if x)  # a capo preservati
        if len(note) < 3:
            raise HTTPException(422, "descrizione obbligatoria")
        if not body.caller_id:
            raise HTTPException(422, "chiamante obbligatorio")
        first = note.splitlines()[0][:60]
        title = f"CHIAMATA: {site_tag} – {first}"
        try:
            caller = await glpi.get_user(body.caller_id)  # nome dal GLPI, non dal client
            if not caller:
                raise HTTPException(422, "chiamante non trovato in GLPI")
            lines = [f"Sito: {loc}", f"Chiamante: {caller['name']}", "", note,
                     "", f"[Control Room app — operatore: {user['name']} ({user['username']})]"]
            tid = await glpi.create_call(title, "\n".join(lines), body.location_id, requester_id=caller["id"])
        except GlpiError as e:
            log.warning("create_call: %s", e)
            store.log(user["username"], user["name"], "call_fail", {"title": title})
            raise HTTPException(502, "GLPI non raggiungibile: ticket NON creato")
        store.log(user["username"], user["name"], "call", {"ticket": tid, "title": title})
        return {"ok": True, "ticket_id": tid, "title": title}

    @app.get("/api/audit")
    async def audit(user: dict = Depends(user_dep)):
        return {"rows": store.recent(30)}

    return app


app = create_app() if (settings.session_secret or settings.dev_auth) else None
