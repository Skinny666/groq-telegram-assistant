#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

readonly APP_USER="telegram-assistant"
readonly APP_DIR="/opt/telegram-assistant"
readonly CREDENTIAL_DIR="/etc/telegram-assistant"
readonly STATE_DIR="/var/lib/telegram-assistant"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SOURCE_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

die() {
  printf 'ERRO: %s\n' "$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || die "Execute como root."
command -v apt-get >/dev/null || die "apt-get não encontrado."
command -v systemctl >/dev/null || die "systemd não encontrado."

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3 \
  python3-venv \
  ca-certificates
rm -rf /var/lib/apt/lists/*

if ! id "${APP_USER}" >/dev/null 2>&1; then
  useradd \
    --system \
    --home-dir "${STATE_DIR}" \
    --shell /usr/sbin/nologin \
    --user-group \
    "${APP_USER}"
fi

install -d -o root -g root -m 0755 "${APP_DIR}"
install -d -o root -g root -m 0700 "${CREDENTIAL_DIR}"
install -d -o "${APP_USER}" -g "${APP_USER}" -m 0700 "${STATE_DIR}"

rm -rf "${APP_DIR}/src" "${APP_DIR}/deploy" "${APP_DIR}/tests"
install -m 0644 "${SOURCE_DIR}/pyproject.toml" "${APP_DIR}/pyproject.toml"
install -m 0644 "${SOURCE_DIR}/requirements.lock" "${APP_DIR}/requirements.lock"
install -m 0644 "${SOURCE_DIR}/README.md" "${APP_DIR}/README.md"
install -m 0644 "${SOURCE_DIR}/THREAT_MODEL.md" "${APP_DIR}/THREAT_MODEL.md"
install -m 0644 "${SOURCE_DIR}/VALIDATION.md" "${APP_DIR}/VALIDATION.md"
install -m 0644 "${SOURCE_DIR}/SECURITY.md" "${APP_DIR}/SECURITY.md"
install -m 0644 "${SOURCE_DIR}/CHANGELOG.md" "${APP_DIR}/CHANGELOG.md"
install -m 0644 "${SOURCE_DIR}/LICENSE" "${APP_DIR}/LICENSE"
cp -a "${SOURCE_DIR}/src" "${APP_DIR}/src"
cp -a "${SOURCE_DIR}/deploy" "${APP_DIR}/deploy"
cp -a "${SOURCE_DIR}/tests" "${APP_DIR}/tests"
chown -R root:root "${APP_DIR}"
find "${APP_DIR}" -type d -exec chmod 0755 {} +
find "${APP_DIR}" -type f -exec chmod 0644 {} +
chmod 0755 "${APP_DIR}/deploy/"*.sh

rm -rf "${APP_DIR}/.venv"
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/python" -m pip install \
  --only-binary=:all: \
  --require-hashes \
  --no-cache-dir \
  -r "${APP_DIR}/requirements.lock"

# O serviço não executa como root. O ambiente permanece imutável para o usuário
# do serviço, mas o grupo precisa conseguir atravessar os diretórios e executar
# o interpretador.
chown -R root:"${APP_USER}" "${APP_DIR}/.venv"
chmod -R u=rwX,g=rX,o= "${APP_DIR}/.venv"

printf '\nAs credenciais não serão passadas por argumentos nem salvas no histórico.\n'
read -r -s -p "Token do bot Telegram: " TELEGRAM_TOKEN
printf '\n'
read -r -s -p "Chave da API Groq: " GROQ_KEY
printf '\n'
read -r -p "Seu Telegram user ID numérico: " TELEGRAM_USER_ID

[[ "${TELEGRAM_TOKEN}" != *[[:space:]]* ]] || die "Token contém espaço."
[[ "${GROQ_KEY}" != *[[:space:]]* ]] || die "Chave Groq contém espaço."
[[ "${TELEGRAM_USER_ID}" =~ ^[1-9][0-9]+$ ]] || die "Telegram user ID inválido."

printf '%s' "${TELEGRAM_TOKEN}" > "${CREDENTIAL_DIR}/telegram_token"
printf '%s' "${GROQ_KEY}" > "${CREDENTIAL_DIR}/groq_api_key"
printf '%s' "${TELEGRAM_USER_ID}" > "${CREDENTIAL_DIR}/authorized_user_id"
unset TELEGRAM_TOKEN GROQ_KEY TELEGRAM_USER_ID

chown root:root "${CREDENTIAL_DIR}/"*
chmod 0600 "${CREDENTIAL_DIR}/"*

install -o root -g root -m 0644 \
  "${APP_DIR}/deploy/telegram-assistant-bot.service" \
  /etc/systemd/system/telegram-assistant-bot.service
install -o root -g root -m 0644 \
  "${APP_DIR}/deploy/telegram-assistant-reminder.service" \
  /etc/systemd/system/telegram-assistant-reminder.service
install -o root -g root -m 0644 \
  "${APP_DIR}/deploy/telegram-assistant-reminder.timer" \
  /etc/systemd/system/telegram-assistant-reminder.timer

systemctl daemon-reload
systemctl enable --now telegram-assistant-bot.service
systemctl enable --now telegram-assistant-reminder.timer

printf '\nInstalação concluída.\n'
systemctl --no-pager --full status telegram-assistant-bot.service || true
systemctl --no-pager --full status telegram-assistant-reminder.timer || true
