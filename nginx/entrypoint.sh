#!/bin/sh
set -e

CERT=/etc/letsencrypt/live/bot.dopsy.kz/fullchain.pem
HTTP_CONF=/etc/nginx/http-only.conf
HTTPS_CONF=/etc/nginx/bot.dopsy.kz.conf
ACTIVE_CONF=/etc/nginx/conf.d/default.conf

# Start with HTTP-only config so certbot can complete the ACME challenge
cp "$HTTP_CONF" "$ACTIVE_CONF"
nginx -g "daemon on;"

# Wait for certbot to obtain the certificate
echo "Waiting for TLS certificate..."
while [ ! -f "$CERT" ]; do
    sleep 5
done
echo "Certificate found, switching to HTTPS config."

# Switch to full HTTPS config and reload
cp "$HTTPS_CONF" "$ACTIVE_CONF"
nginx -s reload

# Reload nginx every 24h so renewed certificates are picked up
while true; do
    sleep 24h
    nginx -s reload
    echo "nginx reloaded to pick up renewed certificate."
done
