#!/bin/bash
# Build overlay omlapp:tekomi desde el fork (git-first, sin copiar por SSH).
# Uso: ./build-tekomi.sh  (desde docker-compose/prod-env)
set -e
cd "$(dirname "$0")"
SRC=../../components-git-repo/django
if [ ! -d "$SRC/.git" ]; then
  echo "ERROR: falta $SRC (clonar: git clone -b tekomi-sipdev <fork> $SRC)"
  exit 1
fi
git -C "$SRC" fetch origin tekomi-sipdev
git -C "$SRC" checkout tekomi-sipdev
git -C "$SRC" pull --ff-only origin tekomi-sipdev || true
docker build -f omlapp-overlay.Dockerfile -t omlapp:tekomi "$SRC"
echo "OK: omlapp:tekomi construido. Recrear servicios con:"
echo "  docker compose up -d django-app daphne cronos background_tasks background-dialer-tasks whatsapp email_events_processor"
