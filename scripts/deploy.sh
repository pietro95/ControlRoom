#!/usr/bin/env bash
# Deploy di Control Room su un host Docker, con TUTTI i dati sotto una cartella (default /appdata/controlroom).
#
#   scripts/deploy.sh <comando>
#
#   prepare     cartelle e permessi, clone del repo (se manca), CSV di config, .env con segreti generati (se manca)
#   up          controlli del .env e dell'host, build, avvio di app + postgres + grafana, attesa "healthy"
#   check-glpi  verifica sul GLPI reale delle search option GLPI_SO_* (sola lettura); se tutto OK abilita "sync"
#   sync        avvia il worker di sync (solo dopo check-glpi OK) e mostra stato ed esclusioni
#   status      stato dei servizi e ultima sincronizzazione
#   backup      pg_dump del database + archivio di audit, grafana, config e .env
#   update      git pull (fast-forward), rebuild e riavvio
#
# Ordine al primo deploy:  prepare -> (compilare .env) -> up -> check-glpi -> sync
# Idempotente: ogni comando si può rieseguire. Non sovrascrive mai .env, CSV o dati esistenti.
#
# Variabili:  CR_BASE (default /appdata/controlroom)
#             CR_REPO_URL (default git@github.com:pietro95/ControlRoom.git; per il clone serve l'agent SSH,
#                          es. "ssh -A" dal PC con 1Password: nessuna chiave viene salvata sul server)
set -euo pipefail

