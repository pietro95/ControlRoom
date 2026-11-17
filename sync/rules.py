"""Regole di normalizzazione dei ticket GLPI per il reporting (funzioni pure, testate in tests/test_kpi.py).

Le regole KPI di aggregazione (esclusioni, finestra ripetizioni, tempi su solvedate, fasce orarie) sono nelle
viste SQL di sync/schema.sql; qui solo ciò che serve per scrivere righe pulite.
"""
import html
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.parsing import parse_alert_title

ROME = ZoneInfo("Europe/Rome")

_ALERT_ON = re.compile(r"^\s*ALERT\s+ON\s*:", re.I)
_ALERT_OFF = re.compile(r"^\s*ALERT\s+OFF\s*:", re.I)
_PROVA = re.compile(r"^\s*prova\b", re.I)
_CALL = re.compile(r"^\s*CHIAMATA\s*:", re.I)
_SITE = re.compile(r"\b([A-Z]{3})-([A-Z0-9]+)\b")
_OPERATOR = re.compile(r"\[Control Room app\s*[—–-]\s*operatore:\s*(.+?)\s*\(([^()]+)\)\s*\]")
_TAG = re.compile(r"<[^>]+>")


def parse_glpi_datetime(s: str | None) -> datetime | None:
    """'YYYY-MM-DD HH:MM:SS' nell'ora locale del server GLPI (Europe/Rome) -> datetime UTC.

    Ora ambigua (fine ora legale, 02:00-02:59 ripetuta): si assume la PRIMA occorrenza (ora legale, fold=0).
    Ora inesistente (inizio ora legale, 02:00-02:59 saltata): zoneinfo applica l'offset precedente (+01:00).
    """
    if not s or not str(s).strip():
        return None
    naive = datetime.strptime(str(s).strip()[:19], "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=ROME, fold=0).astimezone(timezone.utc)


def classify(title: str) -> dict:
    """kind: alert_on | alert_off | prova | call | other; più codice sito e acquedotto se riconoscibili."""
    t = title or ""
    if _PROVA.match(t):
        kind = "prova"
    elif _ALERT_OFF.match(t):
        kind = "alert_off"
    elif _ALERT_ON.match(t):
        kind = "alert_on"
    elif _CALL.match(t):
        kind = "call"
    else:
        kind = "other"
    site = None
    if kind == "alert_on":
        s = parse_alert_title(t)
        site = s.code if s else None
    elif kind in ("call", "alert_off"):
        m = _SITE.search(t.upper())
        site = f"{m.group(1)}-{m.group(2)}" if m else None
    return {"kind": kind, "site_code": site, "acquedotto": site.split("-")[0] if site else None}


def reparto_from_location(loc: str | None) -> str | None:
    """Reparto = prefisso della Posizione GLPI: 'SUD Blufi > BLF-...' o 'SUD Blufi - BLF-...' -> 'SUD Blufi'."""
    if not loc or not loc.strip():
        return None
    head = re.split(r"\s+>\s+|\s+-\s+", loc.strip(), maxsplit=1)[0].strip()
    return head or None


def operator_from_solution(text: str | None) -> tuple[str, str] | None:
    """(display_name, username) dalla riga scritta dall'app nel testo della soluzione; None se assente."""
    if not text:
        return None
    plain = html.unescape(_TAG.sub(" ", html.unescape(str(text))))
    m = _OPERATOR.search(plain)
    return (m.group(1).strip(), m.group(2).strip()) if m else None
