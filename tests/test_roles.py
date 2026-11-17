"""Ruoli operatore/manager: permessi sulle route e derivazione dai gruppi AD (LDAP simulato)."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.glpi import GlpiClient
from app.main import create_app, limiter
from mock import mock_glpi

H = {"X-CR": "1"}
OP_DN = "CN=ControlRoom-Operatori,OU=Gruppi,DC=siciliacque,DC=local"
MG_DN = "CN=ControlRoom-Manager,OU=Gruppi,DC=siciliacque,DC=local"
WRITES = [
    ("/api/resolve", {"ticket_ids": [100], "outcome": "rientrato"}),
    ("/api/call", {"location_id": 2, "location_name": "FNC-IS00600 Casaleno", "kind": "altro", "caller": "Mario"}),
]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "dev_roles", "capo:manager,both:operator+manager,nessuno:")
    limiter._fails.clear()
    mock_glpi.seed()
    app = create_app()
    with TestClient(app) as c:
        app.state.glpi = GlpiClient(transport=httpx.ASGITransport(app=mock_glpi.app))
        yield c


def login(c, user):
    return c.post("/login", data={"username": user, "password": "dev"}, follow_redirects=False)


# ---------- manager: sola lettura ----------

@pytest.mark.parametrize("path,body", WRITES)
def test_manager_gets_403_on_writes(client, path, body):
    assert login(client, "capo").headers["location"] == "/stats"
    n_tickets, n_sol = len(mock_glpi.STATE["tickets"]), len(mock_glpi.STATE["solutions"])
    assert client.post(path, json=body, headers=H).status_code == 403
    assert client.post(path, json={}, headers=H).status_code == 403  # 403 prima della validazione del body
    assert (len(mock_glpi.STATE["tickets"]), len(mock_glpi.STATE["solutions"])) == (n_tickets, n_sol)


def test_manager_views(client):
    login(client, "capo")
    assert client.get("/stats").status_code == 200
    assert client.get("/grafana/d/x").status_code == 503  # ruolo ok, proxy non ancora presente (PR 2)
    assert client.get("/api/me").json()["roles"] == ["manager"]
    assert client.get("/api/queue").status_code == 403
    assert client.get("/api/sites?q=casa").status_code == 403
    r = client.get("/", follow_redirects=False)  # pagina operatore -> rimandato alle statistiche
    assert r.status_code == 303 and r.headers["location"] == "/stats"


# ---------- operatore: niente statistiche ----------

def test_operator_cannot_access_grafana_or_stats(client):
    assert login(client, "mrossi").headers["location"] == "/"
    for m in ("get", "post"):
        assert getattr(client, m)("/grafana/").status_code == 403
        assert getattr(client, m)("/grafana/api/dashboards/db", headers=H).status_code == 403
    r = client.get("/stats", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/api/queue").status_code == 200


def test_both_roles_get_both_views(client):
    assert login(client, "both").headers["location"] == "/"
    assert client.get("/api/me").json()["roles"] == ["operator", "manager"]
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/stats", follow_redirects=False).status_code == 200
    assert client.get("/grafana/").status_code == 503
    assert client.post("/api/resolve", json=WRITES[0][1], headers=H).status_code == 200


def test_no_role_rejected(client):
    r = client.post("/login", data={"username": "nessuno", "password": "dev"})
    assert "non abilitato" in r.text
    assert client.get("/api/me").status_code == 401


def test_unauthenticated_grafana_is_401(client):
    assert client.get("/grafana/").status_code == 401


def test_legacy_session_without_roles_is_rejected(client):
    """Un cookie emesso prima dei ruoli non deve valere come operatore."""
    from starlette.requests import Request
    app = client.app

    @app.get("/_test/legacy")
    async def legacy(request: Request):
        request.session["user"] = {"username": "old", "name": "Old"}
        return {}

    client.get("/_test/legacy")
    assert client.get("/api/queue").status_code == 401
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


# ---------- derivazione ruoli da AD ----------

class FakeConn:
    """Simula un DC: membership transitiva (nested) e memberOf diretto."""

    def __init__(self, direct, transitive):
        self.direct, self.transitive, self.filters, self.entries = direct, transitive, [], []

    def bind(self):
        return True

    def unbind(self):
        pass

    def search(self, base, flt, attributes=None):
        self.filters.append(flt)
        if auth.IN_CHAIN in flt:
            self.entries = [object()] if any(f"={g})" in flt for g in self.transitive) else []
        else:
            self.entries = [FakeEntry(self.direct)]


class FakeAttr:
    def __init__(self, v):
        self.values, self.value = v, v


class FakeEntry:
    def __init__(self, groups):
        self.memberOf, self.displayName = FakeAttr(groups), FakeAttr("Mario Rossi")

    def __contains__(self, k):
        return k in ("memberOf", "displayName")


@pytest.fixture()
def ldap(monkeypatch):
    monkeypatch.setattr(settings, "dev_auth", False)
    monkeypatch.setattr(settings, "ldap_operator_group_dn", OP_DN)
    monkeypatch.setattr(settings, "ldap_manager_group_dn", MG_DN)

    def use(direct, transitive, nested=True):
        monkeypatch.setattr(settings, "ldap_nested_groups", nested)
        conn = FakeConn(direct, transitive)
        monkeypatch.setattr(auth, "Connection", lambda *a, **k: conn)
        return conn
    return use


def test_nested_group_membership_grants_role(ldap):
    # mrossi è in "Turnisti", a sua volta membro di ControlRoom-Operatori: solo IN_CHAIN lo vede
    conn = ldap(direct=["CN=Turnisti,OU=Gruppi,DC=siciliacque,DC=local"], transitive=[OP_DN])
    u = auth.authenticate("SICILIACQUE\\mrossi", "pw")
    assert u.roles == ["operator"] and u.username == "mrossi"
    chain = [f for f in conn.filters if auth.IN_CHAIN in f]
    assert len(chain) == 2 and all("(sAMAccountName=mrossi)" in f for f in chain)


def test_nested_both_roles(ldap):
    ldap(direct=[], transitive=[OP_DN, MG_DN])
    assert auth.authenticate("capo", "pw").roles == ["operator", "manager"]


def test_direct_mode_ignores_nested_and_is_case_insensitive(ldap):
    ldap(direct=["CN=Turnisti,OU=Gruppi,DC=siciliacque,DC=local"], transitive=[OP_DN], nested=False)
    with pytest.raises(auth.AuthError):
        auth.authenticate("mrossi", "pw")
    ldap(direct=[MG_DN.lower()], transitive=[], nested=False)
    assert auth.authenticate("capo", "pw").roles == ["manager"]


def test_no_group_means_no_access(ldap):
    ldap(direct=["CN=Altri,DC=siciliacque,DC=local"], transitive=[])
    with pytest.raises(auth.AuthError, match="non abilitato"):
        auth.authenticate("estraneo", "pw")


def test_unconfigured_role_is_never_granted(ldap, monkeypatch):
    ldap(direct=[], transitive=[OP_DN, MG_DN])
    monkeypatch.setattr(settings, "ldap_manager_group_dn", "")
    assert auth.authenticate("capo", "pw").roles == ["operator"]


def test_group_dn_is_escaped_in_filter(ldap, monkeypatch):
    monkeypatch.setattr(settings, "ldap_operator_group_dn", "CN=Op (CR)*,DC=x")
    conn = ldap(direct=[], transitive=[])
    with pytest.raises(auth.AuthError):
        auth.authenticate("mrossi", "pw")
    assert any("CN=Op \\28CR\\29\\2a,DC=x" in f for f in conn.filters)
