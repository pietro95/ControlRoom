#!/bin/sh
# Eseguito da postgres SOLO alla prima inizializzazione del volume (cartella dati vuota).
# Ruoli:
#   cr_sync    -> proprietario dello schema "reporting", unico che scrive (worker di sync)
#   grafana_ro -> SOLO SELECT su "reporting", nessun altro privilegio (Grafana)
# Le password arrivano dall'ambiente e sono passate come variabili psql (quotate da psql, niente injection).
set -eu
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v sync_pw="$SYNC_DB_PASSWORD" -v graf_pw="$GRAFANA_DB_PASSWORD" -v db="$POSTGRES_DB" <<'SQL'
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE ROLE cr_sync LOGIN PASSWORD :'sync_pw';
CREATE ROLE grafana_ro LOGIN PASSWORD :'graf_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE grafana_ro SET default_transaction_read_only = on;
ALTER ROLE grafana_ro SET statement_timeout = '30s';

GRANT CONNECT ON DATABASE :"db" TO cr_sync, grafana_ro;
CREATE SCHEMA reporting AUTHORIZATION cr_sync;
GRANT USAGE ON SCHEMA reporting TO grafana_ro;
-- tutto ciò che cr_sync creerà in reporting (tabelle, viste) sarà leggibile da grafana_ro, e solo leggibile
ALTER DEFAULT PRIVILEGES FOR ROLE cr_sync IN SCHEMA reporting GRANT SELECT ON TABLES TO grafana_ro;
ALTER ROLE grafana_ro SET search_path = reporting;
ALTER ROLE cr_sync SET search_path = reporting;
SQL
