#!/usr/bin/env bash
# Installazione una tantum (idempotente) dell'utente di servizio di Control Room. Da lanciare con sudo:
#
#   sudo /appdata/controlroom/app/scripts/installa_utente_servizio.sh [--chiave <file>] [--uid-da <n>]
#
# - crea l'utente di sistema "controlroom" (senza login, home /var/lib/controlroom, gruppo docker) con UID = GID,
#   il primo libero scendendo da --uid-da (default 985: gestcontratti sta vicino a 990);
# - copia nella sua home la deploy key (cercata in /root/.ssh e /home/*/.ssh, oppure --chiave) e la chiave host di
#   github.com, e scrive ~/.ssh/config con l'alias dell'URL remoto del clone;
# - assegna all'utente solo codice (app/), configurazione (config/, csv/), backup/ e la nuova log/;
#   MAI data/ (volumi dei container: audit 10001, postgres 70, grafana 472);
# - verifica come utente di servizio: lettura del .env, docker ps, git ls-remote;
# - crea /usr/local/bin/controlroom -> app/scripts/controlroom.sh.
#
# Variabili (solo test): CR_BASE, CR_SVC_USER, CR_SVC_HOME, CR_BIN_LINK.
set -euo pipefail

BASE="${CR_BASE:-/appdata/controlroom}"
SVC="${CR_SVC_USER:-controlroom}"
SVC_HOME="${CR_SVC_HOME:-/var/lib/$SVC}"
LINK="${CR_BIN_LINK:-/usr/local/bin/controlroom}"
APP="$BASE/app"
CONTAINER_UIDS="0 70 472 10001"   # root, postgres (alpine), grafana, app/sync: vedi scripts/deploy.sh

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m  %s\n' "$*"; }
die()  { printf '  \033[31mKO\033[0m  %s\n' "$*" >&2; exit 1; }
come() { runuser -u "$SVC" -- env -i HOME="$SVC_HOME" PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
           LANG=C.UTF-8 "$@"; }

