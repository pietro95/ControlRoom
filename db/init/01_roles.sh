#!/bin/sh
# Eseguito da postgres SOLO alla prima inizializzazione del volume (cartella dati vuota).
# Ruoli:
#   cr_sync    -> proprietario dello schema "reporting", unico che scrive (worker di sync)
#   grafana_ro -> SOLO SELECT su "reporting", nessun altro privilegio (Grafana)
# Le password arrivano dall'ambiente e sono passate come variabili psql (quotate da psql, niente injection).
set -eu
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v sync_pw="$SYNC_DB_PASSWORD" -v graf_pw="$GRAFANA_DB_PASSWORD" -v db="$POSTGRES_DB" \
  -f /docker-entrypoint-initdb.d/roles.psql
