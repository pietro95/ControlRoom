"""Verifica sul GLPI reale che le search option configurate (GLPI_SO_*) puntino ai campi giusti.

    python -m sync.check_glpi          (dentro il container: docker compose run --rm --no-deps -T sync ...)

Confronta tabella e campo interni di GLPI (non tradotti, stabili tra le lingue) con quelli attesi, poi conta i
ticket dell'entità. Exit 0 solo se tutto torna. Non scrive nulla su GLPI.
"""
import asyncio
import sys

from app.config import settings
from app.glpi import GlpiClient, GlpiError

from .config import sync_settings as ss

# chiave -> (tabella attesa, campo atteso, significato). Valori del data model GLPI 10/11.
EXPECTED = {
    "title": ("glpi_tickets", "name", "titolo"),
    "id": ("glpi_tickets", "id", "ID"),
    "request_type": ("glpi_requesttypes", "name", "origine della richiesta"),
    "status": ("glpi_tickets", "status", "stato"),
    "opened": ("glpi_tickets", "date", "data di apertura"),
    "closed": ("glpi_tickets", "closedate", "data di chiusura"),
    "solved": ("glpi_tickets", "solvedate", "data di risoluzione"),
    "modified": ("glpi_tickets", "date_mod", "ultima modifica"),
    "solution_type": ("glpi_solutiontypes", "name", "tipo di soluzione"),
    "solution": ("glpi_itilsolutions", "content", "testo della soluzione"),
    "entity": ("glpi_entities", "completename", "entità"),
    "location": ("glpi_locations", "completename", "posizione"),
}


async def check(glpi: GlpiClient) -> tuple[bool, list[str]]:
    opts = await glpi._request("GET", "/listSearchOptions/Ticket") or {}
    ok, lines = True, []
    for key, so in ss.so.items():
        table, field, meaning = EXPECTED[key]
        o = opts.get(str(so)) or {}
        got_t, got_f = o.get("table", "?"), o.get("field", "?")
        good = got_t == table and got_f == field
        ok &= good
        lines.append(f"{'OK' if good else 'KO':2}  {key:13} opz.{so:<4} {o.get('name', '(assente)')!s:35} "
                     f"{got_t}.{got_f}" + ("" if good else f"   <- atteso {table}.{field} ({meaning})"))
    if ok:
        _, total = await glpi.search("Ticket", {
            "criteria[0][field]": ss.so["entity"], "criteria[0][searchtype]": "equals",
            "criteria[0][value]": settings.entity_id, "forcedisplay[0]": ss.so["id"], "range": "0-0"})
        lines.append(f"Ticket nell'entità {settings.entity_id}: {total} (atteso nell'ordine di ~4.200)")
        if total == 0:
            ok = False
            lines.append("KO  nessun ticket: entità o permessi del profilo di servizio da verificare")
    return ok, lines


async def main() -> int:
    glpi = GlpiClient()
    try:
        ok, lines = await check(glpi)
    except GlpiError as e:
        print(f"KO  GLPI non raggiungibile o credenziali errate: {e}")
        return 2
    finally:
        await glpi.aclose()
    print("\n".join(lines))
    print("ESITO: OK" if ok else "ESITO: KO - correggere GLPI_SO_* nel .env prima di avviare il sync")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
