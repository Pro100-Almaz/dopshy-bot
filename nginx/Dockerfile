FROM nginx:alpine

# envsubst (from gettext) lets the entrypoint render config templates with
# ${SERVER_NAME} at runtime — same image works for dev and prod by changing
# one env var.
RUN apk add --no-cache gettext

COPY entrypoint.sh /entrypoint.sh
COPY http-only.conf.template /etc/nginx/templates/http-only.conf.template
COPY server.conf.template    /etc/nginx/templates/server.conf.template

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
