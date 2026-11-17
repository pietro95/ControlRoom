"""Login contro Active Directory (bind LDAP con UPN) + rate limit semplice."""
import time
from collections import defaultdict
from dataclasses import dataclass, field

from ldap3 import Connection, Server, Tls, SIMPLE
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
import os
import ssl

from .config import settings


OPERATOR = "operator"
MANAGER = "manager"
ROLES = (OPERATOR, MANAGER)
IN_CHAIN = "1.2.840.113556.1.4.1941"  # LDAP_MATCHING_RULE_IN_CHAIN (Active Directory)


@dataclass
class AuthUser:
    username: str
    display_name: str
    roles: list[str] = field(default_factory=list)


def role_groups() -> dict[str, str]:
    """ruolo -> DN del gruppo AD (solo i ruoli configurati)."""
    m = {OPERATOR: settings.ldap_operator_group_dn, MANAGER: settings.ldap_manager_group_dn}
    return {r: dn.strip() for r, dn in m.items() if dn and dn.strip()}


def roles_from_direct_groups(member_of: list[str]) -> list[str]:
    """Ruoli dal solo memberOf diretto (confronto DN case-insensitive)."""
    groups = {g.strip().lower() for g in member_of}
    return [r for r, dn in role_groups().items() if dn.lower() in groups]


def dev_roles_for(username: str) -> list[str]:
    for item in settings.dev_roles.split(","):
        name, _, roles = item.strip().partition(":")
        if name.strip().lower() == username.lower():
            return [r for r in ROLES if r in roles.split("+")]
    return [OPERATOR]


def _nested_roles(conn, username: str) -> list[str]:
    """Membership transitiva: una query per ruolo con la matching rule IN_CHAIN, valutata dal DC."""
    out = []
    for role, dn in role_groups().items():
        conn.search(
            settings.ldap_base_dn,
            f"(&(objectClass=user)(sAMAccountName={escape_filter_chars(username)})"
            f"(memberOf:{IN_CHAIN}:={escape_filter_chars(dn)}))",
            attributes=["sAMAccountName"],
        )
        if conn.entries:
            out.append(role)
    return out


class AuthError(Exception):
    pass


def ldap_tls() -> Tls:
    """TLS verso il DC: certificato sempre verificato (salvo LDAP_VALIDATE_TLS=false, vietato in produzione),
    contro la CA interna di LDAP_CA_CERT_FILE se impostata, altrimenti contro le CA di sistema."""
    if not settings.ldap_validate_tls:
        return Tls(validate=ssl.CERT_NONE)
    return Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=settings.ldap_ca_cert_file or None)


def check_ldap_tls_config() -> None:
    """All'avvio: un file CA configurato ma assente è un errore (come accounts.E001 di gestione-contratti)."""
    f = settings.ldap_ca_cert_file
    if not settings.dev_auth and settings.ldap_validate_tls and f and not os.path.isfile(f):
        raise RuntimeError(f"LDAP_CA_CERT_FILE non trovato: {f}")


def authenticate(username: str, password: str) -> AuthUser:
    username = (username or "").strip().split("\\")[-1].split("@")[0]
    if not username or not password or len(username) > 64:
        raise AuthError("credenziali mancanti")

    if settings.dev_auth:  # solo sviluppo/test
        if password != "dev":
            raise AuthError("credenziali errate")
        roles = dev_roles_for(username)
        if not roles:
            raise AuthError("utente non abilitato")
        return AuthUser(username, username.title(), roles)

    tls = ldap_tls()
    server = Server(settings.ldap_url, use_ssl=settings.ldap_url.startswith("ldaps"), tls=tls, connect_timeout=5)
    try:
        conn = Connection(server, user=f"{username}@{settings.ldap_domain}", password=password,
                          authentication=SIMPLE, receive_timeout=8)
        if not conn.bind():
            raise AuthError("credenziali errate")
        conn.search(
            settings.ldap_base_dn,
            f"(&(objectClass=user)(sAMAccountName={escape_filter_chars(username)}))",
            attributes=["displayName", "memberOf"],
        )
        if not conn.entries:
            raise AuthError("utente non trovato")
        e = conn.entries[0]
        name = str(e.displayName) if "displayName" in e and e.displayName.value else username
        if settings.ldap_nested_groups:
            roles = _nested_roles(conn, username)
        else:
            roles = roles_from_direct_groups([str(g) for g in e.memberOf.values] if "memberOf" in e else [])
        conn.unbind()
        if not roles:
            raise AuthError("utente non abilitato")
        return AuthUser(username, name, roles)
    except LDAPException as ex:
        raise AuthError("directory non raggiungibile") from ex


class RateLimiter:
    """Max N tentativi falliti per chiave nella finestra: blocca il brute force sul login."""

    def __init__(self, max_fail: int = 5, window: int = 300):
        self.max_fail, self.window = max_fail, window
        self._fails: dict[str, list[float]] = defaultdict(list)

    def _prune(self, key: str):
        now = time.time()
        self._fails[key] = [t for t in self._fails[key] if now - t < self.window]

    def blocked(self, key: str) -> bool:
        self._prune(key)
        return len(self._fails[key]) >= self.max_fail

    def fail(self, key: str):
        self._fails[key].append(time.time())

    def reset(self, key: str):
        self._fails.pop(key, None)
