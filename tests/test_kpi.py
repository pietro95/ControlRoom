"""Regole KPI (vedi intestazione di sync/schema.sql): una o più verifiche per ciascuna, su PostgreSQL reale."""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from sync import worker
from sync.config import sync_settings
from sync.rules import classify, operator_from_solution, parse_glpi_datetime, reparto_from_location

UTC = timezone.utc


def utc(*a):
    return datetime(*a, tzinfo=UTC)


_next = [1000]


def ins(db, title="ALERT ON: FNC-IS00400-Campanella", opened=utc(2026, 11, 2, 8, 0), **kw):
    _next[0] += 1
    row = {"id": _next[0], "title": title, **classify(title), "status": 1, "opened_at": opened,
           "solved_at": None, "closed_at": None, "modified_at": opened, "solution_type": None,
           "request_type": "E-Mail", "location": None, "reparto_glpi": None, "op_name": None, "op_user": None}
    row.update(kw)
    db.execute(worker.UPSERT, row)
    return row["id"]


def alerts(db, cols="id"):
    return db.execute(f"SELECT {cols} FROM v_alert ORDER BY opened_at, id").fetchall()


def one(db, tid, col):
    return db.execute(f"SELECT {col} FROM v_alert WHERE id = %s", (tid,)).fetchone()[0]


# ------------------------------------------------------------------ funzioni pure

def test_glpi_datetime_rome_to_utc_with_dst():
    assert parse_glpi_datetime("2026-01-15 10:00:00") == utc(2026, 1, 15, 9, 0)      # CET  +01
    assert parse_glpi_datetime("2026-07-15 10:00:00") == utc(2026, 7, 15, 8, 0)      # CEST +02
    # 25/10/2026: 02:30 avviene due volte -> si assume la prima (ora legale)
    assert parse_glpi_datetime("2026-10-25 02:30:00") == utc(2026, 10, 25, 0, 30)
    assert parse_glpi_datetime("2026-10-25 03:30:00") == utc(2026, 10, 25, 2, 30)
    # 29/03/2026: 02:30 non esiste -> offset precedente (+01), cioè 03:30 CEST
    assert parse_glpi_datetime("2026-03-29 02:30:00") == utc(2026, 3, 29, 1, 30)
    assert parse_glpi_datetime("") is None and parse_glpi_datetime(None) is None


@pytest.mark.parametrize("title,kind,site", [
    ("ALERT ON: FNC-IS00400-Campanella", "alert_on", "FNC-IS00400"),
    ("alert on: anc-pa02100-Salvatorello 2", "alert_on", "ANC-PA02100"),
    ("ALERT OFF: ANC-PA01800-Baronessa", "alert_off", "ANC-PA01800"),
    ("Prova invio ALERT ON: FNC-IS00400-x", "prova", None),
    ("PROVA", "prova", None),
    ("Provato ALERT ON: FNC-IS00400-x", "other", None),
    ("CHIAMATA: BLF-SAP00200 – Mancanza energia", "call", "BLF-SAP00200"),
    ("ALERT ON: titolo senza codice", "alert_on", None),
    ("Richiesta varia", "other", None),
])
def test_classify(title, kind, site):
    c = classify(title)
    assert (c["kind"], c["site_code"]) == (kind, site)
    assert c["acquedotto"] == (site.split("-")[0] if site else None)


def test_reparto_from_location():
    assert reparto_from_location("SUD Blufi > BLF-SAP00200 Serbatoio") == "SUD Blufi"
    assert reparto_from_location("SUD Blufi - BLF-SAP00200 Serbatoio") == "SUD Blufi"
    assert reparto_from_location("CENTRO Fanaco") == "CENTRO Fanaco"
    assert reparto_from_location("") is None and reparto_from_location(None) is None


def test_operator_from_solution_text():
    t = "Allarme rientrato.\n[Control Room app — operatore: Mario Rossi (mrossi)]"
    assert operator_from_solution(t) == ("Mario Rossi", "mrossi")
    html = "&lt;p&gt;Manovra eseguita: riarmo&lt;/p&gt;&lt;p&gt;[Control Room app — operatore: Anna D&#039;Amico (adamico)]&lt;/p&gt;"
    assert operator_from_solution(html) == ("Anna D'Amico", "adamico")
    assert operator_from_solution("Chiuso da tecnico") is None


