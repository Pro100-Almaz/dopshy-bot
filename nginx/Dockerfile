FROM nginx:alpine

COPY entrypoint.sh /entrypoint.sh
COPY http-only.conf /etc/nginx/http-only.conf
COPY bot.dopsy.kz.conf /etc/nginx/bot.dopsy.kz.conf

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
