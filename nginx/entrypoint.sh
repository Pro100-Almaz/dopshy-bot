#!/bin/sh
set -e

# ── Validate config ────────────────────────────────────────────────────────
: "${SERVER_NAME:?SERVER_NAME env var is required (e.g. bot-dev.dopsy.kz)}"

CERT=/etc/letsencrypt/live/${SERVER_NAME}/fullchain.pem
HTTP_TPL=/etc/nginx/templates/http-only.conf.template
HTTPS_TPL=/etc/nginx/templates/server.conf.template
ACTIVE_CONF=/etc/nginx/conf.d/default.conf

# ── Bootstrap: HTTP-only first so certbot can solve the ACME challenge ────
echo "[nginx] Rendering HTTP-only config for ${SERVER_NAME}"
envsubst '${SERVER_NAME}' < "$HTTP_TPL" > "$ACTIVE_CONF"
nginx -g "daemon on;"

# ── Wait for certbot to drop the cert into the shared volume ──────────────
echo "[nginx] Waiting for TLS certificate at ${CERT}..."
while [ ! -f "$CERT" ]; do
    sleep 5
done
echo "[nginx] Certificate found, switching to HTTPS config."

# ── Render full HTTPS config and reload ───────────────────────────────────
envsubst '${SERVER_NAME}' < "$HTTPS_TPL" > "$ACTIVE_CONF"
nginx -s reload

# Reload every 24h so renewed certs are picked up without a restart.
while true; do
    sleep 24h
    nginx -s reload
    echo "[nginx] reloaded to pick up renewed certificate."
done
