"""Login contro Active Directory (bind LDAP con UPN) + rate limit semplice."""
import time
from collections import defaultdict
from dataclasses import dataclass

from ldap3 import Connection, Server, Tls, SIMPLE
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
import ssl

from .config import settings


@dataclass
class AuthUser:
    username: str
    display_name: str


class AuthError(Exception):
    pass


def authenticate(username: str, password: str) -> AuthUser:
    username = (username or "").strip().split("\\")[-1].split("@")[0]
    if not username or not password or len(username) > 64:
        raise AuthError("credenziali mancanti")

    if settings.dev_auth:  # solo sviluppo/test
        if password != "dev":
            raise AuthError("credenziali errate")
        return AuthUser(username, username.title())

    tls = Tls(validate=ssl.CERT_REQUIRED if settings.ldap_validate_tls else ssl.CERT_NONE)
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
        groups = [str(g).lower() for g in e.memberOf.values] if "memberOf" in e else []
        grp = settings.ldap_operator_group_dn.lower()
        if grp and grp not in groups:
            raise AuthError("utente non abilitato")
        name = str(e.displayName) if "displayName" in e and e.displayName.value else username
        conn.unbind()
        return AuthUser(username, name)
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
