"""GLPI finto (solo per sviluppo/test): implementa gli endpoint usati dall'app.

Avvio:  uvicorn mock.mock_glpi:app --port 9000
Poi:    GLPI_URL=http://localhost:9000 GLPI_APP_TOKEN=a GLPI_USER_TOKEN=u DEV_AUTH=1 ...
"""
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI()
STATE = {"next": 100, "tickets": {}, "solutions": [], "sessions": set(), "entity": None}


def seed():
    STATE.update(next=100, tickets={}, solutions=[], sessions=set(), entity=None)
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
        tid = STATE["next"]; STATE["next"] += 1
        STATE["tickets"][tid] = {"id": tid, "name": title, "status": 1, "date": opened, "content": ""}


seed()
LOCATIONS = [
    (1, "SUD Blufi - BLF-SAP00200 Serbatoio Cozzo della Guardia"),
    (2, "CENTRO Fanaco - FNC-IS00600 Casaleno"),
    (3, "CENTRO Fanaco - FNC-IS00400 Campanella"),
]


def auth(app_token: str | None, session: str | None):
    if app_token != "a" or session not in STATE["sessions"]:
        raise HTTPException(401, "ERROR_SESSION_TOKEN_INVALID")


@app.get("/apirest.php/initSession")
async def init(request: Request, authorization: str = Header(""), app_token: str = Header("")):
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


@app.get("/apirest.php/search/Ticket")
async def search_ticket(request: Request, app_token: str = Header(""), session_token: str = Header("")):
    auth(app_token, session_token)
    q = request.query_params
    contains = q.get("criteria[2][value]", "")
    data = [
        {"2": t["id"], "1": t["name"], "12": t["status"], "15": t["date"]}
        for t in STATE["tickets"].values()
        if t["status"] < 5 and contains.lower() in t["name"].lower()
    ]
    return {"totalcount": len(data), "count": len(data), "data": data}


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
    t["status"] = 5
    STATE["solutions"].append(inp)
    return {"id": len(STATE["solutions"])}


@app.post("/apirest.php/Ticket")
async def create(request: Request, app_token: str = Header(""), session_token: str = Header("")):
    auth(app_token, session_token)
    inp = (await request.json())["input"]
    tid = STATE["next"]; STATE["next"] += 1
    STATE["tickets"][tid] = {"id": tid, "name": inp["name"], "status": 1, "content": inp["content"],
                             "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "input": inp}
    return {"id": tid}


@app.post("/_test/reset")
async def reset():
    seed()
    return {"ok": True}


@app.get("/_test/state")
async def state():
    return {"tickets": STATE["tickets"], "solutions": STATE["solutions"], "entity": STATE["entity"]}
