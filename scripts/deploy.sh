#!/usr/bin/env bash
# Deploy di Control Room sul server Docker condiviso. Stesse convenzioni di gestione-contratti:
# tutto sotto una cartella, codice in app/ mai modificato a mano, configurazione in config/, versioni = tag git,
# progetto Compose fisso "controlroom" (non tocca altri container), porta non standard dal .env.
#
#   scripts/deploy.sh <comando>
#
#   prepare             cartelle e permessi, clone (se manca), CSV, config/.env con segreti generati (se manca)
#   install <versione>  backup se già installato -> checkout del tag -> controlli -> build -> avvio -> attesa salute
#                       (riavvia anche il sync se era attivo e la configurazione GLPI non è cambiata)
#   check-glpi          verifica sul GLPI reale delle search option GLPI_SO_* (sola lettura); se OK abilita "sync"
#   sync                avvia il worker di sync (solo dopo check-glpi OK) e mostra stato ed esclusioni
#   status              versione, servizi, ultima sincronizzazione
#   backup [etichetta]  pg_dump + archivio di audit, grafana, config (permessi 600) in backup/
#   restore <file.dump> backup di sicurezza -> sync fermato -> pg_restore del dump nel database -> sync riavviato
#   logs [servizio]     ultime 200 righe dei log dei container
#
# In esercizio si usa il comando "controlroom" (scripts/controlroom.sh), che gira come utente di servizio.
#
# Struttura (CR_BASE, default /appdata/controlroom):
#   app/      checkout git (deploy key in sola lettura, vedi docs/RUNBOOK.md) config/   .env (600), override, certs/ (700)
#   csv/      mapping sito->reparto ed esclusioni, modificabili a mano  backup/   dump e archivi (700)
#   data/     audit (uid 10001), postgres (70), grafana (472)
#
# Variabili: CR_BASE; CR_REPO_URL (default github-controlroom:pietro95/ControlRoom.git, alias SSH della deploy key);
#            CR_COMPOSE_EXTRA (solo test: file compose aggiuntivo).
set -euo pipefail

