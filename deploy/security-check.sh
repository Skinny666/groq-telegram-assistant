#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

readonly APP_USER="telegram-assistant"

printf '== Identidade do serviço ==\n'
id "${APP_USER}"
if id -nG "${APP_USER}" | tr ' ' '\n' | grep -qx docker; then
  printf 'FALHA: usuário pertence ao grupo docker.\n' >&2
  exit 1
fi

printf '\n== Docker socket inacessível ==\n'
if runuser -u "${APP_USER}" -- test -r /var/run/docker.sock; then
  printf 'FALHA: usuário consegue ler o Docker socket.\n' >&2
  exit 1
fi
printf 'OK\n'

printf '\n== Permissões das credenciais ==\n'
stat -c '%A %U:%G %n' /etc/telegram-assistant/*
find /etc/telegram-assistant -type f ! -perm 0600 -print -quit | \
  grep -q . && {
    printf 'FALHA: credencial fora do modo 0600.\n' >&2
    exit 1
  } || true

printf '\n== Portas escutadas pelo usuário ==\n'
if command -v lsof >/dev/null 2>&1; then
  lsof -nP -a -u "${APP_USER}" -iTCP -sTCP:LISTEN || true
else
  printf 'lsof não instalado; valide com ss -lntup.\n'
fi

printf '\n== Propriedades de isolamento ==\n'
systemctl show telegram-assistant-bot.service \
  -p User \
  -p Group \
  -p NoNewPrivileges \
  -p ProtectSystem \
  -p ProtectHome \
  -p PrivateDevices \
  -p RestrictNamespaces \
  -p RestrictAddressFamilies \
  -p CapabilityBoundingSet \
  -p MemoryMax \
  -p TasksMax

printf '\n== Avaliação systemd ==\n'
systemd-analyze security --no-pager telegram-assistant-bot.service || true

printf '\n== Estado ==\n'
systemctl --no-pager --full status telegram-assistant-bot.service || true
systemctl --no-pager --full status telegram-assistant-reminder.timer || true
