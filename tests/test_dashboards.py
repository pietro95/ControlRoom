"""Dashboard provisionate e dataset sintetico: ogni query di ogni pannello gira come grafana_ro su PostgreSQL reale."""
import asyncio
import json
import re
from pathlib import Path

import psycopg
import pytest

from scripts import build_dashboards
from scripts.gen_synthetic import build, replace_with_synthetic
from sync import worker
from sync.config import sync_settings

DASH = Path(__file__).resolve().parent.parent / "grafana" / "dashboards"
FILES = sorted(DASH.glob("*.json"))
ALLOWED = re.compile(r"^(v_\w+|generate_series|s|t|b|h)$")


def dashboards():
    return [(f.stem, json.loads(f.read_text())) for f in FILES]


def test_json_files_are_up_to_date():
    """I JSON versionati coincidono con quanto genera scripts/build_dashboards.py."""
    assert {f.stem for f in FILES} == set(build_dashboards.ALL)
    for name, fn in build_dashboards.ALL.items():
        assert json.loads((DASH / f"{name}.json").read_text()) == json.loads(json.dumps(fn())), name


def test_structure():
    uids = [d["uid"] for _, d in dashboards()]
    assert len(set(uids)) == len(uids)
    for name, d in dashboards():
        assert d["timezone"] == "Europe/Rome" and d["editable"] is False
        ids = [p["id"] for p in d["panels"]]
        assert len(set(ids)) == len(ids)
        assert any(p["title"] == "Ultima sincronizzazione riuscita" for p in d["panels"]), name
        for p in d["panels"]:
            for t in p.get("targets", []):
                assert t["datasource"]["uid"] == "reporting"
                assert "closed_at" not in t["rawSql"] and "closedate" not in t["rawSql"], (name, p["title"])
                # solo viste v_* (le tabelle grezze non applicano le regole KPI)
                for rel in re.findall(r"(?<!epoch )\b(?:FROM|JOIN)\s+([a-z_]\w*)", t["rawSql"]):  # escluso extract(epoch FROM x)
                    assert ALLOWED.match(rel), (name, p["title"], rel)


def expand(sql: str, values: dict[str, list[str]]) -> str:
    sql = re.sub(r"\$__timeFilter\((\w+)\)", r"\1 BETWEEN '2026-01-01T00:00:00Z' AND '2026-11-16T00:00:00Z'", sql)
    for k, vals in values.items():
        sql = sql.replace(f"${k}", ", ".join("'" + v.replace("'", "''") + "'" for v in vals))
    assert "$" not in sql, sql
    return sql


@pytest.fixture()
def synthetic(db, monkeypatch):
    for k in ("audit_db_path", "site_reparto_csv", "excluded_csv"):
        monkeypatch.setattr(sync_settings, k, "")
    rep = asyncio.run(replace_with_synthetic(db, seed=42))
    assert rep["ok"]
    return db


def test_synthetic_dataset_matches_declared_targets(synthetic):
    db = synthetic
    n, off = db.execute("SELECT count(*), count(*) FILTER (WHERE kind = 'alert_off') FROM ticket").fetchone()
    assert (n, off) == (4194, 194)
    assert db.execute("SELECT count(*) FROM ticket WHERE location IS NOT NULL AND kind = 'alert_on'").fetchone()[0] == 800
    assert db.execute("SELECT count(*) FROM excluded_ticket WHERE reason LIKE 'chiusura massiva%%'").fetchone()[0] == 160
    rep = db.execute("SELECT avg(is_repeat::int) FROM v_alert").fetchone()[0]
    assert 0.45 <= rep <= 0.58, rep
    label = db.execute("SELECT dataset_label FROM v_sync_status").fetchone()[0]
    assert label.startswith("SINTETICO") and "NON DATI REALI" in label
    unclassified = db.execute("SELECT avg((outcome = 'Non classificato')::int) FROM v_alert WHERE NOT is_open").fetchone()[0]
    assert 0.8 < unclassified < 1  # tipo soluzione solo dopo l'adozione (01/11)


def test_generator_is_deterministic():
    assert build(7) == build(7) and build(7) != build(8)


def test_generator_refuses_real_data(db):
    db.execute(worker.UPSERT, {"id": 1, "title": "ALERT ON: FNC-IS00400-x", "kind": "alert_on", "site_code": None,
                               "acquedotto": None, "status": 1, "opened_at": None, "solved_at": None,
                               "closed_at": None, "modified_at": None, "solution_type": None, "request_type": None,
                               "location": None, "reparto_glpi": None, "op_name": None, "op_user": None})
    with pytest.raises(SystemExit, match="NON sintetici"):
        asyncio.run(replace_with_synthetic(db))
    assert db.execute("SELECT count(*) FROM ticket").fetchone()[0] == 1


def test_every_panel_query_runs_as_grafana_ro(synthetic):
    empty = []
    with psycopg.connect(synthetic.dsn_for("grafana_ro"), autocommit=True) as g:
        g.execute("SET search_path = reporting")
        for name, d in dashboards():
            values = {}
            for v in d["templating"]["list"]:
                values[v["name"]] = [r[0] for r in g.execute(v["query"]).fetchall()]
                assert values[v["name"]], (name, v["name"])
            for p in d["panels"]:
                for t in p.get("targets", []):
                    try:
                        rows = g.execute(expand(t["rawSql"], values)).fetchall()
                    except psycopg.Error as e:
                        pytest.fail(f"{name} / {p['title']}: {e}")
                    if not rows:
                        empty.append(f"{name} / {p['title']}")
    assert empty == []


def test_heatmap_always_has_24_hours(synthetic):
    d = dict(dashboards())["overview"]
    sql = next(t["rawSql"] for p in d["panels"] if p["title"].startswith("Alert per ora") for t in p["targets"])
    rows = synthetic.execute(expand(sql, {"acquedotto": ["NESSUNO"]})).fetchall()
    assert len(rows) == 24 and all(sum(r[1:]) == 0 for r in rows)