BASE="${CR_BASE:-/appdata/controlroom}"
REPO_URL="${CR_REPO_URL:-github-controlroom:pietro95/ControlRoom.git}"
APP="$BASE/app"
DATA="$BASE/data"
CONF="$BASE/config"
CSV="$BASE/csv"
CERTS="$CONF/certs"
CERTS_IN_CONTAINER=/etc/controlroom/certs
GC_CA=/appdata/gestione-contratti/config/certs/ca-interna.pem
ENV_FILE="$CONF/.env"
OVERRIDE="$CONF/docker-compose.override.yml"
MARKER="$CONF/.glpi_checked"
PROGETTO=controlroom
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33m!!\033[0m  %s\n' "$*"; }
die()  { printf '  \033[31mKO\033[0m  %s\n' "$*" >&2; exit 1; }

# Valore di una variabile del .env senza eseguirlo (i DN LDAP contengono spazi). Vuoto se assente.
envval() { { grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | tail -1 | cut -d= -f2- \
           | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//; s/^"//; s/"$//'; }
so_hash() { grep -E '^GLPI_(SO_|URL=|ENTITY_ID=)' "$ENV_FILE" | sort | sha256sum | cut -d' ' -f1; }
version() { git -C "$APP" describe --tags --always 2>/dev/null || echo local; }

# Docker Compose limitato a questo progetto, con .env e override fuori dal checkout.
compose() {
  local extra=()
  [ -n "${CR_COMPOSE_EXTRA:-}" ] && extra=(-f "$CR_COMPOSE_EXTRA")
  CR_ENV_FILE="$ENV_FILE" APP_VERSION="${APP_VERSION:-$(version)}" \
    docker compose --project-name "$PROGETTO" --project-directory "$APP" --env-file "$ENV_FILE" \
      -f "$APP/docker-compose.yml" -f "$OVERRIDE" "${extra[@]}" "$@"
}
running() { [ -n "$(compose ps --quiet "$1" 2>/dev/null)" ]; }

# ------------------------------------------------------------------ prepare
cmd_prepare() {
  say "Cartelle in $BASE"
  $SUDO mkdir -p "$DATA/audit" "$DATA/postgres" "$DATA/grafana" "$CONF" "$CSV" "$BASE/backup"
  $SUDO chown "$(id -u):$(id -g)" "$BASE" "$CONF" "$CSV" "$BASE/backup"
  chmod 700 "$CONF" "$BASE/backup"
  chmod 755 "$CSV"   # letto dal container del sync (uid 10001): niente segreti qui
  mkdir -p "$CERTS" && chmod 755 "$CERTS"   # CA interna (pubblica), letta dal container dell'app
  # ogni container scrive con il proprio utente: app 10001, grafana 472, postgres 70 (immagine alpine)
  $SUDO chown 10001:10001 "$DATA/audit"
  $SUDO chown 472:0 "$DATA/grafana"
  $SUDO chown 70:70 "$DATA/postgres" && $SUDO chmod 700 "$DATA/postgres"
  ok "data/audit (10001), data/grafana (472), data/postgres (70, 700), config (700), csv (755), backup (700)"

  say "Codice in $APP"
  if [ -d "$APP/.git" ]; then
    ok "repository già presente ($(version))"
  else
    git clone "$REPO_URL" "$APP" || die "clone fallito: deploy key e alias SSH configurati? (docs/RUNBOOK.md, §2)"
    ok "clonato ($(version))"
  fi

  say "CSV in $CSV e configurazione in $CONF"
  for f in site_reparto.csv excluded_tickets.csv; do
    if [ -f "$CSV/$f" ]; then ok "$f presente (non toccato)"; else cp "$APP/config/$f" "$CSV/$f" && chmod 644 "$CSV/$f"; ok "$f creato"; fi
  done
  if [ -f "$ENV_FILE" ]; then
    ok ".env presente (non toccato)"
  else
    cp "$APP/.env.example" "$ENV_FILE" && chmod 600 "$ENV_FILE"
    for k in SESSION_SECRET POSTGRES_PASSWORD SYNC_DB_PASSWORD GRAFANA_DB_PASSWORD GRAFANA_ADMIN_PASSWORD; do
      sed -i "s|^$k=.*|$k=$(openssl rand -hex 24)|" "$ENV_FILE"
    done
    ok ".env creato (600) con segreti generati"
  fi
  say "CA interna per LDAPS in $CERTS"
  if [ -n "$(ls -A "$CERTS" 2>/dev/null)" ]; then
    ok "presente: $(find "$CERTS" -maxdepth 1 -type f -printf '%f ')"
  elif [ -f "$GC_CA" ]; then
    cp "$GC_CA" "$CERTS/ca-interna.pem" && chmod 644 "$CERTS/ca-interna.pem"
    ok "copiata da gestione-contratti ($GC_CA): verificare che sia la CA che firma il DC"
  else
    warn "nessun certificato: copiare la CA interna (PEM) in $CERTS/ca-interna.pem"
  fi
  write_override

  say "Da fare a mano: compilare $ENV_FILE"
  echo "  APP_BIND e APP_PORT (porta libera; con Nginx Proxy Manager in container APP_BIND=172.17.0.1),"
  echo "  GLPI_URL, GLPI_APP_TOKEN, GLPI_USER_TOKEN, LDAP_URL, LDAP_OPERATOR_GROUP_DN, LDAP_MANAGER_GROUP_DN,"
  echo "  PORTAL_PUBLIC_URL (https://<nome DNS del portale>). Poi: $APP/scripts/deploy.sh install <versione>"
}

write_override() {
  cat > "$OVERRIDE" <<EOF
# Generato da scripts/deploy.sh: tutti i dati sotto $BASE. Non modificare a mano.
services:
  app:
    volumes: !override
      - $DATA/audit:/srv/data
      - $CERTS:$CERTS_IN_CONTAINER:ro
  sync:
    volumes: !override
      - $DATA/audit:/srv/appdata:ro
      - $CSV:/srv/config:ro
  postgres:
    volumes: !override
      - $DATA/postgres:/var/lib/postgresql/data
      - ./db/init:/docker-entrypoint-initdb.d:ro
  grafana:
    volumes: !override
      - $DATA/grafana:/var/lib/grafana
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
      - ./grafana/dashboards:/var/lib/grafana/dashboards:ro
EOF
  ok "override generato in $OVERRIDE"
}

# ------------------------------------------------------------------ controlli
# Porta già in ascolto sull'host? ss se c'è, altrimenti un tentativo di connessione.
port_in_use() {
  if command -v ss >/dev/null; then
    ss -ltnH "( sport = :$2 )" | grep -q .
  else
    (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null
  fi
}

preflight() {
  say "Controlli"
  command -v docker >/dev/null || die "docker non installato"
  [ -f "$ENV_FILE" ] || die "manca $ENV_FILE: eseguire prima 'prepare'"
  write_override >/dev/null   # file derivato: sempre allineato alla versione dello script

  local v; v="$(docker compose version --short 2>/dev/null | sed 's/^v//')" || die "manca il plugin docker compose"
  if [ "$(printf '%s\n2.24.0\n' "$v" | sort -V | head -1)" != "2.24.0" ]; then die "docker compose $v: serve >= 2.24 (tag !override)"; fi
  ok "docker compose $v"

  local missing=() k
  for k in APP_BIND APP_PORT GLPI_URL GLPI_APP_TOKEN GLPI_USER_TOKEN LDAP_URL LDAP_OPERATOR_GROUP_DN LDAP_MANAGER_GROUP_DN \
           PORTAL_PUBLIC_URL SESSION_SECRET POSTGRES_PASSWORD SYNC_DB_PASSWORD GRAFANA_DB_PASSWORD GRAFANA_ADMIN_PASSWORD; do
    [ -n "$(envval "$k")" ] || missing+=("$k")
  done
  [ ${#missing[@]} -eq 0 ] || die "variabili vuote in $ENV_FILE: ${missing[*]}"
  ok "variabili obbligatorie presenti"

  if grep -qE '^DEV_AUTH=(1|true|yes|on)' "$ENV_FILE"; then die "DEV_AUTH attivo: vietato in produzione"; fi
  if grep -qE '^DEV_ROLES=.+' "$ENV_FILE"; then die "DEV_ROLES valorizzato: vietato in produzione"; fi
  if grep -qiE '^COOKIE_SECURE=(0|false|no|off)' "$ENV_FILE"; then die "COOKIE_SECURE disattivato"; fi
  if grep -qiE '^LDAP_VALIDATE_TLS=(0|false|no|off)' "$ENV_FILE"; then
    die "LDAP_VALIDATE_TLS disattivato: le password AD sarebbero esposte a un DC falso. Usare LDAP_CA_CERT_FILE"
  fi
  local ca; ca="$(envval LDAP_CA_CERT_FILE)"
  if [ -n "$ca" ]; then
    [[ "$ca" == "$CERTS_IN_CONTAINER/"* ]] || die "LDAP_CA_CERT_FILE deve stare in $CERTS_IN_CONTAINER/ (montata da $CERTS)"
    [ -f "$CERTS/${ca#"$CERTS_IN_CONTAINER"/}" ] || die "manca $CERTS/${ca#"$CERTS_IN_CONTAINER"/}: copiare lì la CA interna (PEM) che firma il DC"
    ok "CA interna per LDAPS: $CERTS/${ca#"$CERTS_IN_CONTAINER"/}"
  elif [[ "$(envval LDAP_URL)" == ldaps://* ]]; then
    warn "LDAP_CA_CERT_FILE vuoto: il certificato del DC sarà verificato solo contro le CA pubbliche dell'immagine"
  fi
  [[ "$(envval SYNC_DB_PASSWORD)" =~ ^[A-Za-z0-9]+$ ]] || die "SYNC_DB_PASSWORD deve contenere solo [A-Za-z0-9] (finisce in un URL)"
  [[ "$(envval PORTAL_PUBLIC_URL)" == https://* ]] || warn "PORTAL_PUBLIC_URL non è https: i cookie Secure non funzioneranno"
  if [ "$(stat -c %a "$ENV_FILE")" != "600" ]; then chmod 600 "$ENV_FILE"; warn ".env reso 600"; fi
  ok "nessuna impostazione di sviluppo attiva"

  local port bind; port="$(envval APP_PORT)"; bind="$(envval APP_BIND)"
  [[ "$port" =~ ^[0-9]+$ ]] || die "APP_PORT non numerica"
  if ! running app && port_in_use "$bind" "$port"; then
    die "la porta $port (APP_PORT) è già usata da un altro servizio: sceglierne un'altra"
  fi
  ok "porta $bind:$port disponibile"
  [ "$bind" != "127.0.0.1" ] || warn "APP_BIND=127.0.0.1: un Nginx Proxy Manager in container non lo raggiunge (di solito 172.17.0.1)"

  if ! docker network inspect "${PROGETTO}_grafana_net" >/dev/null 2>&1 && ip route 2>/dev/null | grep -q '^172\.30\.99\.'; then
    die "la subnet 172.30.99.0/28 (grafana_net) è già instradata su questo host: cambiarla in docker-compose.yml"
  fi
  ok "subnet interna libera"

  [ -z "$(git -C "$APP" status --porcelain --untracked-files=no)" ] \
    || die "modifiche locali in $APP: il codice sul server non si modifica a mano"
}

wait_healthy() {
  local svc s
  for svc in "$@"; do
    for _ in $(seq 1 60); do
      s="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
           "$(compose ps -q "$svc")" 2>/dev/null || echo missing)"
      [ "$s" = "healthy" ] && break
      [ "$s" = "unhealthy" ] && { compose logs --tail 30 "$svc"; die "$svc unhealthy"; }
      sleep 5
    done
    [ "$s" = "healthy" ] || { compose logs --tail 30 "$svc"; die "$svc non healthy dopo 5 minuti (stato: $s)"; }
    ok "$svc healthy"
  done
}

# ------------------------------------------------------------------ install
cmd_install() {
  [ $# -eq 1 ] || die "uso: deploy.sh install <versione>  (tag git, es. v1.0.0; oppure un commit)"
  local richiesta="$1" sync_on=false
  [ -d "$APP/.git" ] || die "manca $APP: eseguire prima 'prepare'"
  running sync && sync_on=true

  if running postgres; then
    say "Backup pre-aggiornamento"
    cmd_backup "pre_${richiesta//[^A-Za-z0-9.]/_}"
  fi

  say "Codice: $richiesta"
  [ -z "$(git -C "$APP" status --porcelain --untracked-files=no)" ] \
    || die "modifiche locali in $APP: il codice sul server non si modifica a mano"
  git -C "$APP" fetch --tags --quiet origin
  git -C "$APP" -c advice.detachedHead=false checkout --quiet "$richiesta"
  APP_VERSION="$(version)"; export APP_VERSION
  ok "versione $APP_VERSION"

  preflight
  say "Build dell'immagine controlroom:$APP_VERSION"
  compose build --pull app || die "build fallita (vedi sopra)"
  say "Avvio"
  local servizi=(app postgres grafana)
  if $sync_on; then
    if [ "$(cat "$MARKER" 2>/dev/null)" = "$(so_hash)" ]; then servizi+=(sync)
    else warn "configurazione GLPI cambiata dopo check-glpi: sync fermato, rieseguire check-glpi e sync"; compose stop sync; fi
  fi
  compose up -d --no-build "${servizi[@]}" || die "avvio fallito (vedi sopra)"
  wait_healthy postgres app grafana
  local bind port; bind="$(envval APP_BIND)"; port="$(envval APP_PORT)"
  curl -fsS --max-time 5 "http://$bind:$port/healthz" >/dev/null || die "il portale non risponde su http://$bind:$port/healthz"
  ok "portale raggiungibile su http://$bind:$port"
  # pulizia: solo immagini di questo progetto, tenendo l'attuale e la precedente
  docker image ls controlroom --format '{{.Tag}}' | grep -vxF -e "$APP_VERSION" -e local \
    | tail -n +2 | sed 's/^/controlroom:/' | xargs -r docker image rm >/dev/null 2>&1 || true
  echo
  echo "  Installata la versione $APP_VERSION."
  $sync_on || echo "  Primo deploy: provare il login AD (operatore, manager, entrambi, nessuno), poi 'check-glpi' e 'sync'."
}

# ------------------------------------------------------------------ check-glpi / sync
cmd_check_glpi() {
  preflight
  say "Verifica search option sul GLPI reale (sola lettura)"
  rm -f "$MARKER"
  if compose run --rm --no-deps -T sync python -m sync.check_glpi; then
    so_hash > "$MARKER"
    ok "configurazione GLPI verificata: ora 'deploy.sh sync'"
  else
    die "correggere GLPI_SO_* / GLPI_URL / token in $ENV_FILE e rieseguire check-glpi"
  fi
}

cmd_sync() {
  preflight
  [ -f "$MARKER" ] || die "eseguire prima 'check-glpi' (deve terminare con ESITO: OK)"
  [ "$(cat "$MARKER")" = "$(so_hash)" ] || die "le impostazioni GLPI sono cambiate dopo check-glpi: rieseguirlo"
  wait_healthy postgres
  say "Avvio del worker di sync"
  local since line=""; since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  compose up -d --no-build sync || die "avvio del sync fallito (vedi sopra)"
  for _ in $(seq 1 60); do
    line="$(compose logs --since "$since" sync 2>/dev/null | grep -E "sync: \{" | tail -1 || true)"
    [ -n "$line" ] && break
    sleep 5
  done
  [ -n "$line" ] || { compose logs --tail 40 sync; die "nessun giro di sync completato in 5 minuti"; }
  echo "  $line"
  echo "$line" | grep -q "'ok': True" || die "il primo giro di sync è fallito (vedi errore sopra)"
  ok "primo giro completato"
  cmd_status
  say "Esclusioni (controllare a mano il numero della chiusura massiva)"
  compose exec -T postgres psql -U postgres -d controlroom -c \
    "SELECT reason, count(*) FROM reporting.excluded_ticket GROUP BY 1 ORDER BY 1"
}

# ------------------------------------------------------------------ status / backup
cmd_status() {
  say "Versione installata: $(version)"
  compose ps
  say "Ultima sincronizzazione"
  compose exec -T postgres psql -U postgres -d controlroom -c \
    "SELECT last_success_at, last_attempt_at, last_error, ticket_in_db, dataset_label FROM reporting.v_sync_status" \
    2>/dev/null || warn "postgres non raggiungibile o schema non ancora creato (il sync non è mai partito)"
}

cmd_backup() {
  local ts out label; ts="$(date +%Y.%m.%d_%H%M)"; out="$BASE/backup"; label="${1:+_$1}"
  mkdir -p "$out" && chmod 700 "$out"
  say "Backup in $out"
  running postgres || die "postgres non in esecuzione: niente da salvare"
  (umask 077 && compose exec -T postgres pg_dump -U postgres -Fc controlroom > "$out/${ts}${label}_controlroom.dump")
  ok "${ts}${label}_controlroom.dump ($(du -h "$out/${ts}${label}_controlroom.dump" | cut -f1))"
  # data/audit (10001) e data/grafana (472) non sono leggibili dall'utente di servizio: senza root il tar gira in un
  # container usa e getta dell'immagine installata, senza rete, con i dati montati in sola lettura.
  if [ "$(id -u)" -eq 0 ]; then
    (umask 077 && tar czf "$out/${ts}${label}_files.tgz" -C "$BASE" data/audit data/grafana config csv)
  else
    (umask 077 && docker run --rm --network none --user 0:0 -v "$BASE:/b:ro" -w /b --entrypoint tar \
       "controlroom:$(version)" czf - data/audit data/grafana config csv > "$out/${ts}${label}_files.tgz") \
      || die "archivio dei file fallito (immagine controlroom:$(version) presente?)"
  fi
  chmod 600 "$out/${ts}${label}_files.tgz"
  ok "${ts}${label}_files.tgz (contiene config/.env con i segreti)"
}

# ------------------------------------------------------------------ restore / logs
# Solo il database di reporting (ricostruibile comunque dal sync). L'archivio _files.tgz si ripristina a mano.
cmd_restore() {
  [ $# -eq 1 ] || die "uso: deploy.sh restore <file.dump in backup/>"
  local dump="$1" sync_on=false
  [[ "$dump" == /* ]] || dump="$BASE/backup/$dump"
  [ -f "$dump" ] || die "dump non trovato: $dump"
  [[ "$dump" == *.dump ]] || die "serve un file .dump (pg_dump -Fc)"
  running postgres || die "postgres non in esecuzione"
  cmd_backup pre_ripristino
  running sync && sync_on=true
  if $sync_on; then compose stop sync; fi
  say "Ripristino di $(basename "$dump")"
  compose exec -T postgres pg_restore -U postgres -d controlroom --clean --if-exists --single-transaction < "$dump" \
    || die "pg_restore fallito: database invariato (transazione unica); backup pre_ripristino in $BASE/backup"
  ok "database ripristinato"
  if $sync_on; then compose up -d --no-build sync; ok "sync riavviato"; fi
}

cmd_logs() { compose logs --tail 200 ${1:+"$1"}; }

# Tutto dentro main, chiamata con "; exit $?" sulla STESSA riga: bash legge l'intera riga (e quindi main) prima di
# eseguirla, ed esce senza leggere altro dal file. Bash legge gli script a mano a mano: se questo file venisse
# riscritto SUL POSTO mentre gira (cp sopra il file, editor), senza questa riga riprenderebbe a leggere il contenuto
# nuovo alla vecchia posizione. Il checkout di "install" non lo fa (git crea un file nuovo e bash continua a leggere
# il vecchio), ma la protezione costa nulla. Non aggiungere nulla dopo l'ultima riga. (tests/test_deploy_selfupdate.py)
main() {
  case "${1:-}" in
    prepare)    cmd_prepare ;;
    install)    shift; cmd_install "$@" ;;
    check-glpi) cmd_check_glpi ;;
    sync)       cmd_sync ;;
    status)     cmd_status ;;
    backup)     shift; cmd_backup "${1:-}" ;;
    restore)    shift; cmd_restore "$@" ;;
    logs)       shift; cmd_logs "${1:-}" ;;
    *) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 2 ;;
  esac
}

main "$@"; exit $?
