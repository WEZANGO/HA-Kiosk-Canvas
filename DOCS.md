# Kiosk Canvas Displays

Build image-only displays: custom text, uploaded icons and live Home Assistant
entity values composed on a canvas, served as a PNG or JPG from a stable URL.

- Editor: the add-on's **Web UI** (sidebar → *Kiosk Canvas*).
- Image URL per canvas: `/canvas/<id>.png` (or `.jpg`).
- Every request re-reads the entities and re-renders, so displays never show a
  stale picture.

## How it fits together

| Piece | Where | Notes |
| --- | --- | --- |
| Canvases (layout + elements) | `/data/canvases.json` | Included in Home Assistant backups with the add-on. |
| Uploaded images and icons | `/data/uploads/` | Also backed up; referenced by file name from a canvas. |
| Shared access token | `/data/access_token` | In the app's own file: `options.json` is rewritten by Home Assistant on restart, which would otherwise invalidate every saved URL. |
| Entity values | Home Assistant Supervisor API | The add-on has `homeassistant_api: true`, so it reads states itself — no user token, nothing to pass in the URL. |

## Image URLs

```
/canvas/<id>.png                          # the canvas at its design size
/canvas/<id>.jpg                          # JPEG, smaller payload
/canvas/<id>.png?w=1920&h=1080            # scale the whole design (text stays sharp)
/canvas/<id>.png?nocache=1                # force a completely fresh render
/uploads/<file>                           # an uploaded image on its own
```

Scaling renders the canvas again at the new size — geometry *and* font sizes
scale together — instead of resizing the finished picture, so text stays crisp.
If only one of `w`/`h` is given, the aspect ratio is kept.

### Authentication

Requests that arrive through Home Assistant ingress are already authenticated.
Direct requests — the ones automations, kiosks, media players and image tags make
— must carry the shared token as `?auth=…` (or an `X-Access-Token` header). The
editor prints the URL with the token included. Treat that URL as a secret: the
canvas *id* is guessable, the token is what protects it.

### Freshness

A canvas is rendered on request, and each image is reused for **2 seconds** while
entity states are reused for **2 seconds** — enough to keep a display polling
every second from re-rendering constantly, short enough that a change is visible
almost immediately. `?nocache=1` skips both, for a guaranteed current picture.

## Using the image

Add-on Web UI → *Image URL* panel gives you the exact URLs to copy.

**Notification (companion app)**

```yaml
actions:
  - service: notify.mobile_app_your_phone
    data:
      title: Oven
      message: "Dinner is ready"
      data:
        image: "http://homeassistant.local:8097/canvas/oven-finished.png?auth=YOUR-TOKEN"
```

For iOS use `data.attachment.url` with `data.attachment.content-type: png`.

**Media player / smart display**

```yaml
actions:
  - service: media_player.play_media
    target:
      entity_id: media_player.kitchen_display
    data:
      media_content_id: "http://homeassistant.local:8097/canvas/oven-finished.png?auth=YOUR-TOKEN"
      media_content_type: image
```

Caveats worth knowing: the *device* fetches this URL, so it must be reachable
from that device (LAN address, not a Home Assistant ingress path), and some TVs
and cast targets insist on HTTPS or reject URLs carrying query strings. If a
device refuses the token-style URL, expose the canvas as a camera instead and
hand the device that entity — Home Assistant then serves the image itself.

**As a camera**

```yaml
camera:
  - platform: generic
    name: Oven canvas
    still_image_url: "http://local-kiosk-canvas-displays:8097/canvas/oven-finished.png?auth=YOUR-TOKEN"
    scan_interval: 5
```

Home Assistant reaches an app installed from a local folder as `local_<slug>`
with underscores turned into hyphens — for this app that is
`local-kiosk-canvas-displays`. An app installed from a GitHub repository uses its
hashed repository id instead, and a LAN address such as `http://192.168.1.10:8097`
always works too.

## The editor

- **Add** text, image or entity elements; select one to edit it in the panel on
  the right.
- **Move**: drag the element (or use the arrow keys; hold Shift for 10px).
  Positions snap to an 8px grid — hold ⌥/Alt for free positioning.
- **Scale**: drag the blue corner handle. Dragging 200px doubles the size
  (leftwards halves it); for text and entity elements this changes the font size,
  for images the width (and height, if you set one).
- **Layers**: the list is the drawing order — items lower in the list draw on
  top. ▲▼ reorder, 👁 hides without deleting.
- **Real preview** renders the canvas on the server and shows that exact image,
  so wrapping, fonts and live values are what the display will show. The HTML
  view while editing is an approximation (browser fonts differ slightly).
- **Images**: uploading puts the picture in a grid of thumbnails, so an image
  already on the add-on can be picked again with one tap instead of re-uploading
  it. Each thumbnail has a ✕ that asks twice before deleting; a delete is refused
  while any canvas — including the one you are editing — still uses that image,
  and the reason is shown. The cell without a thumbnail clears the image.