# ------------------------------------------------------------------ R1, R2: cosa entra nelle statistiche

def test_r1_only_alert_on(db):
    keep = ins(db)
    ins(db, "ALERT OFF: ANC-PA01800-Baronessa")
    ins(db, "Prova ALERT ON: FNC-IS00400-x")
    ins(db, "Richiesta varia")
    call = ins(db, "CHIAMATA: FNC-IS00600 – Altro", request_type="Telefono")
    assert alerts(db) == [(keep,)]
    assert db.execute("SELECT id FROM v_call").fetchall() == [(call,)]


def test_r2_excluded_and_deleted(db):
    keep, excl, gone = ins(db), ins(db), ins(db)
    db.execute("INSERT INTO excluded_ticket VALUES (%s, 'manuale: test')", (excl,))
    db.execute("UPDATE ticket SET deleted = true WHERE id = %s", (gone,))
    assert alerts(db) == [(keep,)]


def test_r2_mass_closure_minute_in_rome_time(db, monkeypatch):
    monkeypatch.setattr(sync_settings, "mass_closure_minutes", ["2026-08-20 17:42"])
    # 20/08/2026 è ora legale: 17:42 a Roma = 15:42 UTC
    inside_solved = ins(db, solved_at=utc(2026, 8, 20, 15, 42, 0), closed_at=utc(2026, 8, 21, 6, 0))
    inside_closed = ins(db, solved_at=utc(2026, 8, 19, 10, 0), closed_at=utc(2026, 8, 20, 15, 42, 59))
    before = ins(db, solved_at=utc(2026, 8, 20, 15, 41, 59))
    after = ins(db, solved_at=utc(2026, 8, 20, 15, 43, 0))
    utc_trap = ins(db, solved_at=utc(2026, 8, 20, 17, 42, 30))  # 17:42 UTC = 19:42 a Roma: NON escluso
    assert worker.apply_mass_closure_exclusions(db) == 2
    assert worker.apply_mass_closure_exclusions(db) == 0  # idempotente
    ids = {r[0] for r in alerts(db)}
    assert ids == {before, after, utc_trap} and not ids & {inside_solved, inside_closed}


# ------------------------------------------------------------------ R3: tempi su solvedate

def test_r3_ttr_uses_solvedate_never_closedate(db):
    a = ins(db, opened=utc(2026, 11, 2, 8, 0), solved_at=utc(2026, 11, 2, 8, 30), closed_at=utc(2026, 11, 3, 7, 0))
    b = ins(db, opened=utc(2026, 11, 2, 9, 0), solved_at=None, closed_at=utc(2026, 11, 3, 7, 0))
    assert one(db, a, "ttr_min") == 30
    assert one(db, b, "ttr_min") is None


def test_r3_median_p90_exclude_missing_solvedate(db):
    for m in (10, 20, 30, 40, 1000):
        ins(db, opened=utc(2026, 11, 2, 8, 0), solved_at=utc(2026, 11, 2, 8, 0) + timedelta(minutes=m))
    ins(db, opened=utc(2026, 11, 2, 8, 0))  # aperto/senza solvedate: escluso
    med, p90, n = db.execute("SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY ttr_min), "
                             "percentile_cont(0.9) WITHIN GROUP (ORDER BY ttr_min), count(ttr_min) FROM v_alert").fetchone()
    assert (med, n) == (30, 5) and p90 == pytest.approx(616.0)


def test_r3_negative_duration_is_not_a_time(db):
    t = ins(db, opened=utc(2026, 11, 2, 8, 0), solved_at=utc(2026, 11, 2, 7, 0))  # dato incoerente
    assert one(db, t, "ttr_min") is None


# ------------------------------------------------------------------ R4: ripetizioni

def _at(m):
    return utc(2026, 11, 2, 8, 0) + timedelta(minutes=m)


