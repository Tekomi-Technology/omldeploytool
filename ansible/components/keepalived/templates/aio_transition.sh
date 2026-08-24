#!/bin/bash
set -Eeuo pipefail
LOG=/var/log/keepalive_transition.log
PATH=/usr/sbin:/usr/bin:/sbin:/bin
SYSTEMCTL=$(command -v systemctl || echo /usr/bin/systemctl)

log(){ echo "$(date '+%F %T'): $*" >>"$LOG"; }
run(){ local d="$1"; shift; "$@" >>"$LOG" 2>&1 || { log "ERROR: $d (cmd: $*)"; return 1; }; log "OK: $d"; }

start_asterisk_with_retry(){
  for i in {1..5}; do
    if "$SYSTEMCTL" start asterisk >>"$LOG" 2>&1; then
      log "OK: start asterisk (attempt $i)"
      return 0
    fi
    log "WARN: start asterisk failed (attempt $i), sleeping 3s"
    sleep 3
    # limpia PID obsoleto si existiera
    [ -f /var/run/asterisk/asterisk.pid ] && rm -f /var/run/asterisk/asterisk.pid || true
  done
  log "ERROR: asterisk no inició tras reintentos"
  return 1
}

ENDSTATE=${1:-UNKNOWN}
log "transition to $ENDSTATE, exec oml_cluster_transition.sh"

case "$ENDSTATE" in
  BACKUP|FAULT)
    run "stop nginx"                 "$SYSTEMCTL" stop nginx
    run "stop ami"                   "$SYSTEMCTL" stop ami
    run "stop email_events_processor""$SYSTEMCTL" stop email_events_processor
    run "stop whatsapp"              "$SYSTEMCTL" stop whatsapp
    run "stop background_tasks"      "$SYSTEMCTL" stop background_tasks
    run "stop omnileads"             "$SYSTEMCTL" stop omnileads
    run "start daphne"               "$SYSTEMCTL" start daphne
    run "stop omlcron"               "$SYSTEMCTL" stop omlcron
    run "stop asterisk"              "$SYSTEMCTL" stop asterisk
    run "stop asterisk_retrieve_conf" "$SYSTEMCTL" stop asterisk_retrieve_conf
    run "stop background_dialer_tasks" "$SYSTEMCTL" stop background_dialer_tasks
    run "stop call_logger"            "$SYSTEMCTL" stop call_logger
    ;;
  MASTER)
    start_asterisk_with_retry || true
    sleep 5
    run "start ami"                     "$SYSTEMCTL" start ami
    run "start omnileads"               "$SYSTEMCTL" start omnileads
    run "start daphne"                  "$SYSTEMCTL" start daphne
    run "start email_events_processor"  "$SYSTEMCTL" start email_events_processor
    run "start whatsapp"                "$SYSTEMCTL" start whatsapp
    run "start background_tasks"        "$SYSTEMCTL" start background_tasks
    run "start background_dialer_tasks" "$SYSTEMCTL" start background_dialer_tasks
    sleep 10
    run "start nginx"                   "$SYSTEMCTL" start nginx
    sleep 3
    run "start asterisk_retrieve_conf"  "$SYSTEMCTL" start asterisk_retrieve_conf
    run "start omlcron"                 "$SYSTEMCTL" start omlcron
    run "start call_logger"             "$SYSTEMCTL" start call_logger
    run "oml_manage --redis_sync"       oml_manage --redis_sync
    ;;
  *)
    log "Unknown state ${ENDSTATE}"; exit 1 ;;
esac
exit 0