BASE="${CR_BASE:-/appdata/controlroom}"
REPO_URL="${CR_REPO_URL:-git@github.com:pietro95/ControlRoom.git}"
APP="$BASE/app"
DATA="$BASE/data"
CONF="$BASE/config"
MARKER="$APP/.glpi_checked"
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33m!!\033[0m  %s\n' "$*"; }
die()  { printf '  \033[31mKO\033[0m  %s\n' "$*" >&2; exit 1; }

compose() { (cd "$APP" && docker compose "$@"); }
envval()  { grep -E "^$1=" "$APP/.env" | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//'; }
so_hash() { grep -E '^GLPI_(SO_|URL=|ENTITY_ID=)' "$APP/.env" | sort | sha256sum | cut -d' ' -f1; }

# ------------------------------------------------------------------ prepare
cmd_prepare() {
  say "Cartelle in $BASE"
  $SUDO mkdir -p "$DATA/audit" "$DATA/postgres" "$DATA/grafana" "$CONF"
  $SUDO chown "$(id -u):$(id -g)" "$BASE" "$CONF"
  # ogni container scrive con il proprio utente: app 10001, grafana 472, postgres 70 (immagine alpine)
  $SUDO chown 10001:10001 "$DATA/audit"
  $SUDO chown 472:0 "$DATA/grafana"
  $SUDO chown 70:70 "$DATA/postgres" && $SUDO chmod 700 "$DATA/postgres"
  ok "data/audit (10001), data/grafana (472), data/postgres (70, 700), config"

  say "Codice in $APP"
  if [ -d "$APP/.git" ]; then
    ok "repository già presente ($(git -C "$APP" rev-parse --short HEAD)): per aggiornarlo usare 'update'"
  else
    [ -n "${SSH_AUTH_SOCK:-}" ] || warn "nessun agent SSH: collegarsi con 'ssh -A' per clonare senza salvare chiavi"
    git clone "$REPO_URL" "$APP"
    ok "clonato $(git -C "$APP" rev-parse --short HEAD)"
  fi

  say "CSV di configurazione in $CONF"
  for f in site_reparto.csv excluded_tickets.csv; do
    if [ -f "$CONF/$f" ]; then ok "$f presente (non toccato)"; else cp "$APP/config/$f" "$CONF/$f"; ok "$f creato"; fi
  done

  say "File .env"
  if [ -f "$APP/.env" ]; then
    ok ".env presente (non toccato)"
  else
    cp "$APP/.env.example" "$APP/.env" && chmod 600 "$APP/.env"
    for k in SESSION_SECRET POSTGRES_PASSWORD SYNC_DB_PASSWORD GRAFANA_DB_PASSWORD GRAFANA_ADMIN_PASSWORD; do
      sed -i "s|^$k=.*|$k=$(openssl rand -hex 24)|" "$APP/.env"
    done
    ok ".env creato (600) con segreti generati"
  fi

  write_override
  say "Da fare a mano: compilare $APP/.env"
  echo "  GLPI_APP_TOKEN, GLPI_USER_TOKEN, GLPI_URL, LDAP_URL, LDAP_OPERATOR_GROUP_DN, LDAP_MANAGER_GROUP_DN,"
  echo "  PORTAL_PUBLIC_URL (URL https del portale). Poi: scripts/deploy.sh up"
}

write_override() {
  say "docker-compose.override.yml (dati in $BASE)"
  cat > "$APP/docker-compose.override.yml" <<EOF
# Generato da scripts/deploy.sh: tutti i dati sotto $BASE. Non versionato.
services:
  app:
    volumes: !override
      - $DATA/audit:/srv/data
  sync:
    volumes: !override
      - $DATA/audit:/srv/appdata:ro
      - $CONF:/srv/config:ro
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
  ok "scritto"
}

# ------------------------------------------------------------------ controlli
preflight() {
  say "Controlli"
  [ -f "$APP/.env" ] || die "manca $APP/.env: eseguire prima 'prepare'"
  [ -f "$APP/docker-compose.override.yml" ] || write_override

  local v; v="$(docker compose version --short 2>/dev/null | sed 's/^v//')" || die "docker compose non disponibile"
  if [ "$(printf '%s\n2.24.0\n' "$v" | sort -V | head -1)" != "2.24.0" ]; then die "docker compose $v: serve >= 2.24 (tag !override)"; fi
  ok "docker compose $v"

  local missing=() k
  for k in GLPI_URL GLPI_APP_TOKEN GLPI_USER_TOKEN LDAP_URL LDAP_OPERATOR_GROUP_DN LDAP_MANAGER_GROUP_DN \
           PORTAL_PUBLIC_URL SESSION_SECRET POSTGRES_PASSWORD SYNC_DB_PASSWORD GRAFANA_DB_PASSWORD GRAFANA_ADMIN_PASSWORD; do
    [ -n "$(envval "$k")" ] || missing+=("$k")
  done
  [ ${#missing[@]} -eq 0 ] || die "variabili vuote in .env: ${missing[*]}"
  ok "variabili obbligatorie presenti"

  if grep -qE '^DEV_AUTH=(1|true|yes|on)' "$APP/.env"; then die "DEV_AUTH attivo: vietato in produzione"; fi
  if grep -qE '^DEV_ROLES=.+' "$APP/.env"; then die "DEV_ROLES valorizzato: vietato in produzione"; fi
  if grep -qiE '^COOKIE_SECURE=(0|false|no|off)' "$APP/.env"; then die "COOKIE_SECURE disattivato"; fi
  [[ "$(envval SYNC_DB_PASSWORD)" =~ ^[A-Za-z0-9]+$ ]] || die "SYNC_DB_PASSWORD deve contenere solo [A-Za-z0-9] (finisce in un URL)"
  [[ "$(envval PORTAL_PUBLIC_URL)" == https://* ]] || warn "PORTAL_PUBLIC_URL non è https: i cookie Secure non funzioneranno"
  [ "$(stat -c %a "$APP/.env")" = "600" ] || { chmod 600 "$APP/.env"; warn ".env reso 600"; }
  ok "nessuna impostazione di sviluppo attiva"

  if ! docker network inspect controlroom_grafana_net >/dev/null 2>&1 && ip route 2>/dev/null | grep -q '^172\.30\.99\.'; then
    die "la subnet 172.30.99.0/28 (grafana_net) è già instradata su questo host: cambiarla in docker-compose.yml"
  fi
  ok "subnet interna libera"
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

# ------------------------------------------------------------------ up
cmd_up() {
  preflight
  say "Build e avvio (app, postgres, grafana)"
  compose up -d --build app postgres grafana || die "build o avvio falliti (vedi sopra)"
  wait_healthy postgres app grafana
  echo
  echo "  Portale su http://127.0.0.1:8000 (da esporre con il reverse proxy TLS su $(envval PORTAL_PUBLIC_URL))."
  echo "  Provare il login AD con: un operatore, un manager, un utente in entrambi i gruppi, uno in nessuno."
  echo "  Poi: scripts/deploy.sh check-glpi"
}

# ------------------------------------------------------------------ check-glpi
cmd_check_glpi() {
  preflight
  say "Verifica search option sul GLPI reale (sola lettura)"
  rm -f "$MARKER"
  if compose run --rm --no-deps -T sync python -m sync.check_glpi; then
    so_hash > "$MARKER"
    ok "configurazione GLPI verificata: ora 'scripts/deploy.sh sync'"
  else
    die "correggere GLPI_SO_* / GLPI_URL / token in .env e rieseguire check-glpi"
  fi
}

# ------------------------------------------------------------------ sync
cmd_sync() {
  preflight
  [ -f "$MARKER" ] || die "eseguire prima 'check-glpi' (deve terminare con ESITO: OK)"
  [ "$(cat "$MARKER")" = "$(so_hash)" ] || die "le impostazioni GLPI in .env sono cambiate dopo check-glpi: rieseguirlo"
  wait_healthy postgres
  say "Avvio del worker di sync"
  local since; since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  compose up -d --build sync || die "build o avvio del sync falliti (vedi sopra)"
  local line=""
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

# ------------------------------------------------------------------ status / backup / update
cmd_status() {
  say "Servizi"
  compose ps
  say "Ultima sincronizzazione"
  compose exec -T postgres psql -U postgres -d controlroom -c \
    "SELECT last_success_at, last_attempt_at, last_error, ticket_in_db, dataset_label FROM reporting.v_sync_status" \
    2>/dev/null || warn "postgres non raggiungibile o schema non ancora creato (il sync non è mai partito)"
}

cmd_backup() {
  local ts out; ts="$(date +%Y.%m.%d_%H%M)"; out="$BASE/backup"
  mkdir -p "$out" && chmod 700 "$out"
  say "Backup in $out"
  (umask 077 && compose exec -T postgres pg_dump -U postgres -Fc controlroom > "$out/${ts}_controlroom.dump")
  ok "${ts}_controlroom.dump ($(du -h "$out/${ts}_controlroom.dump" | cut -f1))"
  $SUDO tar czf "$out/${ts}_files.tgz" -C "$BASE" data/audit data/grafana config app/.env
  $SUDO chown "$(id -u):$(id -g)" "$out/${ts}_files.tgz" && chmod 600 "$out/${ts}_files.tgz"
  ok "${ts}_files.tgz (contiene .env con i segreti: conservarlo protetto)"
}

cmd_update() {
  say "Aggiornamento del codice"
  git -C "$APP" pull --ff-only
  ok "$(git -C "$APP" log --oneline -1)"
  preflight
  if compose ps -q sync | grep -q .; then
    [ "$(cat "$MARKER" 2>/dev/null)" = "$(so_hash)" ] || die "impostazioni GLPI cambiate: rieseguire check-glpi prima"
    compose up -d --build || die "build o avvio falliti (vedi sopra)"
  else
    compose up -d --build app postgres grafana || die "build o avvio falliti (vedi sopra)"
  fi
  wait_healthy postgres app grafana
}

case "${1:-}" in
  prepare)    cmd_prepare ;;
  up)         cmd_up ;;
  check-glpi) cmd_check_glpi ;;
  sync)       cmd_sync ;;
  status)     cmd_status ;;
  backup)     cmd_backup ;;
  update)     cmd_update ;;
  *) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
