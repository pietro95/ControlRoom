#!/usr/bin/env bash
# Comando unico di esercizio di Control Room (link: /usr/local/bin/controlroom). Si lancia con il proprio utente:
# chiede sudo, poi lavora SEMPRE come utente di servizio "controlroom", mai come root.
#
#   controlroom stato                     versione, servizi, ultima sincronizzazione
#   controlroom aggiorna latest|<tag> [--si]
#                                         installa l'ultimo tag vX.Y.Z (o quello indicato), con log in log/
#   controlroom backup [etichetta]        dump del database + archivio dei file in backup/
#   controlroom check-glpi                verifica le search option sul GLPI reale (sola lettura)
#   controlroom sync                      avvia il worker di sync (dopo check-glpi)
#   controlroom ripristina [file.dump] [--si]
#                                         senza file elenca i dump; con file ripristina il database
#   controlroom log [servizio]            senza servizio elenca i log degli aggiornamenti; con servizio i log docker
#   controlroom aiuto                     questo testo
#
# Installazione una tantum: sudo <app>/scripts/installa_utente_servizio.sh (docs/RUNBOOK.md).
# Variabili (solo test, rispettate solo se impostate da root): CR_BASE, CR_SVC_USER, CR_SVC_HOME, CR_DEPLOY.
set -euo pipefail

BASE="${CR_BASE:-/appdata/controlroom}"
SVC="${CR_SVC_USER:-controlroom}"
SVC_HOME="${CR_SVC_HOME:-/var/lib/$SVC}"
APP="$BASE/app"
DEPLOY="${CR_DEPLOY:-$APP/scripts/deploy.sh}"
LOGDIR="$BASE/log"
TAG_RE='^v[0-9]+\.[0-9]+\.[0-9]+$'

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '  \033[33m!!\033[0m  %s\n' "$*"; }
die()  { printf '  \033[31mKO\033[0m  %s\n' "$*" >&2; exit 1; }
aiuto() { awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$(readlink -f "$0")"; }

# Gira dentro tmux o screen? sudo cancella TMUX e STY: si risale la catena dei processi padre.
multiplexer() {
  local pid=$$ comm
  while [ "$pid" -gt 1 ] 2>/dev/null; do
    comm="$(cat "/proc/$pid/comm" 2>/dev/null)" || break
    case "$comm" in tmux*|screen*|SCREEN*) echo "${comm%%:*}"; return ;; esac
    pid="$(awk '/^PPid:/ {print $2}' "/proc/$pid/status" 2>/dev/null)" || break
  done
}

# Da utente qualsiasi -> sudo; da root -> utente di servizio con ambiente pulito.
cambia_utente() {
  local self; self="$(readlink -f "$0")"
  if [ "$(id -u)" -ne 0 ]; then
    exec sudo -- "$self" "$@"
  fi
  id -u "$SVC" >/dev/null 2>&1 \
    || die "utente di servizio $SVC inesistente: eseguire una volta sudo $APP/scripts/installa_utente_servizio.sh"
  local pass=() v
  for v in CR_BASE CR_SVC_USER CR_SVC_HOME CR_DEPLOY; do [ -z "${!v:-}" ] || pass+=("$v=${!v}"); done
  exec runuser -u "$SVC" -- env -i HOME="$SVC_HOME" PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG="${LANG:-C.UTF-8}" TERM="${TERM:-dumb}" CR_LANCIATO_DA="${SUDO_USER:-root}" \
    CR_MULTIPLEXER="${CR_MULTIPLEXER:-$(multiplexer)}" "${pass[@]}" "$self" "$@"
}

conferma() {   # $1 domanda, $2 "--si" se già confermato
  [ "${2:-}" = "--si" ] && return 0
  local r; read -r -p "  $1 [s/N] " r || r=""
  case "$r" in s|S|si|Si|SI|sì) return 0 ;; *) echo "  Annullato."; return 1 ;; esac
}

installata() { git -C "$APP" describe --tags --exact-match HEAD 2>/dev/null || git -C "$APP" describe --tags --always 2>/dev/null || echo "?"; }

