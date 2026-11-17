"""Ambiente di test: impostato prima di qualunque import di app.* (Settings legge l'ambiente all'import)."""
import os
import tempfile

os.environ.update(DEV_AUTH="1", GLPI_APP_TOKEN="a", GLPI_USER_TOKEN="u",
                  DB_PATH=os.path.join(tempfile.mkdtemp(), "audit.db"))


# ---------------------------------------------------------------- PostgreSQL reale per i test di reporting
import glob
import shutil
import socket
import subprocess
import time
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pg_bin() -> str | None:
    if os.getenv("PG_BIN"):
        return os.getenv("PG_BIN")
    found = sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb")) or ([shutil.which("initdb")] if shutil.which("initdb") else [])
    return os.path.dirname(found[-1]) if found else None


def _as_postgres(cmd: list[str]) -> list[str]:
    # initdb/postgres rifiutano di girare come root
    return ["runuser", "-u", "postgres", "--"] + cmd if os.geteuid() == 0 else cmd


@pytest.fixture(scope="session")
def pg_cluster():
    """Cluster PostgreSQL temporaneo (trust su 127.0.0.1) con ruoli e schema da db/init/roles.psql.
    Restituisce la DSN base del database modello 'crtemplate'."""
    bindir = _pg_bin()
    if not bindir:
        pytest.skip("PostgreSQL non installato: imposta PG_BIN per eseguire i test di reporting")
    data = tempfile.mkdtemp(prefix="crpg_")
    if os.geteuid() == 0:
        shutil.chown(data, "postgres")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.run(_as_postgres([f"{bindir}/initdb", "-D", data, "-A", "trust", "-U", "postgres", "--no-sync",
                                 "-E", "UTF8", "--locale=C"]), check=True, capture_output=True)
    subprocess.run(_as_postgres([f"{bindir}/pg_ctl", "-D", data, "-l", f"{data}/log", "-w", "start",
                                 "-o", f"-p {port} -c listen_addresses=127.0.0.1 -k {data} -c fsync=off "
                                       f"-c timezone=UTC"]), check=True, capture_output=True)
    psql = shutil.which("psql") or f"{bindir}/psql"
    base = ["-h", "127.0.0.1", "-p", str(port), "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-q"]
    subprocess.run([psql, *base, "-d", "postgres", "-c", "CREATE DATABASE crtemplate"], check=True)
    subprocess.run([psql, *base, "-d", "crtemplate", "-v", "db=crtemplate", "-v", "sync_pw=x", "-v", "graf_pw=y",
                    "-f", os.path.join(ROOT, "db/init/roles.psql")], check=True)
    yield {"port": port, "psql": psql}
    subprocess.run(_as_postgres([f"{bindir}/pg_ctl", "-D", data, "-m", "immediate", "stop"]), capture_output=True)
    shutil.rmtree(data, ignore_errors=True)


@pytest.fixture()
def db(pg_cluster):
    """Database nuovo per ogni test (clone del modello) con schema reporting applicato da cr_sync.
    Restituisce una connessione psycopg come cr_sync (autocommit), con .dsn_for(ruolo)."""
    import psycopg
    from sync.worker import apply_schema

    name = "t_" + uuid.uuid4().hex[:10]
    port = pg_cluster["port"]
    with psycopg.connect(f"host=127.0.0.1 port={port} user=postgres dbname=postgres", autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}" TEMPLATE crtemplate')
        c.execute(f'GRANT CONNECT ON DATABASE "{name}" TO cr_sync, grafana_ro')
    dsn = lambda role: f"host=127.0.0.1 port={port} user={role} dbname={name}"  # noqa: E731
    conn = psycopg.connect(dsn("cr_sync"), autocommit=True)
    conn.execute("SET search_path = reporting")
    apply_schema(conn)
    conn.dsn_for = dsn
    yield conn
    conn.close()
    with psycopg.connect(f"host=127.0.0.1 port={port} user=postgres dbname=postgres", autocommit=True) as c:
        c.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