def test_r4_window_boundaries(db):
    first = ins(db, opened=_at(0))
    at59 = ins(db, opened=_at(59))
    at119 = ins(db, opened=_at(119))          # 60 min esatti dal precedente: ripetuto
    at180 = ins(db, opened=_at(180))          # 61 min: non ripetuto
    other_site = ins(db, "ALERT ON: ANC-PA02100-Salvatorello 2", opened=_at(1))
    got = dict(db.execute("SELECT id, is_repeat FROM v_alert").fetchall())
    assert got == {first: False, at59: True, at119: True, at180: False, other_site: False}


def test_r4_excluded_alert_is_not_a_predecessor(db):
    ins(db, opened=_at(0))
    prova = ins(db, "Prova ALERT ON: FNC-IS00400-x", opened=_at(100))
    excl = ins(db, opened=_at(110))
    db.execute("INSERT INTO excluded_ticket VALUES (%s, 'manuale: test')", (excl,))
    last = ins(db, opened=_at(120))
    assert one(db, last, "is_repeat") is False and prova and excl


def test_r4_window_is_configurable(db):
    ins(db, opened=_at(0))
    t = ins(db, opened=_at(45))
    assert one(db, t, "is_repeat") is True
    db.execute("UPDATE setting SET value = '30' WHERE key = 'flapping_window_min'")
    assert one(db, t, "is_repeat") is False


def test_r4_window_from_env_on_schema_apply(db, monkeypatch):
    monkeypatch.setattr(sync_settings, "flapping_window_min", 15)
    worker.apply_schema(db)
    assert db.execute("SELECT value FROM setting WHERE key = 'flapping_window_min'").fetchone()[0] == "15"


def test_r4_unparsable_titles_never_repeat(db):
    a = ins(db, "ALERT ON: titolo senza codice", opened=_at(0))
    b = ins(db, "ALERT ON: titolo senza codice", opened=_at(5))
    assert one(db, a, "is_repeat") is False and one(db, b, "is_repeat") is False


# ------------------------------------------------------------------ R5: fuso orario

@pytest.mark.parametrize("opened,hour,day,dow", [
    (utc(2026, 1, 15, 22, 30), 23, "2026-01-15", 4),     # inverno +01
    (utc(2026, 7, 15, 22, 30), 0, "2026-07-16", 4),      # estate +02: cambia giorno
    (utc(2026, 3, 29, 0, 30), 1, "2026-03-29", 7),       # prima del salto
    (utc(2026, 3, 29, 1, 30), 3, "2026-03-29", 7),       # dopo il salto (le 02 non esistono)
    (utc(2026, 10, 25, 0, 30), 2, "2026-10-25", 7),      # 02:30 CEST
    (utc(2026, 10, 25, 1, 30), 2, "2026-10-25", 7),      # 02:30 CET (ora ripetuta)
    (utc(2026, 11, 16, 23, 30), 0, "2026-11-17", 2),     # lunedì sera UTC = martedì a Roma
])
def test_r5_hour_day_dow_in_rome(db, opened, hour, day, dow):
    t = ins(db, opened=opened)
    h, d, w = db.execute("SELECT hour_local, day_local::text, dow_local FROM v_alert WHERE id = %s", (t,)).fetchone()
    assert (h, d, w) == (hour, day, dow)


def test_r5_independent_of_session_timezone(db):
    t = ins(db, opened=utc(2026, 7, 15, 22, 30))
    db.execute("SET TIME ZONE 'America/New_York'")
    assert one(db, t, "hour_local") == 0


# ------------------------------------------------------------------ R6: esiti

def test_r6_outcomes(db):
    ids = {k: ins(db, solution_type=v) for k, v in [
        ("Rientrato", "Allarme rientrato"), ("Manovra", "Manovra eseguita"), ("Altro", "Altro"),
        ("Altro2", "Intervento tecnico"), ("Non classificato", None), ("Vuoto", " ")]}
    got = dict(db.execute("SELECT id, outcome FROM v_alert").fetchall())
    assert got[ids["Rientrato"]] == "Rientrato" and got[ids["Manovra"]] == "Manovra"
    assert got[ids["Altro"]] == "Altro" and got[ids["Altro2"]] == "Altro"
    assert got[ids["Non classificato"]] == got[ids["Vuoto"]] == "Non classificato"


