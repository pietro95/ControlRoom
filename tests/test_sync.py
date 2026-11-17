"""Worker di sync contro il GLPI simulato e un PostgreSQL reale."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.glpi import GlpiClient
from app.main import create_app, limiter
from mock import mock_glpi
from sync import worker
from sync.config import sync_settings

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def glpi():
    mock_glpi.seed()
    return GlpiClient(transport=httpx.ASGITransport(app=mock_glpi.app))


@pytest.fixture(autouse=True)
def no_local_files(monkeypatch, tmp_path):
    for k in ("audit_db_path", "site_reparto_csv", "excluded_csv"):
        monkeypatch.setattr(sync_settings, k, str(tmp_path / f"assente_{k}"))


def run(db, glpi, now=T0):
    return asyncio.run(worker.run_once(db, glpi, now))


def count(db, sql="SELECT count(*) FROM ticket"):
    return db.execute(sql).fetchone()[0]


def test_initial_full_load(db, glpi):
    r = run(db, glpi)
    assert r["ok"] and r["upserted"] == 7 and r["since"] is None and r["reconciled"] == 7
    kinds = dict(db.execute("SELECT kind, count(*) FROM ticket GROUP BY kind").fetchall())
    assert kinds == {"alert_on": 5, "alert_off": 1, "other": 1}
    assert count(db, "SELECT count(*) FROM v_alert") == 5
    # date GLPI (ora di Roma, novembre = +01) salvate in UTC
    assert db.execute("SELECT opened_at FROM ticket WHERE title LIKE '%%Roccapalumba'").fetchone()[0] == \
        datetime(2026, 11, 17, 6, 40, tzinfo=timezone.utc)


def test_rerun_is_idempotent(db, glpi):
    run(db, glpi)
    snap = db.execute("SELECT id, title, status, opened_at, modified_at FROM ticket ORDER BY id").fetchall()
    r = run(db, glpi, T0 + timedelta(minutes=1))
    assert r["ok"] and r["since"] is not None
    assert db.execute("SELECT id, title, status, opened_at, modified_at FROM ticket ORDER BY id").fetchall() == snap


def test_incremental_uses_date_mod_and_picks_up_changes(db, glpi, monkeypatch):
    run(db, glpi)
    seen = []
    orig = glpi.search

    async def spy(itemtype, params):
        seen.append(params)
        return await orig(itemtype, params)
    monkeypatch.setattr(glpi, "search", spy)
    tid = mock_glpi.add_ticket("ALERT ON: VIG-PA00100-Nuovo", "2026-11-17 14:00:00")
    old = next(t for t in mock_glpi.STATE["tickets"].values() if "Roccapalumba" in t["name"])
    old.update(status=5, solvedate="2026-11-17 14:05:00", date_mod="2026-11-17 14:05:00", solutiontype="Allarme rientrato")
    r = run(db, glpi, T0 + timedelta(minutes=5))
    p = seen[0]
    assert (p["criteria[1][field]"], p["criteria[1][searchtype]"]) == (19, "morethan")
    assert p["sort"] == 19 and p["order"] == "ASC"
    # cursore = ultima modifica vista (09:30 Roma) meno 5 minuti di overlap, espresso in ora di Roma
    assert p["criteria[1][value]"] == "2026-11-17 09:25:00"
    assert r["upserted"] == 3  # i due modificati + quello delle 09:30 riletto per l'overlap (upsert innocuo)
    row = db.execute("SELECT outcome, ttr_min FROM v_alert WHERE id = %s", (old["id"],)).fetchone()
    assert row == ("Rientrato", 385.0)
    assert count(db, f"SELECT count(*) FROM ticket WHERE id = {tid}") == 1


def test_pagination(db, glpi, monkeypatch):
    monkeypatch.setattr(sync_settings, "page_size", 2)
    for i in range(6):
        mock_glpi.add_ticket(f"ALERT ON: EGD-PA0010{i}-X", f"2026-11-16 1{i}:00:00")
    assert run(db, glpi)["upserted"] == 13 and count(db) == 13


def test_glpi_down_keeps_data_and_last_success(db, glpi):
    run(db, glpi)
    mock_glpi.STATE["fail"] = True
    glpi._session = None
    before = db.execute("SELECT id, status FROM ticket ORDER BY id").fetchall()
    r = run(db, glpi, T0 + timedelta(hours=1))
    assert r["ok"] is False
    st = db.execute("SELECT last_success_at, last_attempt_at, last_error, ticket_in_db FROM v_sync_status").fetchone()
    assert st[0] == T0 and st[1] == T0 + timedelta(hours=1) and "503" in st[2] and st[3] == 7
    assert db.execute("SELECT id, status FROM ticket ORDER BY id").fetchall() == before
    mock_glpi.STATE["fail"] = False
    assert run(db, glpi, T0 + timedelta(hours=2))["ok"]
    assert db.execute("SELECT last_error FROM v_sync_status").fetchone()[0] is None


def test_glpi_unreachable_network(db):
    def boom(request):
        raise httpx.ConnectError("down")
    r = run(db, GlpiClient(transport=httpx.MockTransport(boom)))
    assert r["ok"] is False and count(db) == 0


def test_reconcile_marks_deleted_and_restores(db, glpi, monkeypatch):
    run(db, glpi)
    victim = next(iter(mock_glpi.STATE["tickets"].values()))
    victim["is_deleted"] = True  # messo nel cestino: sparisce dalla ricerca, date_mod non utile
    assert "reconciled" not in run(db, glpi, T0 + timedelta(hours=1))  # non ancora dovuta
    run(db, glpi, T0 + timedelta(hours=25))
    assert db.execute("SELECT deleted FROM ticket WHERE id = %s", (victim["id"],)).fetchone()[0] is True
    victim["is_deleted"] = False
    run(db, glpi, T0 + timedelta(hours=50))
    assert db.execute("SELECT deleted FROM ticket WHERE id = %s", (victim["id"],)).fetchone()[0] is False


def test_reconcile_skipped_when_glpi_returns_nothing(db, glpi):
    run(db, glpi)
    for t in mock_glpi.STATE["tickets"].values():
        t["is_deleted"] = True
    r = run(db, glpi, T0 + timedelta(hours=25))
    assert r["reconcile_skipped"] and count(db, "SELECT count(*) FROM ticket WHERE deleted") == 0


def test_portal_to_reporting_end_to_end(db, glpi, monkeypatch, tmp_path):
    """Operatore chiude dal portale -> audit SQLite + soluzione GLPI -> sync -> v_alert."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "audit.db"))
    monkeypatch.setattr(sync_settings, "audit_db_path", str(tmp_path / "audit.db"))
    limiter._fails.clear()
    app = create_app()
    with TestClient(app) as c:
        app.state.glpi = GlpiClient(transport=httpx.ASGITransport(app=mock_glpi.app))
        c.post("/login", data={"username": "mrossi", "password": "dev"})
        ids = next(g["ids"] for g in c.get("/api/queue").json()["groups"] if g["code"] == "FNC-IS00400")
        assert c.post("/api/resolve", json={"ticket_ids": ids, "outcome": "manovra", "note": "Riarmo Q3"},
                      headers={"X-CR": "1"}).json()["ok"]
        body = {"location_id": 1, "location_name": "SUD Blufi > BLF-SAP00200 Serbatoio", "kind": "energia",
                "caller": "Guardiano"}
        call_id = c.post("/api/call", json=body, headers={"X-CR": "1"}).json()["ticket_id"]
    r = run(db, glpi, datetime.now(timezone.utc))
    assert r["ok"] and r["handlers"] == 4
    rows = db.execute("SELECT outcome, handled_by, is_repeat FROM v_alert WHERE id = ANY(%s) ORDER BY opened_at",
                      (ids,)).fetchall()
    assert rows == [("Manovra", "Mrossi", False), ("Manovra", "Mrossi", True), ("Manovra", "Mrossi", True)]  # 08:01, 08:20 (+19), 09:05 (+45)
    call = db.execute("SELECT site_code, request_type, handled_by FROM v_call").fetchall()
    assert call == [("BLF-SAP00200", "Telefono", "Mrossi")]
    # anche senza audit, l'operatore è ricostruito dal testo della soluzione
    db.execute("DELETE FROM handler")
    assert db.execute("SELECT handled_by FROM v_alert WHERE id = %s", (ids[0],)).fetchone()[0] == "Mrossi"
    assert call_id
