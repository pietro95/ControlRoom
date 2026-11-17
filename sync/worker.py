"""Worker di sincronizzazione GLPI -> PostgreSQL (schema reporting). Avvio: python -m sync.worker

Ogni giro:
1. legge da GLPI i ticket dell'entità modificati dopo (cursore - overlap), per data di ultima modifica (opz. 19),
   paginando; se GLPI non risponde NON tocca i dati né il cursore: registra solo tentativo ed errore;
2. in UNA transazione: upsert dei ticket (idempotente), avanzamento cursore, last_success_at;
3. audit dell'app (SQLite in sola lettura) -> tabella handler; CSV mapping/esclusioni se cambiati;
   esclusione delle chiusure massive; riconciliazione cancellazioni ogni SYNC_RECONCILE_HOURS.
"""
import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

from app.config import settings
from app.glpi import GlpiClient, GlpiError
from app.parsing import parse_alert_title

from .config import sync_settings as ss
from .rules import ROME, classify, operator_from_solution, parse_glpi_datetime, reparto_from_location

log = logging.getLogger("sync")
SCHEMA = Path(__file__).with_name("schema.sql")

UPSERT = """
INSERT INTO ticket (id, title, kind, site_code, acquedotto, status, opened_at, solved_at, closed_at, modified_at,
                    solution_type, request_type, location, reparto_glpi, solution_operator_name,
                    solution_operator_user, deleted, synced_at)
VALUES (%(id)s, %(title)s, %(kind)s, %(site_code)s, %(acquedotto)s, %(status)s, %(opened_at)s, %(solved_at)s,
        %(closed_at)s, %(modified_at)s, %(solution_type)s, %(request_type)s, %(location)s, %(reparto_glpi)s,
        %(op_name)s, %(op_user)s, false, now())
ON CONFLICT (id) DO UPDATE SET
    title = EXCLUDED.title, kind = EXCLUDED.kind, site_code = EXCLUDED.site_code, acquedotto = EXCLUDED.acquedotto,
    status = EXCLUDED.status, opened_at = EXCLUDED.opened_at, solved_at = EXCLUDED.solved_at,
    closed_at = EXCLUDED.closed_at, modified_at = EXCLUDED.modified_at, solution_type = EXCLUDED.solution_type,
    request_type = EXCLUDED.request_type, location = EXCLUDED.location, reparto_glpi = EXCLUDED.reparto_glpi,
    solution_operator_name = EXCLUDED.solution_operator_name, solution_operator_user = EXCLUDED.solution_operator_user,
    deleted = false, synced_at = now()
"""


# ------------------------------------------------------------------ schema e stato

