"""Verifica del certificato del DC con la CA interna (LDAP_CA_CERT_FILE), su un vero handshake TLS.

Un server TLS locale presenta un certificato firmato da una CA di prova: con quella CA l'handshake riesce
(poi il "DC" chiude la connessione, perché non parla LDAP); senza, o con un'altra CA, è rifiutato.
"""
import socket
import ssl
import subprocess
import threading

import pytest
from ldap3 import IP_V4_ONLY, Connection, Server
from ldap3.core.exceptions import LDAPException

from app import auth
from app.config import settings
from app.main import create_app


def _openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    for name in ("ca", "altra-ca"):
        # CA conforme (Python >= 3.13 verifica in modo stretto: serve keyUsage sulla CA)
        _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", f"/CN={name}",
                 "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                 "-keyout", f"{name}.key", "-out", f"{name}.pem", cwd=d)
    # come in produzione: ci si collega per nome DNS (ldap3 confronta il nome dell'URL con i SAN DNS)
    (d / "san.cnf").write_text("subjectAltName=DNS:localhost\n")
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost", "-keyout", "dc.key", "-out", "dc.csr", cwd=d)
    _openssl("x509", "-req", "-in", "dc.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-days", "2",
             "-extfile", "san.cnf", "-out", "dc.pem", cwd=d)
    return d


@pytest.fixture()
def fake_dc(pki):
    """Server TLS che completa l'handshake e chiude: registra se l'handshake è riuscito."""
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(pki / "dc.pem", pki / "dc.key")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    state = {"handshakes": 0, "stop": False}

    def loop():
        srv.settimeout(0.2)
        while not state["stop"]:
            try:
                c, _ = srv.accept()
            except OSError:
                continue
            try:
                with ctx.wrap_socket(c, server_side=True) as s:
                    state["handshakes"] += 1
                    s.recv(1)
            except (ssl.SSLError, OSError):
                pass

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    yield srv.getsockname()[1], state
    state["stop"] = True
    t.join(2)
    srv.close()


def _try(port, monkeypatch, ca: str | None):
    monkeypatch.setattr(settings, "ldap_validate_tls", True)
    monkeypatch.setattr(settings, "ldap_ca_cert_file", ca or "")
    server = Server(f"ldaps://localhost:{port}", use_ssl=True, tls=auth.ldap_tls(), connect_timeout=3, mode=IP_V4_ONLY)
    try:
        Connection(server, user="x@y", password="z", receive_timeout=3, auto_bind=True)
    except LDAPException as e:
        return str(e)
    return ""


def test_handshake_ok_with_internal_ca(fake_dc, pki, monkeypatch):
    port, state = fake_dc
    err = _try(port, monkeypatch, str(pki / "ca.pem"))
    assert state["handshakes"] == 1, err          # certificato accettato
    assert "certificate verify failed" not in err


def test_rejected_without_internal_ca(fake_dc, monkeypatch):
    port, state = fake_dc
    err = _try(port, monkeypatch, None)            # solo CA di sistema
    assert state["handshakes"] == 0 and "certificate verify failed" in err.lower()


def test_rejected_with_wrong_ca(fake_dc, pki, monkeypatch):
    port, state = fake_dc
    err = _try(port, monkeypatch, str(pki / "altra-ca.pem"))
    assert state["handshakes"] == 0 and "certificate verify failed" in err.lower()


def test_validate_false_accepts_anything(fake_dc, monkeypatch):
    """Il motivo per cui LDAP_VALIDATE_TLS=false è vietato: qualunque certificato viene accettato."""
    port, state = fake_dc
    monkeypatch.setattr(settings, "ldap_validate_tls", False)
    server = Server(f"ldaps://localhost:{port}", use_ssl=True, tls=auth.ldap_tls(), connect_timeout=3, mode=IP_V4_ONLY)
    with pytest.raises(LDAPException):
        Connection(server, user="x@y", password="z", receive_timeout=3, auto_bind=True)
    assert state["handshakes"] == 1


def test_app_refuses_to_start_with_missing_ca_file(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "dev_auth", False)
    monkeypatch.setattr(settings, "session_secret", "x" * 32)
    monkeypatch.setattr(settings, "ldap_validate_tls", True)
    monkeypatch.setattr(settings, "ldap_ca_cert_file", str(tmp_path / "manca.pem"))
    with pytest.raises(RuntimeError, match="LDAP_CA_CERT_FILE non trovato"):
        create_app()
