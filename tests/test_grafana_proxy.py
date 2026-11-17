"""Proxy /grafana/: header di auth proxy non falsificabili, isolamento dal portale operatore."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.glpi import GlpiClient
from app.grafana_proxy import GrafanaProxy
from app.main import create_app, limiter
from mock import mock_glpi, mock_grafana


def grafana_echo():
    return GrafanaProxy(transport=httpx.ASGITransport(app=mock_grafana.app))


def grafana_down():
    def boom(request):
        raise httpx.ConnectError("down")
    return GrafanaProxy(transport=httpx.MockTransport(boom))


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "dev_roles", "capo:manager,both:operator+manager,cr-grafana-admin:manager")
    limiter._fails.clear()
    mock_glpi.seed()
    app = create_app()
    with TestClient(app) as c:
        app.state.glpi = GlpiClient(transport=httpx.ASGITransport(app=mock_glpi.app))
        app.state.grafana = grafana_echo()
        yield c


def login(c, user):
    assert c.post("/login", data={"username": user, "password": "dev"}, follow_redirects=False).status_code == 303


def test_forged_auth_header_is_overwritten(client):
    login(client, "capo")
    r = client.get("/grafana/d/abc", headers={"X-WEBAUTH-USER": "admin", "x-webauth-name": "x",
                                             "Authorization": "Basic YWRtaW46YWRtaW4=",
                                             "X-Grafana-Org-Id": "2"})
    assert r.status_code == 200
    h = r.json()["headers"]
    assert h["x-webauth-user"] == "capo"
    assert "x-webauth-name" not in h and "authorization" not in h and "x-grafana-org-id" not in h


def test_portal_cookie_not_forwarded_grafana_cookie_is(client):
    login(client, "capo")
    client.cookies.set("grafana_session", "g123")
    h = client.get("/grafana/").json()["headers"]
    assert "cr_session" not in h.get("cookie", "") and "grafana_session=g123" in h["cookie"]


def test_path_query_body_and_framing(client):
    login(client, "capo")
    r = client.post("/grafana/api/ds/query?from=now-7d&to=now", content=b'{"queries":[]}',
                    headers={"Content-Type": "application/json"})
    j = r.json()
    assert (j["path"], j["query"], j["method"], j["body"]) == ("api/ds/query", "from=now-7d&to=now", "POST", '{"queries":[]}')
    assert r.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"  # il "deny" di Grafana è sostituito
    assert "grafana_session=abc" in r.headers["set-cookie"]


def test_grafana_admin_username_is_refused(client):
    login(client, "cr-grafana-admin")
    assert client.get("/grafana/").status_code == 403


def test_write_methods_not_proxied(client):
    login(client, "capo")
    for m in ("put", "patch", "delete"):
        assert client.request(m.upper(), "/grafana/api/dashboards/uid/x").status_code == 405


def test_operator_never_reaches_grafana(client):
    login(client, "mrossi")
    assert client.get("/grafana/", headers={"X-WEBAUTH-USER": "capo"}).status_code == 403
    assert client.get("/grafana").status_code == 403


def test_grafana_down_does_not_affect_operator(client):
    client.app.state.grafana = grafana_down()
    login(client, "both")
    r = client.get("/grafana/api/health")
    assert r.status_code == 502 and "non raggiungibile" in r.json()["error"]
    assert client.get("/api/queue").status_code == 200
    ids = next(g["ids"] for g in client.get("/api/queue").json()["groups"] if g["code"] == "MOE-PA00500")
    assert client.post("/api/resolve", json={"ticket_ids": ids, "outcome": "rientrato"}, headers={"X-CR": "1"}).json()["ok"]


def test_app_starts_without_grafana_configured(monkeypatch):
    monkeypatch.setattr(settings, "grafana_url", "http://127.0.0.1:1")  # porta chiusa
    with TestClient(create_app()) as c:
        assert c.get("/healthz").status_code == 200


def test_html_pages_get_portal_theme(client):
    login(client, "capo")
    r = client.get("/grafana/page.html", headers={"Accept": "text/html", "Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert '<link rel="stylesheet" href="/static/grafana-brand.css"></head>' in r.text
    assert "<body>identity</body>" in r.text          # HTML richiesto non compresso
    assert int(r.headers["content-length"]) == len(r.content)
    assert r.headers["content-security-policy"] == "frame-ancestors 'self'"
    # le risposte non HTML passano invariate
    assert "grafana-brand" not in client.get("/grafana/api/x").text
