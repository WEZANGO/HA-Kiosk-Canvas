FROM ghcr.io/home-assistant/base:latest

LABEL \
  io.hass.version="0.1.2" \
  io.hass.type="app" \
  io.hass.arch="aarch64|amd64|armv7|armhf|i386"

# Pillow renders each canvas server-side, and the font packages are the editor's
# font choices. python3 + py3-pillow are required; the fonts are installed
# best-effort, one at a time, because Alpine drops and renames font families
# between releases — font-ubuntu, for instance, no longer exists in the Alpine
# version this base image uses — and a missing font must not fail the build.
# Whatever does install shows up in the editor's picker automatically.
RUN apk add --no-cache python3 py3-pillow \
 && for font in font-dejavu font-liberation font-noto font-roboto; do \
        apk add --no-cache "$font" \
        || echo "kiosk-canvas: $font is not available in this Alpine release, skipping it"; \
    done

COPY run.sh /run.sh
COPY app.py /app/app.py
COPY web /app/web
RUN chmod a+x /run.sh

CMD ["/run.sh"]
