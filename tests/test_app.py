import os
import tempfile

os.environ.update(DEV_AUTH="1", GLPI_APP_TOKEN="a", GLPI_USER_TOKEN="u",
                  DB_PATH=os.path.join(tempfile.mkdtemp(), "audit.db"))

import httpx
import pytest
from fastapi.testclient import TestClient

from app.glpi import GlpiClient
from app.main import create_app
from app.parsing import parse_alert_title
from mock import mock_glpi

H = {"X-CR": "1"}


def test_parse_title():
    s = parse_alert_title("ALERT ON: FNC-IS00600-Casaleno")
    assert (s.acquedotto, s.code, s.name) == ("FNC", "FNC-IS00600", "Casaleno")
    assert parse_alert_title("ALERT ON: ANC-PA02100-Salvatorello 2").name == "Salvatorello 2"
    assert parse_alert_title("ALERT OFF: ANC-PA01800-Baronessa") is None
    assert parse_alert_title("Titolo strano") is None


@pytest.fixture()
def client():
    mock_glpi.seed()
    app = create_app()
    with TestClient(app) as c:
        app.state.glpi = GlpiClient(transport=httpx.ASGITransport(app=mock_glpi.app))
        yield c


def login(c):
    r = c.post("/login", data={"username": "mrossi", "password": "dev"}, follow_redirects=False)
    assert r.status_code == 303


def test_auth_required(client):
    assert client.get("/api/queue").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303
    r = client.post("/login", data={"username": "x", "password": "sbagliata"})
    assert "Accesso negato" in r.text


def test_login_rate_limit(client):
    from app.main import limiter
    limiter._fails.clear()
    for _ in range(5):
        client.post("/login", data={"username": "zz", "password": "no"})
    r = client.post("/login", data={"username": "zz", "password": "dev"})
    assert "Troppi tentativi" in r.text
    limiter._fails.clear()


def test_queue_grouping(client):
    login(client)
    q = client.get("/api/queue").json()
    by = {g["code"]: g for g in q["groups"]}
    assert by["FNC-IS00400"]["count"] == 3 and by["FNC-IS00400"]["flapping"] is True
    assert by["ANC-PA02100"]["count"] == 1 and by["ANC-PA02100"]["flapping"] is False
    odd = [g for g in q["groups"] if g["code"].startswith("#")]  # titolo non riconosciuto: visibile, mai raggruppato
    assert len(odd) == 1 and odd[0]["count"] == 1 and "Titolo strano" in odd[0]["name"]
    assert not any("Baronessa" in g["name"] for g in q["groups"])  # ALERT OFF escluso
    assert q["total"] == 6
    assert q["groups"][0]["code"] == "ANC-PA02100"  # ordinato per ultimo alert


def test_resolve_rientrato_closes_whole_group(client):
    login(client)
    ids = next(g["ids"] for g in client.get("/api/queue").json()["groups"] if g["code"] == "FNC-IS00400")
    r = client.post("/api/resolve", json={"ticket_ids": ids, "outcome": "rientrato"}, headers=H).json()
    assert r["ok"] and sorted(r["done"]) == sorted(ids)
    st = client.get("/_noop").status_code if False else mock_glpi.STATE
    assert all(st["tickets"][i]["status"] == 5 for i in ids)
    sol = st["solutions"][0]
    assert sol["solutiontypes_id"] == 1 and "mrossi" in sol["content"] and sol["itemtype"] == "Ticket"
    assert st["entity"] == 14
    assert "FNC-IS00400" not in [g["code"] for g in client.get("/api/queue").json()["groups"]]


def test_manovra_requires_note_and_uses_type_2(client):
    login(client)
    tid = next(g["ids"] for g in client.get("/api/queue").json()["groups"] if g["code"] == "MOE-PA00500")
    assert client.post("/api/resolve", json={"ticket_ids": tid, "outcome": "manovra", "note": " "}, headers=H).status_code == 422
    r = client.post("/api/resolve", json={"ticket_ids": tid, "outcome": "manovra", "note": "Riarmato interruttore Q3"}, headers=H).json()
    assert r["ok"]
    sol = mock_glpi.STATE["solutions"][-1]
    assert sol["solutiontypes_id"] == 2 and "Riarmato interruttore Q3" in sol["content"]


def test_cannot_resolve_ticket_outside_queue(client):
    login(client)
    off_id = next(t["id"] for t in mock_glpi.STATE["tickets"].values() if t["name"].startswith("ALERT OFF"))
    r = client.post("/api/resolve", json={"ticket_ids": [off_id, 99999], "outcome": "rientrato"}, headers=H).json()
    assert r["done"] == [] and sorted(r["skipped"]) == sorted([off_id, 99999])
    assert mock_glpi.STATE["tickets"][off_id]["status"] == 1


def test_csrf_header_required(client):
    login(client)
    r = client.post("/api/resolve", json={"ticket_ids": [100], "outcome": "rientrato"})
    assert r.status_code == 403


def test_new_call(client):
    login(client)
    sites = client.get("/api/sites?q=casaleno").json()["sites"]
    assert sites and "FNC-IS00600" in sites[0]["name"]
    body = {"location_id": sites[0]["id"], "location_name": sites[0]["name"], "kind": "intrusione",
            "caller": "Mario Bianchi", "phone": "3331112222", "note": "porta forzata"}
    r = client.post("/api/call", json=body, headers=H).json()
    assert r["title"] == "CHIAMATA: FNC-IS00600 – Intrusione / sicurezza"
    t = mock_glpi.STATE["tickets"][r["ticket_id"]]
    inp = t["input"]
    assert inp["requesttypes_id"] == 8 and inp["locations_id"] == 2 and inp["entities_id"] == 14 and inp["urgency"] == 4
    assert "Mario Bianchi" in t["content"] and "mrossi" in t["content"]
    assert client.post("/api/call", json={**body, "location_id": None}, headers=H).status_code == 422
    assert client.post("/api/call", json={**body, "kind": "boh"}, headers=H).status_code == 422


def test_glpi_session_expiry_recovers(client):
    login(client)
    client.get("/api/queue")
    mock_glpi.STATE["sessions"].clear()  # GLPI invalida la sessione
    assert client.get("/api/queue").status_code == 200


def test_glpi_down_gives_502_not_crash(client):
    login(client)

    def boom(request):
        raise httpx.ConnectError("down")

    client.app.state.glpi = GlpiClient(transport=httpx.MockTransport(boom))
    r = client.get("/api/queue")
    assert r.status_code == 502 and r.json()["ok"] is False
    body = {"location_id": 2, "location_name": "x FNC-IS00600", "kind": "altro", "caller": "Mario"}
    assert client.post("/api/call", json=body, headers=H).status_code == 502