- **Recent colours** sit under every colour picker: tap one to reuse it. They are
  remembered in the browser you are using.
- **Canvas sizes**: presets for common screens (1920×1080, 1080×1920, 1024×600, …)
  or any custom size from 64 to 4096 in each direction. Give the current size a
  name and **Save** it; saved sizes appear under *Saved sizes* in the size list on
  every new canvas, and can be re-applied or forgotten in the canvas panel. Like
  the recent colours, they are remembered in this browser (they are editor
  preferences, not part of a canvas, so they are not in Home Assistant backups).

### Element reference

Common: `x`, `y` (canvas pixels, the element's top-left), `rotation` (degrees),
`opacity` (0-100), `visible`, `name` (label in the layer list).

| Type | Options |
| --- | --- |
| `text` | `text` (newlines allowed), `size`, `colour`, `font` (system/serif/mono), `bold`, `italic`, `align`, `shadow`, `line_height`, `wrap` (pixel width, 0 = off) |
| `entity` | `entity` (e.g. `sensor.oven_temperature`), `attribute` (blank = the state itself, e.g. `battery_level`), `prefix`, `suffix`, `decimals` (−1 = as reported, 0-6 to round), `show_unit`, plus the text styling above |
| `image` | `file` (an uploaded name), `width`, `height` (0 = keep the aspect ratio; both set = stretch) |

### Live values in custom text

Inside a `text` element, `{{…}}` is replaced when the image is rendered:

```
Oven at {{sensor.oven_temperature}} · mode {{sensor.oven_mode}}
Battery: {{sensor.phone.battery_level}}%
```

An entity that is missing, `unknown` or `unavailable` renders as `—`, so a media
screen never shows the word "unavailable" in the middle of a message.

## Animations

Any element can move, to catch the eye on a screen that is being looked at from
across a room. Per element: pick an **Animation**, then **Seconds per cycle** and
an **Amount** (0 = the natural size for that effect, shown next to the field).

| Animation | What it does | Default amount |
| --- | --- | --- |
| Jump up and down | hops up and back, one hop per cycle | 26 px |
| Pulse | grows and shrinks | 16 % |
| Blink | flashes on and off | - |
| Shake | jitters left and right | 14 px |
| Wobble | rocks side to side | 9° |
| Slide in from the left | glides in and holds, repeating | 180 px |

Fetch the animation from an animated URL — the still URLs keep working:

```
/canvas/<id>.gif      animated GIF   (plays anywhere an image plays)
/canvas/<id>.webp     animated WebP  (smaller, better quality, less universal)
/canvas/<id>.png      one frame: the resting pose, i.e. exactly the design
```

- **Animation smoothness** is a canvas setting (10 / 15 / 25 / 40 / 50 fps,
  default 25). With one jumping headline at the default 720px width the GIF costs
  roughly 10 fps -> 86 KB, 15 -> 129 KB, 25 (default) -> 186 KB, 40 -> 244 KB,
  50 -> 316 KB; the same animation as WebP is about 40% smaller. Measurably
  smoother too: the largest per-frame movement of the headline drops from 7px at
  10 fps to 3px at 25 fps and 2px at 50 fps.
- **Elements share one loop.** One animated image can only run one loop, so the
  slowest animated element sets the period and every faster element's speed is
  rounded to a whole number of cycles inside it (a 1.2s hop next to a 2s pulse
  becomes a 2s loop with the hop running twice, i.e. every 1.0s). A loop that long
  needs more frames, so canvases mixing different speeds get noticeably bigger
  files - the demo above with a second animated line: 280 KB at 10 fps, 675 KB at
  25 fps. Elements sharing a speed share frames, and `?w=` still shrinks things.
- `?fps=40` overrides the smoothness for a single request and `?frames=N` asks for
  an explicit frame count; N acts as a ceiling, stepping down to a count that
  keeps the cycle time exact, so asking for more frames never speeds the animation
  up. GIF stores delays in 10ms steps and browsers bump anything under 20ms up to
  100ms, so GIF tops out near 40-50 fps; WebP keeps millisecond precision.
- `?w=` / `?h=` still apply. Animated output defaults to **720 px wide** when you
  do not pass a size, because a dozen-plus full frames at 1080p is a heavy file
  for a media device to pull.
- The cycle time is exact: frame delays are chosen on GIF's 10 ms grid, so an
  "every 1.2 s" hop loops every 1.2 s.

Which format for which device: **animated GIF** is the safe default — browsers,
dashboards, kiosk browsers and most media players show it. Some cast devices and
TV apps only render the first frame of a GIF or WebP; those want video instead
(MP4/WebM), which is not generated here because it needs ffmpeg in the add-on
image (available as an option if you need it — it makes the add-on a good deal
bigger). When a player shows a still, it shows the resting pose rather than
nothing.

Animated images are much larger than stills (roughly 5-20x for the same canvas);
they are re-rendered on request like any other canvas, so keep an eye on size if
a device polls the URL often.

## Fonts

The add-on image installs these families, and the editor's font list is built
from whatever is actually installed:

| Installed | Notes |
| --- | --- |
| DejaVu Sans / Serif / Sans Mono | Default; `system`, `serif` and `mono` on older canvases mean these. |
| Liberation Sans / Serif / Mono | Metric-compatible with Arial, Times New Roman and Courier New. |
| Noto Sans | Neutral and very legible at small sizes. |
| Roboto | Modern Android-style sans. |
| Inconsolata | Narrow monospace, good for numbers and codes. |
| JetBrains Mono | Wider monospace with tall x-height. |

Each family is offered in regular, bold, italic and bold-italic wherever the
package provides them (a single variable-font file covering several weights is
handled too).

Installation is **best-effort on purpose**: Alpine retires and renames font
families between releases — `font-ubuntu`, which this add-on used to install, no
longer exists in the Alpine version the base image is built on — so each font is
installed with its own `apk` call and a failure is reported in the build log
(`kiosk-canvas: <package> is not available in this Alpine release, skipping it`)
instead of breaking the build. The picker simply lists what the image has.

**Adding a family is a one-line change to the Dockerfile**: add `font-<name>` to
the list in that `RUN` line and rebuild. It appears in the picker automatically —
no code change, because font directories are scanned at runtime.

The editor is served these very files over `/fonts/<key>`, so its canvas uses the
same typeface and metrics as the rendered image: text position and line wrapping
agree to within about a pixel (verified by diffing a screenshot of the editor
canvas against the rendered PNG). This matters more than it sounds — the font
you choose changes wrapping, so a long line may fit in a narrow sans face and wrap
in a wide monospace one, and both the editor and the image wrap it identically.

## HTTP API

Useful for automations that create or change canvases, and for debugging.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/canvas/<id>.png` \| `.jpg` | The rendered canvas (also `/canvas/<id>`). |
| GET | `/api/canvases` | All canvases as JSON. |
| POST | `/api/canvases` | Create (body = canvas JSON). |
| PUT | `/api/canvases/<id>` | Replace a canvas. |
| DELETE | `/api/canvases/<id>` | Delete a canvas. |
| POST | `/api/preview` | Render a canvas JSON without saving it (what the editor's preview uses). |
| GET | `/api/entities?q=&limit=` | Entity picker data: id, name, state, unit. |
| GET | `/api/entity/<entity_id>?attribute=&decimals=&show_unit=` | One entity's current formatted value. |
| POST | `/api/uploads?name=<file>` | Upload an image (body = bytes; `Content-Type: image/png` etc.). |
| GET | `/uploads/<file>?w=120` | The image, or a thumbnail when `w`/`h` is given (what the editor's grid loads). |
| GET | `/api/uploads` | List uploaded images. |
| DELETE | `/api/uploads/<file>` | Delete an image (refused while a canvas uses it). |
| GET | `/health` | Liveness. |

Example — create a canvas from an automation:

```yaml
actions:
  - service: rest_command.canvas_create_alert
```
```yaml
rest_command:
  canvas_create_alert:
    url: "http://local-kiosk-canvas-displays:8097/api/canvases?auth=YOUR-TOKEN"
    method: POST
    content_type: "application/json"
    payload: >-
      {"name":"Doorbell","width":1280,"height":720,
       "background":{"color":"#111827","image":"","fit":"cover"},
       "elements":[{"type":"text","text":"Someone is at the door","x":80,"y":120,"size":72,"colour":"#f8fafc"}]}
```

Limits: canvases 64-4096 px per side, images up to 8 MB, at most ~16 megapixels
of output, 2000 characters of text, decimals −1 to 6.

## Troubleshooting

- **The editor loads but nothing responds to clicks.** The page's script did not
  run — the editor is written for browsers from around 2018 onwards, so an old
  tablet/WebView (a kiosk device, for instance) can fail to parse it. The editor
  now says so itself: a red banner appears at the top naming the error and the
  line, and `window.__kioskEditorReady` / `window.__kioskHasPointer` report the
  state (which input path it chose). If you see that banner, update the device's
  browser or use another one. Please report the banner text if it appears on a
  browser you consider modern.


- **Entity values show as dashes** — the add-on could not read states. Check the
  add-on log: it prints the reason (`homeassistant_api` missing, Home Assistant
  API unreachable, or an HTTP status). Text and icon canvases still render.
- **A device shows nothing** — it cannot reach the URL. Use the LAN address and
  check that port 8097 is exposed; through ingress the phone/display has no
  authenticated session for that path.
- **An image element is blank** — its upload is missing; re-upload it (the editor
  refuses to save a canvas that references a missing image).
- **The picture looks stale** — 2-second reuse window; add `?nocache=1` for an
  immediate re-render, and make sure the entity is actually changing in Home
  Assistant.
