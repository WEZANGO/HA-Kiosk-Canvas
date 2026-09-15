# Kiosk Canvas Displays

A Home Assistant add-on for building **image-only dashboards**. Compose custom
text, uploaded icons/images and live Home Assistant entity values on a canvas,
then use the canvas as an image URL anywhere a picture can be shown:

```
http://homeassistant.local:8097/canvas/oven-finished.png?auth=YOUR-TOKEN
```

Every request re-reads the entities and re-renders, so the picture is never
stale. Typical use: the oven finishes, your automation announces it and puts a
full-screen image on the kitchen display showing "Dinner is ready", the oven's
temperature, and a dial icon.

## Why an image, and not a dashboard?

Media players, wall panels, doorbell screens and "play a picture" automations
accept an image URL — not a Lovelace dashboard. This add-on gives you those URLs.

## Features

- **Canvas editor** in the add-on's Web UI: add any number of text, image and
  entity elements, drag to move, drag the corner handle to scale, rotate, fade,
  reorder layers, hide without deleting, snap to an 8px grid (hold ⌥/Alt for
  free positioning, arrows to nudge).
- **Live entity values**: an entity element shows the entity's state (or any
  attribute) with its unit, a chosen number of decimals and optional prefix and
  suffix text. Custom text can also embed `{{sensor.entity_id}}` or
  `{{sensor.entity_id.attribute}}` inline.
- **Five font families** installed in the image (DejaVu, Liberation, Noto, Roboto,
  Ubuntu) in regular/bold/italic, offered through a picker that reflects what the
  image has. The editor is served the same font files it renders with, so the
  preview matches the image to within a pixel. Adding a family = adding an `apk`
  package.
- **Uploaded PNG/JPG icons and images** (with transparency), usable as elements
  or as the canvas background (cover / contain / stretch).
- **PNG or JPG output**, at the canvas size or scaled with `?w=` / `?h=`.
- **Real preview**: the editor shows the exact server-rendered image, so what
  you see is what the display gets.
- No browser engine and no build step: rendering is Pillow-based, so the add-on
  still runs on armv7/armhf/i386.

## Install locally

1. Copy this folder to your Home Assistant `addons` directory, for example
   `/addons/kiosk_canvas_displays`.
2. In Home Assistant go to **Settings → Add-ons → Add-on store**, use the
   overflow menu, then **Check for updates**.
3. Install **Kiosk Canvas Displays** and start it.
4. Open its Web UI from the sidebar, create a canvas, and copy the image URL it
   shows.

The add-on needs the `homeassistant_api` permission (already set in
`config.yaml`) to read entity values through the Supervisor API proxy. No
long-lived access token is required.

## Use the image

```yaml
# Send it with a notification
actions:
  - service: notify.mobile_app_your_phone
    data:
      title: Oven
      message: "Dinner is ready — {{ states('sensor.oven_temperature') }} °C"
      data:
        image: "http://homeassistant.local:8097/canvas/oven-finished.png?auth=YOUR-TOKEN"
```

```yaml
# Show it on a media player / smart display
actions:
  - service: media_player.play_media
    target:
      entity_id: media_player.kitchen_display
    data:
      media_content_id: "http://homeassistant.local:8097/canvas/oven-finished.png?auth=YOUR-TOKEN"
      media_content_type: image
```

```yaml
# Or as a camera the rest of Home Assistant can use
camera:
  - platform: generic
    name: Oven canvas
    still_image_url: "http://local-kiosk-canvas-displays:8097/canvas/oven-finished.png?auth=YOUR-TOKEN"
    scan_interval: 5
```

See `DOCS.md` for the full reference: URL parameters, element options, how the
token works, and the TV/Chromecast caveat.

## Development

`app.py` is the whole server (stdlib + Pillow): storage, the Supervisor entity
client, the renderer and the HTTP routes. `web/editor.html` is the editor (plain
HTML/CSS/JS, no build step).

For local work outside Home Assistant, run a wrapper that points the app's path
constants at a temporary folder and stubs the Supervisor call — see the
"Local testing" note in the add-on skill, and `DOCS.md` for how each piece is
expected to behave.
