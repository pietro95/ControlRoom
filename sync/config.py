"""Configurazione del worker di sync (stesse variabili .env del portale, più queste)."""
import os


def _ids(name: str, default: str) -> int:
    return int(os.getenv(name, default))


class SyncSettings:
    database_url = os.getenv("DATABASE_URL", "")
    interval_seconds = int(os.getenv("SYNC_INTERVAL_SECONDS", "60"))
    overlap_seconds = int(os.getenv("SYNC_OVERLAP_SECONDS", "300"))   # rilegge gli ultimi N s a ogni giro
    page_size = int(os.getenv("SYNC_PAGE_SIZE", "500"))
    reconcile_hours = float(os.getenv("SYNC_RECONCILE_HOURS", "24"))  # riconciliazione cancellazioni
    heartbeat_file = os.getenv("SYNC_HEARTBEAT_FILE", "/tmp/sync_heartbeat")
    flapping_window_min = int(os.getenv("FLAPPING_WINDOW_MIN", "60"))
    # minuti (ora di Roma) delle chiusure massive da escludere, separati da virgola
    mass_closure_minutes = [m.strip() for m in os.getenv("MASS_CLOSURE_MINUTES", "2026-08-20 17:42").split(",")
                            if m.strip()]
    audit_db_path = os.getenv("AUDIT_DB_PATH", "/srv/appdata/audit.db")
    site_reparto_csv = os.getenv("SITE_REPARTO_CSV", "/srv/config/site_reparto.csv")
    excluded_csv = os.getenv("EXCLUDED_TICKETS_CSV", "/srv/config/excluded_tickets.csv")

    # search option GLPI (Ticket). DA VERIFICARE con GET /apirest.php/listSearchOptions/Ticket
    so = {
        "title": _ids("GLPI_SO_TITLE", "1"),
        "id": _ids("GLPI_SO_ID", "2"),
        "request_type": _ids("GLPI_SO_REQUESTTYPE", "9"),
        "status": _ids("GLPI_SO_STATUS", "12"),
        "opened": _ids("GLPI_SO_DATE", "15"),
        "closed": _ids("GLPI_SO_CLOSEDATE", "16"),
        "solved": _ids("GLPI_SO_SOLVEDATE", "17"),
        "modified": _ids("GLPI_SO_DATE_MOD", "19"),
        "solution_type": _ids("GLPI_SO_SOLUTIONTYPE", "23"),
        "solution": _ids("GLPI_SO_SOLUTION", "24"),
        "entity": _ids("GLPI_SO_ENTITY", "80"),
        "location": _ids("GLPI_SO_LOCATION", "83"),
    }


sync_settings = SyncSettings()