def apply_schema(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute(SCHEMA.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO setting VALUES ('flapping_window_min', %s) "
                     "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(ss.flapping_window_min),))


def get_state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = %s", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn, key: str, value: str | None) -> None:
    conn.execute("INSERT INTO sync_state (key, value, updated_at) VALUES (%s, %s, now()) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", (key, value))


# ------------------------------------------------------------------ lettura GLPI

def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, list):  # campi multivalore: GLPI può restituire una lista
        v = ", ".join(str(x) for x in v if x is not None)
    v = str(v).strip()
    return v or None


def to_row(r: dict) -> dict:
    """Riga di /search/Ticket (chiavi = id search option) -> riga per la tabella ticket."""
    so = ss.so
    g = lambda k: r.get(str(so[k]))  # noqa: E731
    title = _text(g("title")) or ""
    c = classify(title)
    loc = _text(g("location"))
    op = operator_from_solution(_text(g("solution")))
    try:
        status = int(g("status")) if g("status") not in (None, "") else None
    except (TypeError, ValueError):
        status = None
    return {
        "id": int(g("id")), "title": title, **c, "status": status,
        "opened_at": parse_glpi_datetime(_text(g("opened"))),
        "solved_at": parse_glpi_datetime(_text(g("solved"))),
        "closed_at": parse_glpi_datetime(_text(g("closed"))),
        "modified_at": parse_glpi_datetime(_text(g("modified"))),
        "solution_type": _text(g("solution_type")), "request_type": _text(g("request_type")),
        "location": loc, "reparto_glpi": reparto_from_location(loc),
        "op_name": op[0] if op else None, "op_user": op[1] if op else None,
    }


async def fetch_modified(glpi: GlpiClient, since_utc: datetime | None) -> list[dict]:
    """Tutti i ticket dell'entità modificati dopo since_utc (None = carico completo), ordinati per data modifica."""
    so = ss.so
    base = {"criteria[0][field]": so["entity"], "criteria[0][searchtype]": "equals",
            "criteria[0][value]": settings.entity_id, "sort": so["modified"], "order": "ASC"}
    if since_utc:
        local = since_utc.astimezone(ROME).strftime("%Y-%m-%d %H:%M:%S")  # GLPI confronta in ora locale
        base.update({"criteria[1][link]": "AND", "criteria[1][field]": so["modified"],
                     "criteria[1][searchtype]": "morethan", "criteria[1][value]": local})
    for i, f in enumerate(so.values()):
        base[f"forcedisplay[{i}]"] = f
    rows, start = [], 0
    while True:
        page, total = await glpi.search("Ticket", {**base, "range": f"{start}-{start + ss.page_size - 1}"})
        rows += [to_row(r) for r in page]
        start += ss.page_size
        if not page or start >= total:
            return rows


async def fetch_all_ids(glpi: GlpiClient) -> list[int]:
    so = ss.so
    base = {"criteria[0][field]": so["entity"], "criteria[0][searchtype]": "equals",
            "criteria[0][value]": settings.entity_id, "forcedisplay[0]": so["id"], "sort": so["id"], "order": "ASC"}
    ids, start, total = [], 0, 0
    while True:
        page, total = await glpi.search("Ticket", {**base, "range": f"{start}-{start + ss.page_size - 1}"})
        ids += [int(r[str(so["id"])]) for r in page]
        start += ss.page_size
        if not page or start >= total:
            break
    if len(set(ids)) != total:
        raise GlpiError(f"riconciliazione incoerente: {len(set(ids))} id letti, totalcount {total}")
    return ids


# ------------------------------------------------------------------ fonti locali

def load_audit(conn) -> int:
    """Operatore che ha gestito, dall'audit SQLite dell'app (aperto in sola lettura)."""
    if not ss.audit_db_path or not os.path.exists(ss.audit_db_path):
        return 0
    try:
        src = sqlite3.connect(f"file:{ss.audit_db_path}?mode=ro", uri=True, timeout=5)
        rows = src.execute("SELECT ts, username, display_name, action, detail FROM audit "
                           "WHERE action IN ('resolve', 'call') ORDER BY id").fetchall()
        src.close()
    except sqlite3.Error as e:
        log.warning("audit non leggibile: %s", e)
        return 0
    out: dict[int, tuple] = {}
    for ts, user, name, action, detail in rows:
        d = json.loads(detail or "{}")
        ids = d.get("done", []) if action == "resolve" else [d.get("ticket")]
        for tid in ids:
            if isinstance(tid, int):
                out[tid] = (tid, user, name, action, ts)  # l'ultima azione vince
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO handler (ticket_id, username, display_name, action, ts) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (ticket_id) DO UPDATE SET username = EXCLUDED.username, display_name = "
            "EXCLUDED.display_name, action = EXCLUDED.action, ts = EXCLUDED.ts", list(out.values()))
    return len(out)


def _read_csv(path: str) -> tuple[str, list[list[str]]] | None:
    if not path or not os.path.exists(path):
        return None
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8-sig")
    delim = ";" if text.count(";") >= text.count(",") else ","
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim)
            if r and any(c.strip() for c in r) and not r[0].strip().startswith("#")]
    return hashlib.sha256(raw).hexdigest(), rows[1:]  # prima riga = intestazione