main() {
  local chiave="" uid_da=985
  while [ $# -gt 0 ]; do
    case "$1" in
      --chiave) chiave="${2:?--chiave richiede un file}"; shift 2 ;;
      --uid-da) uid_da="${2:?--uid-da richiede un numero}"; shift 2 ;;
      *) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 2 ;;
    esac
  done
  [ "$(id -u)" -eq 0 ] || die "lanciare con sudo"
  [ -d "$APP/.git" ] || die "manca $APP: eseguire prima il deploy (scripts/deploy.sh prepare/install)"
  [ -f "$BASE/config/.env" ] || die "manca $BASE/config/.env"
  getent group docker >/dev/null || die "gruppo docker inesistente"
  local t; for t in runuser useradd groupadd usermod ssh-keygen git docker; do
    command -v "$t" >/dev/null || die "comando mancante: $t"
  done

  say "Utente di servizio $SVC"
  local uid
  if uid="$(id -u "$SVC" 2>/dev/null)"; then
    ok "esiste già (uid $uid)"
  else
    uid="$uid_da"
    while getent passwd "$uid" >/dev/null || getent group "$uid" >/dev/null; do
      uid=$((uid - 1)); [ "$uid" -gt 100 ] || die "nessun UID/GID libero sotto $uid_da"
    done
    getent group "$SVC" >/dev/null && die "esiste già un gruppo $SVC senza utente: risolvere a mano"
    groupadd --system --gid "$uid" "$SVC"
    useradd --system --uid "$uid" --gid "$uid" --home-dir "$SVC_HOME" --create-home \
            --shell /usr/sbin/nologin --comment "Control Room (servizio)" "$SVC"
    ok "creato: uid=gid=$uid, home $SVC_HOME, shell nologin"
  fi
  local c; for c in $CONTAINER_UIDS; do
    [ "$uid" != "$c" ] || die "l'UID $uid coincide con un UID dei container ($CONTAINER_UIDS): usare --uid-da"
  done
  usermod -aG docker "$SVC"
  ok "nel gruppo docker"

  say "Deploy key"
  local url alias src kh
  url="$(git -c safe.directory="$APP" -C "$APP" config --get remote.origin.url)" || die "remote origin non leggibile in $APP"
  case "$url" in
    *://*) die "URL remoto $url: atteso il formato scp (alias:owner/repo.git) della deploy key" ;;
    *:*)   alias="${url%%:*}"; alias="${alias#*@}" ;;
    *)     die "URL remoto non SSH: $url" ;;
  esac
  if [ -z "$chiave" ]; then
    mapfile -t trovate < <(find /root/.ssh /home/*/.ssh -maxdepth 1 -type f -name 'controlroom_deploy' 2>/dev/null)
    [ "${#trovate[@]}" -ge 1 ] || die "deploy key controlroom_deploy non trovata in /root/.ssh né /home/*/.ssh: usare --chiave"
    if [ "${#trovate[@]}" -gt 1 ]; then
      [ "$(sha256sum "${trovate[@]}" | cut -d' ' -f1 | sort -u | wc -l)" -eq 1 ] \
        || die "più deploy key diverse (${trovate[*]}): scegliere con --chiave"
    fi
    chiave="${trovate[0]}"
  fi
  [ -f "$chiave" ] || die "chiave non trovata: $chiave"
  src="$(dirname "$chiave")/known_hosts"
  kh="$(ssh-keygen -F github.com -f "$src" 2>/dev/null | grep -v '^#' || true)"
  [ -n "$kh" ] || die "github.com assente in $src: collegarsi una volta a GitHub con quell'utente o aggiungere la chiave host"
  install -d -m 700 -o "$SVC" -g "$SVC" "$SVC_HOME/.ssh"
  install -m 600 -o "$SVC" -g "$SVC" "$chiave" "$SVC_HOME/.ssh/controlroom_deploy"
  printf '%s\n' "$kh" > "$SVC_HOME/.ssh/known_hosts"
  cat > "$SVC_HOME/.ssh/config" <<EOF
# Generato da scripts/installa_utente_servizio.sh
Host $alias
  HostName github.com
  User git
  IdentityFile $SVC_HOME/.ssh/controlroom_deploy
  IdentitiesOnly yes
  StrictHostKeyChecking yes
EOF
  chown "$SVC:$SVC" "$SVC_HOME/.ssh/known_hosts" "$SVC_HOME/.ssh/config"
  chmod 600 "$SVC_HOME/.ssh/known_hosts" "$SVC_HOME/.ssh/config"
  ok "chiave da $chiave, chiave host di github.com da $src, alias $alias"

  say "Proprietari in $BASE (data/ esclusa)"
  install -d -m 750 "$BASE/log"
  chown "$SVC:$SVC" "$BASE"
  chown -R "$SVC:$SVC" "$APP" "$BASE/config" "$BASE/csv" "$BASE/backup" "$BASE/log"
  chmod 700 "$BASE/config" "$BASE/backup"
  chmod 600 "$BASE/config/.env"
  ok "app/ config/ csv/ backup/ log/ a $SVC; .env 600; data/ invariata"

  say "Verifiche come $SVC"
  come test -r "$BASE/config/.env" || die "$SVC non legge $BASE/config/.env"
  ok "lettura del .env"
  come docker ps >/dev/null || die "$SVC non può usare docker (gruppo docker? socket?)"
  ok "docker ps"
  come git -C "$APP" ls-remote --exit-code origin HEAD >/dev/null || die "git ls-remote verso $url fallito come $SVC"
  ok "git ls-remote"

  say "Comando $LINK"
  ln -sfn "$APP/scripts/controlroom.sh" "$LINK"
  ok "$LINK -> $APP/scripts/controlroom.sh. Provare con il proprio utente: controlroom stato"
}

main "$@"; exit $?
