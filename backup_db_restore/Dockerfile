FROM ghcr.io/home-assistant/base:latest

ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=0.5.8

LABEL \
    io.hass.name="Backup DB Restore" \
    io.hass.description="Import Home Assistant recorder history from uploaded backup databases." \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}"

RUN \
    apk add --no-cache \
        python3

COPY app.py /app/app.py
COPY web /app/web
COPY run.sh /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
