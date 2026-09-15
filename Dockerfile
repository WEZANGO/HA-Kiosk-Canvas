FROM ghcr.io/home-assistant/base:latest

LABEL \
  io.hass.version="0.1.5" \
  io.hass.type="app" \
  io.hass.arch="aarch64|amd64|armv7|armhf|i386"

# Pillow renders each canvas server-side, and these font packages ARE the
# editor's font choices. python3 + py3-pillow are required (a build that cannot
# render is not worth shipping quietly); the font packages are installed
# best-effort, one apk call at a time, because Alpine retires and renames font
# families between releases — font-ubuntu, for example, no longer exists in the
# Alpine release this base image is on. Any family that is unavailable is
# reported in the build log and simply absent from the picker.
RUN apk add --no-cache python3 py3-pillow \
 && for font in font-dejavu font-liberation font-noto font-roboto \
               font-inconsolata font-jetbrains-mono; do \
        apk add --no-cache "$font" \
        || echo "kiosk-canvas: $font is not available in this Alpine release, skipping it"; \
    done

COPY run.sh /run.sh
COPY app.py /app/app.py
COPY web /app/web
RUN chmod a+x /run.sh

CMD ["/run.sh"]