def load_site_reparto_csv(conn, path: str | None = None) -> str:
    """CSV 'codice_sito;reparto' -> site_reparto_manual (sostituzione completa, solo se il file è cambiato).
    Un file con righe non valide NON viene caricato: resta il mapping precedente."""
    got = _read_csv(path or ss.site_reparto_csv)
    if got is None:
        return "assente"
    sha, rows = got
    if get_state(conn, "site_reparto_csv_sha") == sha:
        return "invariato"
    bad = [i + 2 for i, r in enumerate(rows) if len(r) < 2 or not r[0] or not r[1]]
    codes = [r[0].upper() for r in rows if len(r) >= 2]
    if bad or len(codes) != len(set(codes)):
        msg = f"CSV mapping non caricato: righe non valide {bad[:10]}" if bad else "CSV mapping: codici duplicati"
        set_state(conn, "site_reparto_csv_error", msg)
        log.error(msg)
        return "errore"
    with conn.cursor() as cur:
        cur.execute("DELETE FROM site_reparto_manual")
        cur.executemany("INSERT INTO site_reparto_manual (site_code, reparto) VALUES (%s, %s)",
                        [(r[0].upper(), r[1]) for r in rows])
    set_state(conn, "site_reparto_csv_sha", sha)
    set_state(conn, "site_reparto_csv_error", None)
    return f"caricate {len(rows)} righe"


def load_excluded_csv(conn, path: str | None = None) -> str:
    """CSV 'ticket_id;motivo' -> excluded_ticket (aggiunte; le righe 'manuale' assenti dal file sono rimosse)."""
    got = _read_csv(path or ss.excluded_csv)
    if got is None:
        return "assente"
    sha, rows = got
    if get_state(conn, "excluded_csv_sha") == sha:
        return "invariato"
    try:
        items = [(int(r[0]), "manuale: " + (r[1] if len(r) > 1 and r[1] else "senza motivo")) for r in rows]
    except ValueError:
        log.error("CSV esclusioni non caricato: id non numerico")
        return "errore"
    with conn.cursor() as cur:
        cur.execute("DELETE FROM excluded_ticket WHERE reason LIKE 'manuale:%%'")
        cur.executemany("INSERT INTO excluded_ticket (ticket_id, reason) VALUES (%s, %s) "
                        "ON CONFLICT (ticket_id) DO NOTHING", items)
    set_state(conn, "excluded_csv_sha", sha)
    return f"caricate {len(items)} righe"


def apply_mass_closure_exclusions(conn) -> int:
    """Esclude i ticket risolti o chiusi nei minuti di chiusura massiva (ora di Roma)."""
    n = 0
    for m in ss.mass_closure_minutes:
        start = datetime.strptime(m, "%Y-%m-%d %H:%M").replace(tzinfo=ROME).astimezone(timezone.utc)
        end = start + timedelta(minutes=1)
        cur = conn.execute(
            "INSERT INTO excluded_ticket (ticket_id, reason) "
            "SELECT id, %s FROM ticket WHERE (solved_at >= %s AND solved_at < %s) OR (closed_at >= %s AND closed_at < %s) "
            "ON CONFLICT (ticket_id) DO NOTHING",
            (f"chiusura massiva {m}", start, end, start, end))
        n += cur.rowcount
    return n


# ------------------------------------------------------------------ giro di sync

_loc_cache: dict[str, tuple[float, dict | None]] = {}  # codice sito -> (istante, posizione GLPI o None)
LOC_TTL_HIT, LOC_TTL_MISS = 3600.0, 300.0


async def fill_locations(glpi: GlpiClient) -> dict:
    """Alert aperti senza Posizione GLPI: si cerca tra le Posizioni quella che contiene il codice sito e la si
    scrive sul ticket, senza aspettare la presa in carico. Nessuna o più corrispondenze: ticket lasciato com'è
    (sarà l'operatore a indicarla alla presa in carico). Errori su un ticket non fermano il giro."""
    out = {"set": 0, "unresolved": 0, "errors": 0}
    now = time.monotonic()
    for r in await glpi.open_alerts():
        site = parse_alert_title(r["title"])
        if r.get("location") or not site:
            continue
        hit = _loc_cache.get(site.code)
        if not hit or now - hit[0] > (LOC_TTL_HIT if hit[1] else LOC_TTL_MISS):
            try:
                hit = _loc_cache[site.code] = (now, await glpi.location_for_code(site.code))
            except GlpiError as e:
                log.warning("posizione per %s: %s", site.code, e)
                out["errors"] += 1
                continue
        if not hit[1]:
            out["unresolved"] += 1
            continue
        try:
            await glpi.set_location(r["id"], hit[1]["id"])
            out["set"] += 1
            log.info("ticket #%s: posizione %s (%s)", r["id"], hit[1]["name"], site.code)
        except GlpiError as e:
            log.warning("set_location #%s: %s", r["id"], e)
            out["errors"] += 1
    return out


