"""GLPI finto (solo per sviluppo/test): implementa gli endpoint usati dall'app e dal worker di sync.

Avvio:  uvicorn mock.mock_glpi:app --port 9000
Poi:    GLPI_URL=http://localhost:9000 GLPI_APP_TOKEN=a GLPI_USER_TOKEN=u DEV_AUTH=1 ...

Riproduce (secondo la documentazione dell'API legacy, NON verificato sul GLPI reale):
- /search/Ticket con criteria[i][field|searchtype|value|link], forcedisplay, sort/order, range "a-b", totalcount;
- campi: 1 titolo, 2 id, 9 origine, 12 stato, 15 apertura, 16 chiusura, 17 risoluzione, 19 ultima modifica,
  23 tipo soluzione, 24 testo soluzione, 80 entità, 83 posizione. Date in ora locale Europe/Rome senza fuso;
- ticket nel cestino (is_deleted) esclusi dalla ricerca, come in GLPI.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI()
ROME = ZoneInfo("Europe/Rome")
STATE: dict = {}
SOLUTION_TYPES = {1: "Allarme rientrato", 2: "Manovra eseguita", 3: "Altro"}
REQUEST_TYPES = {1: "E-Mail", 8: "Telefono"}
LOCATIONS = [
    (1, "SUD Blufi > BLF-SAP00200 Serbatoio Cozzo della Guardia"),
    (2, "CENTRO Fanaco > FNC-IS00600 Casaleno"),
    (3, "CENTRO Fanaco > FNC-IS00400 Campanella"),
]
FIELDS = {  # search option -> chiave del ticket simulato
    1: "name", 2: "id", 9: "requesttype", 12: "status", 15: "date", 16: "closedate", 17: "solvedate",
    19: "date_mod", 23: "solutiontype", 24: "solution", 80: "entity", 83: "location",
}


def now_local() -> str:
    return datetime.now(ROME).strftime("%Y-%m-%d %H:%M:%S")


def add_ticket(name: str, date: str, **kw) -> int:
    """Crea un ticket simulato (usato da seed e dai test). kw: status, date_mod, solvedate, closedate, ..."""
    tid = kw.pop("id", None) or STATE["next"]
    STATE["next"] = max(STATE["next"], tid + 1)
    t = {"id": tid, "name": name, "status": 1, "date": date, "date_mod": date, "solvedate": None,
         "closedate": None, "solutiontype": None, "solution": None, "requesttype": "E-Mail",
         "entity": "13) Control Room Centralizzata", "entity_id": 14, "location": None, "is_deleted": False,
         "content": ""}
    t.update(kw)
    STATE["tickets"][tid] = t
    return tid


def seed():
    STATE.clear()
    STATE.update(next=100, tickets={}, solutions=[], sessions=set(), entity=None, fail=False)
    rows = [
        ("ALERT ON: FNC-IS00400-Campanella", "2026-11-17 08:01:00"),
        ("ALERT ON: FNC-IS00400-Campanella", "2026-11-17 08:20:00"),
        ("ALERT ON: FNC-IS00400-Campanella", "2026-11-17 09:05:00"),
        ("ALERT ON: ANC-PA02100-Salvatorello 2", "2026-11-17 09:30:00"),
        ("ALERT ON: MOE-PA00500-Roccapalumba", "2026-11-17 07:40:00"),
        ("ALERT OFF: ANC-PA01800-Baronessa", "2026-11-17 07:00:00"),  # non deve entrare in coda
        ("Titolo strano senza codice ALERT ON", "2026-11-17 06:00:00"),
    ]
    for title, opened in rows:
        add_ticket(title, opened)


seed()


def auth(app_token: str | None, session: str | None):
    if STATE.get("fail"):
        raise HTTPException(503, "GLPI in manutenzione")
    if app_token != "a" or session not in STATE["sessions"]:
        raise HTTPException(401, "ERROR_SESSION_TOKEN_INVALID")


@app.get("/apirest.php/initSession")
async def init(request: Request, authorization: str = Header(""), app_token: str = Header("")):
    if STATE.get("fail"):
        raise HTTPException(503, "GLPI in manutenzione")
    if app_token != "a" or authorization != "user_token u":
        raise HTTPException(401, "bad creds")
    tok = f"sess{len(STATE['sessions']) + 1}"
    STATE["sessions"].add(tok)
    return {"session_token": tok}


@app.post("/apirest.php/changeActiveEntities")
async def entities(request: Request, app_token: str = Header(""), session_token: str = Header("")):
    auth(app_token, session_token)
    STATE["entity"] = (await request.json())["entities_id"]
    return True


@app.get("/apirest.php/getFullSession")
async def full(app_token: str = Header(""), session_token: str = Header("")):
    auth(app_token, session_token)
    return {"session": {}}


def _criteria(q) -> list[dict]:
    out, i = [], 0
    while f"criteria[{i}][field]" in q:
        out.append({"field": int(q[f"criteria[{i}][field]"]), "type": q.get(f"criteria[{i}][searchtype]", ""),
                    "value": q.get(f"criteria[{i}][value]", "")})
        i += 1
    return out


def _match(t: dict, c: dict) -> bool:
    f, typ, val = c["field"], c["type"], c["value"]
    if f == 80:
        return typ == "equals" and t["entity_id"] == int(val)
    if f == 12:
        return t["status"] < 5 if val == "notold" else t["status"] == int(val)
    if f == 1 and typ == "contains":
        return val.lower() in t["name"].lower()
    if f == 19 and typ == "morethan":
        return bool(t["date_mod"]) and t["date_mod"] > val
    raise HTTPException(400, f"criterio non simulato: {c}")


@app.get("/apirest.php/search/Ticket")
async def search_ticket(request: Request, app_token: str = Header(""), session_token: str = Header("")):
    auth(app_token, session_token)
    q = request.query_params
    crit = _criteria(q)
    rows = [t for t in STATE["tickets"].values() if not t["is_deleted"] and all(_match(t, c) for c in crit)]
    sort = FIELDS.get(int(q.get("sort", 2)), "id")
    rows.sort(key=lambda t: (t[sort] or "", t["id"]) if sort != "id" else t["id"], reverse=q.get("order") == "DESC")
    a, b = (int(x) for x in q.get("range", "0-999").split("-"))
    page = rows[a:b + 1]
    shown = sorted({2, *(int(v) for k, v in q.items() if k.startswith("forcedisplay["))})
    data = [{str(f): t[FIELDS[f]] for f in shown} for t in page]
    return {"totalcount": len(rows), "count": len(data), "data": data}


@app.get("/apirest.php/search/Location")
async def search_loc(request: Request, app_token: str = Header(""), session_token: str = Header("")):
    auth(app_token, session_token)
    text = request.query_params.get("criteria[0][value]", "").lower()
    data = [{"2": i, "1": n} for i, n in LOCATIONS if text in n.lower()]
    return {"totalcount": len(data), "count": len(data), "data": data}


@app.post("/apirest.php/ITILSolution")
async def solution(request: Request, app_token: str = Header(""), session_token: str = Header("")):
    auth(app_token, session_token)
    inp = (await request.json())["input"]
    t = STATE["tickets"].get(inp["items_id"])
    if not t:
        raise HTTPException(400, "ERROR_GLPI_ADD")
    ts = now_local()
    t.update(status=5, solvedate=ts, date_mod=ts, solutiontype=SOLUTION_TYPES.get(inp["solutiontypes_id"]),
             solution=inp["content"])
    STATE["solutions"].append(inp)
    return {"id": len(STATE["solutions"])}


@app.post("/apirest.php/Ticket")
async def create(request: Request, app_token: str = Header(""), session_token: str = Header("")):
    auth(app_token, session_token)
    inp = (await request.json())["input"]
    loc = dict(LOCATIONS).get(inp.get("locations_id"))
    tid = add_ticket(inp["name"], now_local(), content=inp["content"], input=inp, location=loc,
                     requesttype=REQUEST_TYPES.get(inp.get("requesttypes_id"), "E-Mail"))
    return {"id": tid}


@app.post("/_test/reset")
async def reset():
    seed()
    return {"ok": True}


@app.get("/_test/state")
async def state():
    return {"tickets": STATE["tickets"], "solutions": STATE["solutions"], "entity": STATE["entity"]}
