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


def test_parse_body():
    from app.parsing import parse_alert_body
    a = parse_alert_body("&amp;lt;p&amp;gt;Tipo allarme: Blocco pompa&amp;lt;br /&amp;gt;Oggetto: Pompa P2"
                         "&amp;lt;br&amp;gt;Descrizione: Intervento termico&amp;lt;/p&amp;gt;")
    assert (a.tipo, a.oggetto, a.descrizione) == ("Blocco pompa", "Pompa P2", "Intervento termico")
    b = parse_alert_body("ALERT ON: X\n\nMancanza rete ENEL\naltro")
    assert (b.tipo, b.oggetto, b.descrizione) == ("", "", "Mancanza rete ENEL")
    assert parse_alert_body("").descrizione == ""


def test_queue_grouping(client):
    login(client)
    mock_glpi.add_ticket("ALERT ON: FNC-IS00400-Campanella", "2026-11-17 09:10:00",
                         content="Tipo allarme: Livello\nOggetto: Vasca V1\nDescrizione: Livello minimo vasca V1")
    q = client.get("/api/queue").json()
    camp = [g for g in q["groups"] if g["code"] == "FNC-IS00400"]
    by = {g["oggetto"]: g for g in camp}  # stesso sito, oggetti/tipi diversi -> gruppi distinti
    assert set(by) == {"Pompa P2", "Vasca V1"}
    p = by["Pompa P2"]
    assert p["count"] == 3 and p["flapping"] is True and p["tipo"] == "Blocco pompa"
    assert p["descrizione"] == "Intervento termico pompa P2" and len(p["items"]) == 3
    assert p["items"][0]["opened"] >= p["items"][-1]["opened"]
    assert by["Vasca V1"]["count"] == 1 and by["Vasca V1"]["tipo"] == "Livello"
    sal = next(g for g in q["groups"] if g["code"] == "ANC-PA02100")
    assert sal["count"] == 1 and sal["flapping"] is False and sal["descrizione"] == "Mancanza rete ENEL"
    odd = [g for g in q["groups"] if g["code"].startswith("#")]  # titolo non riconosciuto: visibile, mai raggruppato
    assert len(odd) == 1 and odd[0]["count"] == 1 and "Titolo strano" in odd[0]["name"]
    assert not any("Baronessa" in g["name"] for g in q["groups"])  # ALERT OFF escluso
    assert q["total"] == 7
    assert len({g["key"] for g in q["groups"]}) == len(q["groups"])
    assert q["groups"][0]["code"] == "ANC-PA02100"  # ordinato per ultimo alert


def test_resolve_rientrato_closes_whole_group(client):
    login(client)
    mock_glpi.add_ticket("ALERT ON: FNC-IS00400-Campanella", "2026-11-17 09:10:00",
                         content="Tipo allarme: Livello\nOggetto: Vasca V1")
    ids = next(g["ids"] for g in client.get("/api/queue").json()["groups"] if g["oggetto"] == "Pompa P2")
    r = client.post("/api/resolve", json={"ticket_ids": ids, "outcome": "rientrato"}, headers=H).json()
    assert r["ok"] and sorted(r["done"]) == sorted(ids)
    st = client.get("/_noop").status_code if False else mock_glpi.STATE
    assert all(st["tickets"][i]["status"] == 5 for i in ids)
    sol = st["solutions"][0]
    assert sol["solutiontypes_id"] == 1 and "mrossi" in sol["content"] and sol["itemtype"] == "Ticket"
    assert st["entity"] == 14
    left = [g for g in client.get("/api/queue").json()["groups"] if g["code"] == "FNC-IS00400"]
    assert [g["oggetto"] for g in left] == ["Vasca V1"]  # chiuso solo il gruppo, non l'altro allarme del sito


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
    users = client.get("/api/users?q=bianc").json()["users"]
    assert users == [{"id": 7, "name": "Bianchi Mario (mbianchi)"}]
    assert client.get("/api/users?q=neri").json()["users"] == []  # disattivato
    assert len(client.get("/api/users").json()["users"]) == 2  # tendina aperta senza testo: tutti gli attivi
    assert len(client.get("/api/sites").json()["sites"]) == 3
    body = {"location_id": sites[0]["id"], "location_name": sites[0]["name"], "caller_id": 7,
            "note": "Porta forzata\nlucchetto rotto"}
    r = client.post("/api/call", json=body, headers=H).json()
    assert r["title"] == "CHIAMATA: FNC-IS00600 – Porta forzata"
    t = mock_glpi.STATE["tickets"][r["ticket_id"]]
    inp = t["input"]
    assert inp["requesttypes_id"] == 8 and inp["locations_id"] == 2 and inp["entities_id"] == 14
    assert inp["_users_id_requester"] == 7
    assert "Bianchi Mario" in t["content"] and "lucchetto" in t["content"] and "mrossi" in t["content"]
    assert client.post("/api/call", json={**body, "location_id": None}, headers=H).status_code == 422
    assert client.post("/api/call", json={**body, "caller_id": None}, headers=H).status_code == 422
    assert client.post("/api/call", json={**body, "caller_id": 9}, headers=H).status_code == 422
    assert client.post("/api/call", json={**body, "caller_id": 999}, headers=H).status_code == 422
    assert client.post("/api/call", json={**body, "note": "  "}, headers=H).status_code == 422


def test_open_calls_in_queue_and_closable(client):
    login(client)
    site = client.get("/api/sites?q=casaleno").json()["sites"][0]
    body = {"location_id": site["id"], "location_name": site["name"], "caller_id": 7, "note": "Porta forzata"}
    tid = client.post("/api/call", json=body, headers=H).json()["ticket_id"]
    q = client.get("/api/queue").json()
    calls = [g for g in q["groups"] if g.get("kind") == "call"]
    assert [g["ids"] for g in calls] == [[tid]]
    c = calls[0]
    assert c["code"] == "FNC-IS00600" and c["acquedotto"] == "FNC" and c["tipo"] == "Chiamata"
    assert "Porta forzata" in c["descrizione"] and "Bianchi Mario" in c["descrizione"]
    assert "Control Room app" not in c["descrizione"]
    r = client.post("/api/resolve", json={"ticket_ids": [tid], "outcome": "altro", "note": "Verificato sul posto"},
                    headers=H).json()
    assert r["done"] == [tid]
    assert not any(g.get("kind") == "call" for g in client.get("/api/queue").json()["groups"])


def test_parse_call_without_site_code():
    from app.parsing import parse_call
    c = parse_call("CHIAMATA: Potabilizzatori – perdita", "Sito: Potabilizzatori\nChiamante: X (x)\n\nperdita")
    assert c.code == "" and c.acquedotto == "—" and c.sito == "Potabilizzatori" and c.descrizione == "perdita"
    assert parse_call("ALERT ON: ANC-PO00100 x", "") is None


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
    body = {"location_id": 2, "location_name": "x FNC-IS00600", "caller_id": 7, "note": "test"}
    assert client.post("/api/call", json=body, headers=H).status_code == 502
