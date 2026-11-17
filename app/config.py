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
    status_waiting = int(os.getenv("GLPI_STATUS_WAITING", "4"))  # "In attesa"
    status_resume = int(os.getenv("GLPI_STATUS_RESUME", "2"))  # ripresa dall'attesa: "In carico (assegnato)"
    # URL "nativo" mostrato nel banner di ripiego
    glpi_public_url = os.getenv("GLPI_PUBLIC_URL", glpi_url)

    # --- Autenticazione ---
    dev_auth = _b("DEV_AUTH", False)  # SOLO sviluppo: accetta qualunque utente con password "dev"
    ldap_url = os.getenv("LDAP_URL", "ldaps://dc.siciliacque.local")
    ldap_domain = os.getenv("LDAP_DOMAIN", "siciliacque.local")  # per bind UPN utente@dominio
    ldap_base_dn = os.getenv("LDAP_BASE_DN", "DC=siciliacque,DC=local")
    # Ruoli dai gruppi AD (DN completi). Un DN vuoto = nessuno riceve quel ruolo.
    ldap_operator_group_dn = os.getenv("LDAP_OPERATOR_GROUP_DN", "")
    ldap_manager_group_dn = os.getenv("LDAP_MANAGER_GROUP_DN", "")
    # true = membership transitiva (gruppi annidati) via LDAP_MATCHING_RULE_IN_CHAIN; false = solo memberOf diretto
    ldap_nested_groups = _b("LDAP_NESTED_GROUPS", True)
    # SOLO sviluppo: ruoli per utente con DEV_AUTH, es. "mbianchi:manager,capo:operator+manager" (default operator)
    dev_roles = os.getenv("DEV_ROLES", "")
    ldap_validate_tls = _b("LDAP_VALIDATE_TLS", True)
    # certificato (PEM) della CA interna che firma il certificato del DC; vuoto = CA di sistema dell'immagine
    ldap_ca_cert_file = os.getenv("LDAP_CA_CERT_FILE", "").strip()
    session_secret = os.getenv("SESSION_SECRET", "")
    session_hours = int(os.getenv("SESSION_HOURS", "12"))
    cookie_secure = _b("COOKIE_SECURE", True)

    # --- Grafana (solo rete interna; raggiunto attraverso il proxy /grafana/ del portale) ---
    grafana_url = os.getenv("GRAFANA_URL", "http://grafana:3000").rstrip("/")
    grafana_timeout = float(os.getenv("GRAFANA_TIMEOUT", "30"))
    # admin locale di Grafana: mai raggiungibile via proxy (un utente AD omonimo non diventa admin)
    grafana_admin_user = os.getenv("GRAFANA_ADMIN_USER", "cr-grafana-admin")

    # --- Storage ---
    db_path = os.getenv("DB_PATH", "data/audit.db")

    # --- UI ---
    refresh_seconds = int(os.getenv("REFRESH_SECONDS", "15"))
    flapping_threshold = int(os.getenv("FLAPPING_THRESHOLD", "3"))
    # coda condivisa fra operatori e azioni: riletta da GLPI al massimo ogni N secondi (0 = sempre);
    # ogni scrittura dal portale la invalida subito
    queue_cache_seconds = float(os.getenv("QUEUE_CACHE_SECONDS", "5"))
    # richieste GLPI in parallelo per singola operazione (risoluzione nomi, scritture su più ticket, storico)
    glpi_concurrency = int(os.getenv("GLPI_CONCURRENCY", "6"))


settings = Settings()