# ------------------------------------------------------------------ R7: chi ha gestito

def test_r7_handler_sources(db):
    audit = ins(db, op_name="Da Testo", op_user="testo")
    text = ins(db, op_name="Anna Bianchi", op_user="abianchi")
    old = ins(db)
    db.execute("INSERT INTO handler VALUES (%s, 'mrossi', 'Mario Rossi', 'resolve', now())", (audit,))
    got = dict(db.execute("SELECT id, handled_by FROM v_alert").fetchall())
    assert got == {audit: "Mario Rossi", text: "Anna Bianchi", old: "sconosciuto"}


# ------------------------------------------------------------------ reparto e copertura mapping

def test_reparto_mapping_and_coverage(db, tmp_path):
    ins(db, "ALERT ON: BLF-SAP00200-Cozzo", location="SUD Blufi > BLF-SAP00200", reparto_glpi="SUD Blufi")
    ins(db, "ALERT ON: BLF-SAP00200-Cozzo")                       # stesso sito, senza posizione: eredita
    ins(db, "ALERT ON: FNC-IS00400-Campanella", reparto_glpi="Errato")
    ins(db, "ALERT ON: MOE-PA00500-Roccapalumba")                 # nessuna fonte
    csv = tmp_path / "m.csv"
    csv.write_text("codice_sito;reparto\nfnc-is00400;CENTRO Fanaco\n", encoding="utf-8")
    assert worker.load_site_reparto_csv(db, str(csv)) == "caricate 1 righe"
    assert worker.load_site_reparto_csv(db, str(csv)) == "invariato"
    got = dict(db.execute("SELECT site_code, reparto FROM v_alert").fetchall())
    assert got == {"BLF-SAP00200": "SUD Blufi", "FNC-IS00400": "CENTRO Fanaco", "MOE-PA00500": "non mappato"}
    assert db.execute("SELECT * FROM v_mapping_coverage").fetchone() == (3, 2, 4, 3)


def test_invalid_mapping_csv_keeps_previous(db, tmp_path):
    csv = tmp_path / "m.csv"
    csv.write_text("codice_sito;reparto\nFNC-IS00400;CENTRO Fanaco\n")
    worker.load_site_reparto_csv(db, str(csv))
    csv.write_text("codice_sito;reparto\nFNC-IS00400;\nANC-PA02100;NORD\n")
    assert worker.load_site_reparto_csv(db, str(csv)) == "errore"
    assert db.execute("SELECT site_code, reparto FROM site_reparto_manual").fetchall() == [("FNC-IS00400", "CENTRO Fanaco")]
    assert "righe non valide [2]" in worker.get_state(db, "site_reparto_csv_error")


def test_excluded_csv(db, tmp_path):
    a, b = ins(db), ins(db)
    csv = tmp_path / "e.csv"
    csv.write_text(f"ticket_id,motivo\n{a},ticket di collaudo\n")
    worker.load_excluded_csv(db, str(csv))
    assert alerts(db) == [(b,)]
    csv.write_text("ticket_id,motivo\n")  # rimosso dal file -> torna nelle statistiche
    worker.load_excluded_csv(db, str(csv))
    assert len(alerts(db)) == 2


# ------------------------------------------------------------------ privilegi Grafana

def test_grafana_ro_can_only_select(db):
    ins(db)
    with psycopg.connect(db.dsn_for("grafana_ro"), autocommit=True) as g:
        assert g.execute("SELECT count(*) FROM v_alert").fetchone()[0] == 1
        for sql in ("INSERT INTO reporting.setting VALUES ('x','y')", "DELETE FROM reporting.ticket",
                    "CREATE TABLE reporting.x (i int)", "CREATE TABLE public.x (i int)",
                    "UPDATE reporting.setting SET value = '1'"):
            with pytest.raises(psycopg.Error):
                g.execute(sql)
