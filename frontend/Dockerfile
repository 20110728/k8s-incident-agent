FROM node:24-alpine AS builder

WORKDIR /build

COPY frontend/package.json frontend/package-lock.json ./

RUN npm ci

COPY frontend/ ./

RUN npm run build


FROM nginx:1.30.4-alpine

ENV BACKEND_HOST=backend \
    BACKEND_PORT=8000 \
    NGINX_ENVSUBST_FILTER="^(BACKEND_HOST|BACKEND_PORT)$"

COPY --chown=nginx:nginx \
    frontend/nginx/nginx.conf \
    /etc/nginx/nginx.conf

COPY --chown=nginx:nginx \
    frontend/nginx/default.conf.template \
    /etc/nginx/templates/default.conf.template

COPY --from=builder \
    --chown=nginx:nginx \
    /build/dist \
    /usr/share/nginx/html

RUN rm -f /etc/nginx/conf.d/default.conf \
    && chown -R nginx:nginx \
        /etc/nginx/conf.d \
        /usr/share/nginx/html \
        /var/cache/nginx

USER nginx

EXPOSE 8080

HEALTHCHECK \
    --interval=5s \
    --timeout=3s \
    --start-period=5s \
    --retries=5 \
    CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/frontend-healthz"]

CMD ["nginx", "-g", "daemon off;"]