cmd_aggiorna() {
  local richiesta="" si=""
  while [ $# -gt 0 ]; do case "$1" in --si) si="--si" ;; *) [ -z "$richiesta" ] || die "uso: controlroom aggiorna latest|<tag> [--si]"; richiesta="$1" ;; esac; shift; done
  [ -n "$richiesta" ] || die "uso: controlroom aggiorna latest|<tag> [--si]"

  say "Tag da GitHub"
  git -C "$APP" fetch --tags --quiet origin || die "git fetch fallito"
  local nuova attuale
  if [ "$richiesta" = latest ]; then
    nuova="$(git -C "$APP" tag -l | grep -E "$TAG_RE" | sort -V | tail -1 || true)"
    [ -n "$nuova" ] || die "nessun tag nel formato vX.Y.Z"
  else
    git -C "$APP" rev-parse -q --verify "refs/tags/$richiesta" >/dev/null || die "il tag $richiesta non esiste"
    nuova="$richiesta"
  fi
  attuale="$(installata)"
  echo "  installata: $attuale"
  echo "  richiesta:  $nuova"
  if [ "$attuale" = "$nuova" ]; then echo "  Già installata: nulla da fare."; return 0; fi
  [ -n "${CR_MULTIPLEXER:-}" ] || warn "non sei in tmux/screen: se la connessione cade l'aggiornamento si interrompe"
  conferma "Installare $nuova al posto di $attuale?" "$si" || return 0

  mkdir -p "$LOGDIR"
  local log; log="$LOGDIR/$(date +%Y.%m.%d_%H%M)_aggiorna_${nuova//[^A-Za-z0-9.]/_}.log"
  { echo "aggiorna $attuale -> $nuova"; echo "lanciato da: ${CR_LANCIATO_DA:-?} (eseguito come $(id -un)) su $(hostname) il $(date '+%d/%m/%Y %H:%M:%S')"; } | tee "$log"
  local rc
  set +e; "$DEPLOY" install "$nuova" 2>&1 | tee -a "$log"; rc="${PIPESTATUS[0]}"; set -e
  if [ "$rc" -ne 0 ]; then
    echo "ESITO: FALLITO (codice $rc)" >> "$log"
    die "deploy fallito (log: $log). Rollback: docs/RUNBOOK.md §6 (controlroom aggiorna $attuale, poi eventuale ripristina)"
  fi
  echo "ESITO: OK" >> "$log"
  echo "  Log: $log"
}

cmd_ripristina() {
  local f="" si=""
  while [ $# -gt 0 ]; do case "$1" in --si) si="--si" ;; *) f="$1" ;; esac; shift; done
  if [ -z "$f" ]; then
    say "Dump disponibili in $BASE/backup"; find "$BASE/backup" -maxdepth 1 -name '*.dump' -printf '  %f\n' | sort -r; return 0
  fi
  conferma "Sostituire il database di reporting con $f? (prima viene fatto un backup)" "$si" || return 0
  "$DEPLOY" restore "$f"
}

cmd_log() {
  if [ -n "${1:-}" ]; then "$DEPLOY" logs "$1"; return; fi
  say "Log degli aggiornamenti in $LOGDIR"; find "$LOGDIR" -maxdepth 1 -name '*.log' -printf '  %f\n' 2>/dev/null | sort -r | head -20
}

# Tutto dentro main, chiamata sulla stessa riga di exit: "aggiorna" sostituisce questo file mentre gira.
main() {
  case "${1:-aiuto}" in aiuto|-h|--help) aiuto; return 0 ;; esac
  [ "$(id -un)" = "$SVC" ] || cambia_utente "$@"
  local cmd="$1"; shift
  case "$cmd" in
    stato)      "$DEPLOY" status ;;
    aggiorna)   cmd_aggiorna "$@" ;;
    backup)     "$DEPLOY" backup "${1:-}" ;;
    check-glpi) "$DEPLOY" check-glpi ;;
    sync)       "$DEPLOY" sync ;;
    ripristina) cmd_ripristina "$@" ;;
    log)        cmd_log "${1:-}" ;;
    *) aiuto; return 2 ;;
  esac
}

main "$@"; exit $?