async def run_once(conn: psycopg.Connection, glpi: GlpiClient, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cursor = get_state(conn, "cursor_modified_at")
    since = datetime.fromisoformat(cursor) - timedelta(seconds=ss.overlap_seconds) if cursor else None
    report: dict = {"since": since.isoformat() if since else None}
    try:  # prima della lettura: le posizioni appena scritte entrano già in questo giro
        report["locations"] = await fill_locations(glpi)
    except GlpiError as e:
        log.warning("valorizzazione posizioni saltata: %s", e)
    try:
        rows = await fetch_modified(glpi, since)
        last_rec = get_state(conn, "last_reconcile_at")
        due = not last_rec or now - datetime.fromisoformat(last_rec) >= timedelta(hours=ss.reconcile_hours)
        ids = await fetch_all_ids(glpi) if due else None
    except GlpiError as e:
        with conn.transaction():
            set_state(conn, "last_attempt_at", now.isoformat())
            set_state(conn, "last_error", str(e)[:500])
        log.warning("GLPI non disponibile, dati invariati: %s", e)
        return {**report, "ok": False, "error": str(e)}

    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(UPSERT, rows)
        mods = [r["modified_at"] for r in rows if r["modified_at"]]
        if mods:
            new_cursor = max(mods + ([datetime.fromisoformat(cursor)] if cursor else []))
            set_state(conn, "cursor_modified_at", new_cursor.isoformat())
        if ids is not None and not ids and conn.execute("SELECT count(*) FROM ticket").fetchone()[0]:
            # GLPI risponde "zero ticket" ma il DB ne ha: più probabile un permesso/filtro rotto che una
            # cancellazione totale. Non si marca nulla come cancellato.
            log.error("riconciliazione saltata: GLPI ha restituito 0 ticket")
            report["reconcile_skipped"] = True
            ids = None
        if ids is not None:
            conn.execute("UPDATE ticket SET deleted = NOT (id = ANY(%s))", (ids,))
            set_state(conn, "last_reconcile_at", now.isoformat())
            report["reconciled"] = len(ids)
        report["mapping"] = load_site_reparto_csv(conn)
        report["excluded_csv"] = load_excluded_csv(conn)
        report["mass_excluded"] = apply_mass_closure_exclusions(conn)
        report["handlers"] = load_audit(conn)
        set_state(conn, "last_attempt_at", now.isoformat())
        set_state(conn, "last_success_at", now.isoformat())
        set_state(conn, "last_error", None)
    return {**report, "ok": True, "upserted": len(rows)}


def connect() -> psycopg.Connection:
    if not ss.database_url:
        raise SystemExit("DATABASE_URL mancante")
    conn = psycopg.connect(ss.database_url, autocommit=True, connect_timeout=10)
    conn.execute("SET search_path = reporting")
    return conn


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    glpi = GlpiClient()
    conn = None
    while True:
        try:
            if conn is None or conn.closed:
                conn = connect()
                apply_schema(conn)
                log.info("schema applicato")
            log.info("sync: %s", await run_once(conn, glpi))
        except psycopg.Error as e:  # Postgres giù: riprova al prossimo giro con una connessione nuova
            log.warning("database non disponibile: %s", e.__class__.__name__)
            if conn is not None:
                conn.close()
            conn = None
        Path(ss.heartbeat_file).write_text(str(time.time()))
        await asyncio.sleep(ss.interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
