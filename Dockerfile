FROM ghcr.io/home-assistant/base:latest

LABEL \
  io.hass.version="0.1.1" \
  io.hass.type="app" \
  io.hass.arch="aarch64|amd64|armv7|armhf|i386"

# Pillow renders each canvas server-side, and the font packages are the font
# choices in the editor: the editor is served these very same files over
# /fonts/<key>, so its live preview has the same metrics as the image. Adding a
# font family is just adding a package here — no code change.
RUN apk add --no-cache python3 py3-pillow \
      font-dejavu font-liberation font-noto font-roboto font-ubuntu

COPY run.sh /run.sh
COPY app.py /app/app.py
COPY web /app/web
RUN chmod a+x /run.sh

CMD ["/run.sh"]
