"""Configurazione da variabili d'ambiente (vedi .env.example)."""
import os


def _b(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    # --- GLPI ---
    glpi_url = os.getenv("GLPI_URL", "https://glpi.siciliacque.it").rstrip("/")
    glpi_app_token = os.getenv("GLPI_APP_TOKEN", "")
    glpi_user_token = os.getenv("GLPI_USER_TOKEN", "")
    glpi_verify_tls = _b("GLPI_VERIFY_TLS", True)
    glpi_timeout = float(os.getenv("GLPI_TIMEOUT", "10"))
    entity_id = int(os.getenv("GLPI_ENTITY_ID", "14"))  # 13) Control Room Centralizzata
    request_type_phone = int(os.getenv("GLPI_REQUESTTYPE_PHONE", "8"))  # origine "Telefono"
    sol_rientrato = int(os.getenv("GLPI_SOLTYPE_RIENTRATO", "1"))
    sol_manovra = int(os.getenv("GLPI_SOLTYPE_MANOVRA", "2"))
    sol_altro = int(os.getenv("GLPI_SOLTYPE_ALTRO", "3"))
    # URL "nativo" mostrato nel banner di ripiego
    glpi_public_url = os.getenv("GLPI_PUBLIC_URL", glpi_url)

    # --- Autenticazione ---
    dev_auth = _b("DEV_AUTH", False)  # SOLO sviluppo: accetta qualunque utente con password "dev"
    ldap_url = os.getenv("LDAP_URL", "ldaps://dc.siciliacque.local")
    ldap_domain = os.getenv("LDAP_DOMAIN", "siciliacque.local")  # per bind UPN utente@dominio
    ldap_base_dn = os.getenv("LDAP_BASE_DN", "DC=siciliacque,DC=local")
    ldap_operator_group_dn = os.getenv("LDAP_OPERATOR_GROUP_DN", "")  # vuoto = qualunque utente AD
    ldap_validate_tls = _b("LDAP_VALIDATE_TLS", True)
    session_secret = os.getenv("SESSION_SECRET", "")
    session_hours = int(os.getenv("SESSION_HOURS", "12"))
    cookie_secure = _b("COOKIE_SECURE", True)

    # --- Storage ---
    db_path = os.getenv("DB_PATH", "data/audit.db")

    # --- UI ---
    refresh_seconds = int(os.getenv("REFRESH_SECONDS", "15"))
    flapping_threshold = int(os.getenv("FLAPPING_THRESHOLD", "3"))


settings = Settings()
