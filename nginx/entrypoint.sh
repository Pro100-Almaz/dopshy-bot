#!/bin/sh
set -e

# ── Validate config ────────────────────────────────────────────────────────
: "${SERVER_NAME:?SERVER_NAME env var is required (e.g. bot.dopsy.kz)}"
: "${API_SERVER_NAME:?API_SERVER_NAME env var is required (e.g. api.dopsy.kz)}"

CERT=/etc/letsencrypt/live/${SERVER_NAME}/fullchain.pem
API_CERT=/etc/letsencrypt/live/${API_SERVER_NAME}/fullchain.pem
HTTP_TPL=/etc/nginx/templates/http-only.conf.template
HTTPS_TPL=/etc/nginx/templates/server.conf.template
ACTIVE_CONF=/etc/nginx/conf.d/default.conf
# Only these two placeholders are substituted; nginx runtime vars ($host, etc.)
# are left untouched.
SUBST='${SERVER_NAME} ${API_SERVER_NAME}'

# ── Bootstrap: HTTP-only first so certbot can solve the ACME challenge ────
echo "[nginx] Rendering HTTP-only config for ${SERVER_NAME} and ${API_SERVER_NAME}"
envsubst "$SUBST" < "$HTTP_TPL" > "$ACTIVE_CONF"
nginx -g "daemon on;"

# ── Wait for certbot to drop BOTH certs into the shared volume ────────────
echo "[nginx] Waiting for TLS certificates (${SERVER_NAME}, ${API_SERVER_NAME})..."
while [ ! -f "$CERT" ] || [ ! -f "$API_CERT" ]; do
    sleep 5
done
echo "[nginx] Certificates found, switching to HTTPS config."

# ── Render full HTTPS config and reload ───────────────────────────────────
envsubst "$SUBST" < "$HTTPS_TPL" > "$ACTIVE_CONF"
nginx -s reload

# Reload every 24h so renewed certs are picked up without a restart.
while true; do
    sleep 24h
    nginx -s reload
    echo "[nginx] reloaded to pick up renewed certificate."
done
