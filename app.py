"""Kiosk Canvas Displays.

Compose text, icons and live Home Assistant entity values on a canvas, then
serve that canvas as a PNG or JPG over a stable URL (/canvas/<id>.png) for
automations, kiosk browsers and media-player screens. Every request re-reads the
entity states from Home Assistant and re-renders, so the image is never stale.

No browser and no build step: the image is composed server-side with Pillow,
which keeps the add-on working on the small architectures (armv7/armhf/i386)
where a headless Chromium is not available.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import secrets
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except ImportError:  # pragma: no cover - depends on the runtime image
    HAVE_PIL = False

DATA_FILE = Path("/data/canvases.json")
UPLOAD_DIR = Path("/data/uploads")
EDITOR_FILE = Path("/app/web/editor.html")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "").strip()
SUPERVISOR_API = "http://supervisor/core/api"

CANVAS_MIN, CANVAS_MAX = 64, 4096
ELEMENT_TYPES = ("text", "image", "entity")
DISPLAY_FONTS = ("system", "serif", "mono")
ALIGNMENTS = ("left", "center", "right")
UPLOAD_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
UPLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ENTITY_RE = re.compile(r"^[a-z_]+(\.[a-z0-9_]+)+$")
NUMBER_RE = re.compile(r"^-?\d+(?:[.,]\d+)?$")
MAX_UPLOAD = 8_000_000
STATE_TTL = 2.0        # seconds entity states are reused (editor + polling players)
RENDER_TTL = 2.0       # seconds a finished image is reused
MAX_PREVIEW_PIXELS = 4096 * 4096

DEFAULT_BACKGROUND = {"color": "#0f172a", "image": "", "fit": "cover"}
DEFAULT_CANVAS = {"name": "New canvas", "width": 1280, "height": 720,
                  "background": dict(DEFAULT_BACKGROUND), "elements": []}

# Text/icon elements and the entity values they contain are drawn with DejaVu
# (font-dejavu in the image); the built-in Pillow font is the fallback so a
# missing font degrades instead of failing the render.
FONT_FILES = {
    "system": {
        (False, False): ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        (True, False): ("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        (False, True): ("/usr/share/fonts/dejavu/DejaVuSans-Oblique.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"),
        (True, True): ("/usr/share/fonts/dejavu/DejaVuSans-BoldOblique.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf"),
    },
    "serif": {
        (False, False): ("/usr/share/fonts/dejavu/DejaVuSerif.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"),
        (True, False): ("/usr/share/fonts/dejavu/DejaVuSerif-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"),
        (False, True): ("/usr/share/fonts/dejavu/DejaVuSerif-Italic.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf"),
        (True, True): ("/usr/share/fonts/dejavu/DejaVuSerif-BoldItalic.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf"),
    },
    "mono": {
        (False, False): ("/usr/share/fonts/dejavu/DejaVuSansMono.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
        (True, False): ("/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"),
        (False, True): ("/usr/share/fonts/dejavu/DejaVuSansMono-Oblique.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Oblique.ttf"),
        (True, True): ("/usr/share/fonts/dejavu/DejaVuSansMono-BoldOblique.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-BoldOblique.ttf"),
    },
}

STATE_CACHE: dict[str, tuple[float, dict[str, dict]]] = {}
STATE_LOCK = threading.Lock()
RENDER_CACHE: dict[tuple, tuple[float, bytes]] = {}
RENDER_LOCK = threading.Lock()
FONT_CACHE: dict[tuple, object] = {}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def number(value, fallback: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def as_text(value, limit: int) -> str:
    return str(value if value is not None else "")[:limit]


def as_flag(value, fallback: bool = False) -> bool:
    """Booleans arrive from JSON as true/false but from form posts as 'true'."""
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("", "false", "0", "off", "no")


def as_colour(value, fallback: str) -> str:
    text = str(value or "").strip()
    if COLOUR_RE.match(text):
        return text.lower()
    if re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return "#" + text.lower()
    return fallback


def clamp_number(value, low: float, high: float, fallback: float) -> float:
    return clamp(number(value, fallback), low, high)


def hex_rgb(value: str) -> tuple[int, int, int]:
    text = str(value).lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        text = "ffffff"
    return tuple(int(text[index:index + 2], 16) for index in (0, 2, 4))


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def load_canvases() -> list[dict]:
    try:
        value = json.loads(DATA_FILE.read_text())
        return value if isinstance(value, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_canvases(canvases: list[dict]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(canvases, indent=2) + "\n")
    temporary.replace(DATA_FILE)


def access_token() -> str:
    """Shared token for connections that did not come through HA ingress.

    Kept in the app's OWN file rather than options.json: Home Assistant rewrites
    options.json from the add-on configuration on every restart, which would
    regenerate the token and break every saved image URL.
    """
    token_file = DATA_FILE.parent / "access_token"
    try:
        token = token_file.read_text().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = token_file.with_suffix(".tmp")
        temporary.write_text(token + "\n")
        temporary.replace(token_file)
    except OSError:
        return ""
    return token


def upload_path(name: str) -> Path | None:
    """Resolve an upload name inside UPLOAD_DIR, refusing anything that could
    escape it (the name comes from canvas JSON, which is user input)."""
    if not UPLOAD_NAME_RE.match(name or ""):
        return None
    candidate = (UPLOAD_DIR / name).resolve()
    root = UPLOAD_DIR.resolve()
    return candidate if candidate.parent == root else None


# --------------------------------------------------------------------------- #
# Home Assistant entity data (Supervisor API proxy)
# --------------------------------------------------------------------------- #
def states() -> dict[str, dict]:
    """Every entity state, keyed by entity_id, cached for a couple of seconds.

    One call fills the cache for the whole render (a canvas may show several
    entities) and absorbs the polling of media players and the editor's live
    previews instead of asking Home Assistant on every single request.
    """
    with STATE_LOCK:
        cached = STATE_CACHE.get("states")
        if cached and time.time() - cached[0] < STATE_TTL:
            return cached[1]
    if not SUPERVISOR_TOKEN:
        raise ValueError("Home Assistant entity values are unavailable: this app needs "
                         "homeassistant_api access and must run under Home Assistant.")
    request = Request(f"{SUPERVISOR_API}/states",
                      headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=15) as response:
            listed = json.load(response)
    except HTTPError as error:
        raise ValueError(f"Home Assistant API returned HTTP {error.code}.") from None
    except (URLError, OSError):
        raise ValueError("Home Assistant API is unreachable.") from None
    mapping = {item.get("entity_id", ""): item for item in listed if item.get("entity_id")}
    with STATE_LOCK:
        STATE_CACHE["states"] = (time.time(), mapping)
    return mapping


def entity_listing(query: str = "", limit: int = 400) -> list[dict]:
    """Entities for the editor's picker: id, friendly name, current state, unit."""
    needle = (query or "").strip().lower()
    items = []
    for entity_id, state in sorted(states().items()):
        attributes = state.get("attributes") or {}
        name = as_text(attributes.get("friendly_name") or entity_id, 120)
        if needle and needle not in entity_id.lower() and needle not in name.lower():
            continue
        items.append({"entity_id": entity_id, "name": name, "state": as_text(state.get("state"), 200),
                      "unit": as_text(attributes.get("unit_of_measurement") or "", 24)})
    return items[:limit]


def format_value(state: dict | None, attribute: str, decimals: int, show_unit: bool) -> str:
    """The text an entity element shows: an attribute when asked for, its unit
    when it has one, and a dash for anything missing or unavailable (a media
    screen should never show 'unavailable' in the middle of a message)."""
    if not state:
        return "—"
    attributes = state.get("attributes") or {}
    value = attributes.get(attribute) if attribute else state.get("state")
    if value is None:
        return "—"
    if isinstance(value, bool):
        text = "On" if value else "Off"
    elif isinstance(value, (int, float)):
        text = f"{value:.{decimals}f}" if decimals >= 0 else f"{value:g}"
    else:
        text = str(value)
        if text.strip().lower() in ("", "unknown", "unavailable", "none", "null"):
            return "—"
        numeric = NUMBER_RE.match(text.strip())
        if numeric and decimals >= 0:
            try:
                text = f"{float(text.replace(',', '.')):.{decimals}f}"
            except ValueError:
                pass
    if show_unit:
        unit = as_text(attributes.get("unit_of_measurement") or "", 24)
        if unit and not text.endswith(unit):
            text = f"{text} {unit}"
    return text


TOKEN_RE = re.compile(r"\{\{\s*([a-z_]+(?:\.[a-z0-9_]+)+)(?:\.([A-Za-z0-9_]+))?\s*\}\}")


def expand_tokens(text: str, lookup) -> str:
    """Replace {{entity_id}} / {{entity_id.attribute}} inside custom text, so a
    sentence can mix fixed wording with live values ("Oven is at
    {{sensor.oven_temperature}}")."""
    def replace(match):
        state = lookup(match.group(1))
        if state is None:
            return "—"
        if match.group(2):
            value = (state.get("attributes") or {}).get(match.group(2))
            return "—" if value is None else as_text(value, 60)
        return format_value(state, "", -1, False)
    return TOKEN_RE.sub(replace, text)


# --------------------------------------------------------------------------- #
# Canvas model
# --------------------------------------------------------------------------- #
def slugify(value: str) -> str:
    identifier = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower()).strip("-")[:48]
    if not identifier:
        raise ValueError("The canvas name does not produce a valid ID.")
    return identifier


def clean_element(payload: dict, existing: dict | None = None) -> dict:
    existing = existing or {}
    kind = str(payload.get("type") or existing.get("type") or "").strip().lower()
    if kind not in ELEMENT_TYPES:
        raise ValueError("Each element must be a text, image or entity element.")
    element = {
        "id": as_text(existing.get("id") or payload.get("id") or uuid.uuid4().hex[:10], 40),
        "type": kind,
        "name": as_text(payload.get("name", existing.get("name", "")), 60),
        "x": clamp_number(payload.get("x", existing.get("x")), -CANVAS_MAX, CANVAS_MAX, 40),
        "y": clamp_number(payload.get("y", existing.get("y")), -CANVAS_MAX, CANVAS_MAX, 40),
        "rotation": clamp_number(payload.get("rotation", existing.get("rotation")), -360, 360, 0),
        "opacity": clamp_number(payload.get("opacity", existing.get("opacity")), 0, 100, 100),
        "visible": as_flag(payload.get("visible", existing.get("visible")), True),
    }
    if kind == "image":
        element.update({
            "file": as_text(payload.get("file", existing.get("file", "")), 128),
            "width": clamp_number(payload.get("width", existing.get("width")), 0, CANVAS_MAX, 240),
            "height": clamp_number(payload.get("height", existing.get("height")), 0, CANVAS_MAX, 0),
        })
        return element
    family = str(payload.get("font", existing.get("font", "system"))).strip().lower()
    alignment = str(payload.get("align", existing.get("align", "left"))).strip().lower()
    element.update({
        "size": clamp_number(payload.get("size", existing.get("size")), 6, 512, 48),
        "colour": as_colour(payload.get("colour", existing.get("colour")), "#f8fafc"),
        "font": family if family in DISPLAY_FONTS else "system",
        "bold": as_flag(payload.get("bold", existing.get("bold")), True),
        "italic": as_flag(payload.get("italic", existing.get("italic")), False),
        "align": alignment if alignment in ALIGNMENTS else "left",
        "shadow": as_flag(payload.get("shadow", existing.get("shadow")), True),
        "line_height": clamp_number(payload.get("line_height", existing.get("line_height")), 0.6, 3.0, 1.15),
        "wrap": clamp_number(payload.get("wrap", existing.get("wrap")), 0, CANVAS_MAX, 0),
    })
    if kind == "entity":
        entity_id = str(payload.get("entity", existing.get("entity", ""))).strip().lower()
        if entity_id and not ENTITY_RE.match(entity_id):
            raise ValueError("That does not look like an entity ID (for example sensor.oven_temperature).")
        element.update({
            "entity": entity_id,
            "attribute": as_text(payload.get("attribute", existing.get("attribute", "")), 60),
            "prefix": as_text(payload.get("prefix", existing.get("prefix", "")), 80),
            "suffix": as_text(payload.get("suffix", existing.get("suffix", "")), 80),
            "decimals": clamp_number(payload.get("decimals", existing.get("decimals")), -1, 6, -1),
            "show_unit": as_flag(payload.get("show_unit", existing.get("show_unit")), True),
        })
        return element
    element["text"] = as_text(payload.get("text", existing.get("text", "")), 2000)
    return element


def clean_canvas(payload: dict, existing: dict | None = None) -> dict:
    existing = existing or {}
    name = as_text(payload.get("name", existing.get("name", DEFAULT_CANVAS["name"])), 80).strip()
    if not name:
        raise ValueError("Give the canvas a name.")
    background = payload.get("background") if isinstance(payload.get("background"), dict) else {}
    previous = existing.get("background") if isinstance(existing.get("background"), dict) else {}
    fit = str(background.get("fit", previous.get("fit", "cover"))).strip().lower()
    uploads = set()
    canvas = {
        "id": as_text(existing.get("id") or payload.get("id") or slugify(name), 48),
        "name": name,
        "width": int(clamp_number(payload.get("width", existing.get("width")), CANVAS_MIN, CANVAS_MAX, 1280)),
        "height": int(clamp_number(payload.get("height", existing.get("height")), CANVAS_MIN, CANVAS_MAX, 720)),
        "background": {
            "color": as_colour(background.get("color", previous.get("color")), "#0f172a"),
            "image": as_text(background.get("image", previous.get("image", "")), 128),
            "fit": fit if fit in ("cover", "contain", "stretch") else "cover",
        },
        "elements": [],
    }
    if not canvas["id"]:
        raise ValueError("The canvas name does not produce a valid ID.")
    seen = set()
    previous_elements = {item.get("id"): item for item in existing.get("elements", []) if isinstance(item, dict)}
    for item in payload.get("elements", existing.get("elements", [])):  # type: ignore[union-attr]
        if not isinstance(item, dict):
            continue
        element = clean_element(item, previous_elements.get(item.get("id")))
        if element["id"] in seen:  # ids are how the editor tracks elements
            element["id"] = uuid.uuid4().hex[:10]
        seen.add(element["id"])
        if element["type"] == "image" and element["file"]:
            uploads.add(element["file"])
        canvas["elements"].append(element)
    if canvas["background"]["image"]:
        uploads.add(canvas["background"]["image"])
    missing = [name for name in uploads if not upload_path(name) or not (UPLOAD_DIR / name).exists()]
    if missing:
        raise ValueError(f"Uploaded image(s) not found: {', '.join(sorted(missing))}. Re-upload them.")
    return canvas


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
RESAMPLE = getattr(getattr(Image, "Resampling", Image), "BICUBIC") if HAVE_PIL else None
DOWNSCALE = getattr(getattr(Image, "Resampling", Image), "LANCZOS") if HAVE_PIL else None


def load_font(family: str, size: int, bold: bool, italic: bool):
    """A DejaVu file for the requested style, falling back to Pillow's built-in
    font: a canvas must still render if the font package is missing."""
    key = (family, max(6, int(size)), bool(bold), bool(italic))
    cached = FONT_CACHE.get(key)
    if cached is not None:
        return cached
    font = None
    candidates = FONT_FILES.get(family, FONT_FILES["system"]).get((bool(bold), bool(italic)), ())
    for path in candidates:
        if Path(path).exists():
            try:
                font = ImageFont.truetype(path, key[1])
                break
            except OSError:
                continue
    if font is None:
        try:
            font = ImageFont.load_default(size=key[1])
        except TypeError:  # Pillow < 10.1 has no size argument
            font = ImageFont.load_default()
    FONT_CACHE[key] = font
    return font


def text_width(painter, text: str, font) -> int:
    left, _, right, _ = painter.textbbox((0, 0), text, font=font)
    return max(0, right - left)


def wrap_lines(painter, text: str, font, limit: int) -> list[str]:
    """Greedy word wrap to a pixel width; a single over-long word is kept whole
    rather than being chopped mid-word."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if limit <= 0 or text_width(painter, paragraph, font) <= limit:
            lines.append(paragraph)
            continue
        current = ""
        for word in paragraph.split(" "):
            candidate = f"{current} {word}".strip()
            if current and text_width(painter, candidate, font) > limit:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return lines


def element_text(element: dict, lookup) -> str:
    """What the element says right now: custom text (with any {{entity}} tokens
    resolved), or the entity's formatted value with its prefix/suffix."""
    if element["type"] == "text":
        return expand_tokens(element.get("text", ""), lookup)
    state = lookup(element.get("entity", "")) if element.get("entity") else None
    value = format_value(state, element.get("attribute", ""), int(element.get("decimals", -1)),
                         as_flag(element.get("show_unit"), True))
    return f"{element.get('prefix', '')}{value}{element.get('suffix', '')}"


def text_layer(element: dict, content: str, scale: float):
    """Render a text/entity element onto its own RGBA layer.

    The layer's top-left IS the text's top-left, so an element's x/y means the
    same thing in the editor and in the rendered image (the only extra is a
    little padding on the right/bottom for the drop shadow)."""
    size = max(6, int(round(number(element.get("size"), 48) * scale)))
    font = load_font(str(element.get("font", "system")), size, as_flag(element.get("bold"), True),
                     as_flag(element.get("italic"), False))
    line_height = max(0.6, number(element.get("line_height"), 1.15))
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    wrap = number(element.get("wrap"), 0) * scale
    lines = wrap_lines(measure, content, font, int(wrap)) if wrap > 0 else content.split("\n")
    if not lines:
        lines = [""]
    widths = [text_width(measure, line, font) for line in lines]
    # A bitmap fallback font reports no bearings, hence the floor of 1px.
    block_width = max(1, max(widths))
    line_step = max(1, int(round(size * line_height)))
    padding = max(2, int(round(size * 0.12)))
    layer = Image.new("RGBA", (block_width + padding, line_step * len(lines) + padding), (0, 0, 0, 0))
    painter = ImageDraw.Draw(layer)
    colour = hex_rgb(as_colour(element.get("colour"), "#f8fafc")) + (255,)
    shadow = as_flag(element.get("shadow"), True)
    align = str(element.get("align", "left"))
    for index, line in enumerate(lines):
        if align == "center":
            x = (block_width - widths[index]) / 2
        elif align == "right":
            x = block_width - widths[index]
        else:
            x = 0
        y = index * line_step
        if shadow:
            offset = max(1, int(round(size * 0.05)))
            painter.text((x + offset, y + offset), line, font=font, fill=(0, 0, 0, 220))
        painter.text((x, y), line, font=font, fill=colour)
    return layer


def image_layer(element: dict, scale: float) -> Image.Image | None:
    """Load an uploaded PNG/JPG for an image element, sized by the element's
    width (height 0 keeps the native aspect ratio)."""
    path = upload_path(element.get("file", ""))
    if not path or not path.exists():
        return None
    try:
        source = Image.open(path)
        source.load()
    except Exception:
        return None
    source = source.convert("RGBA")
    native_width, native_height = source.size
    width = number(element.get("width"), 240) * scale
    height = number(element.get("height"), 0) * scale
    if width <= 0 and height <= 0:
        width, height = native_width * scale, native_height * scale
    elif width <= 0:
        width = height * native_width / max(1, native_height)
    elif height <= 0:
        height = width * native_height / max(1, native_width)
    width, height = max(1, int(round(width))), max(1, int(round(height)))
    if (width, height) != (native_width, native_height):
        method = DOWNSCALE if width * height < native_width * native_height else RESAMPLE
        source = source.resize((width, height), method)
    return source


def apply_alpha(layer: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 100:
        return layer
    alpha = layer.getchannel("A").point(lambda value: int(value * max(0.0, opacity) / 100))
    faded = layer.copy()
    faded.putalpha(alpha)
    return faded


def rotate_layer(layer: Image.Image, degrees: float) -> Image.Image:
    if not degrees:
        return layer
    return layer.rotate(-degrees, resample=RESAMPLE, expand=True)  # negative: clockwise on screen


def background_image(canvas: dict) -> Image.Image | None:
    name = (canvas.get("background") or {}).get("image", "")
    if not name:
        return None
    path = upload_path(name)
    if not path or not path.exists():
        return None
    try:
        image = Image.open(path)
        image.load()
        return image.convert("RGB")
    except Exception:
        return None


def fit_background(source: Image.Image, width: int, height: int, mode: str, colour: str) -> Image.Image:
    """cover = fill and crop, contain = fit inside with background bars,
    stretch = fill ignoring the aspect ratio."""
    if mode == "stretch":
        return source.resize((width, height), RESAMPLE)
    ratio = (max(width / source.width, height / source.height) if mode == "cover"
             else min(width / source.width, height / source.height))
    scaled = source.resize((max(1, int(source.width * ratio)), max(1, int(source.height * ratio))), RESAMPLE)
    canvas = Image.new("RGB", (width, height), hex_rgb(colour))
    left = (scaled.width - width) // 2 if mode == "cover" else 0
    top = (scaled.height - height) // 2 if mode == "cover" else 0
    canvas.paste(scaled, (-left, -top))
    return canvas


def state_lookup(needed: bool):
    """Resolve entity values during a render. A canvas made only of text and
    icons must render even when Home Assistant is unreachable, and one that does
    need entity values should still produce a picture (with dashes) rather than
    a broken image — the add-on log carries the reason."""
    if not needed:
        return lambda entity_id: None
    try:
        return states().get
    except ValueError as error:
        print(f"Canvas render: {error} Showing dashes for entity values.", flush=True)
        return lambda entity_id: None


def render_canvas(canvas: dict, width: int | None = None, height: int | None = None,
                  image_format: str = "PNG") -> tuple[bytes, str]:
    """Render a canvas to image bytes. `width`/`height` scale the whole design
    (geometry and font sizes together, so text stays sharp) instead of resizing
    the finished picture."""
    if not HAVE_PIL:
        raise ValueError("Image rendering needs Pillow, which this app image does not have.")
    design_width = max(1, int(number(canvas.get("width"), 1280)))
    design_height = max(1, int(number(canvas.get("height"), 720)))
    if width and height:
        scale = min(width / design_width, height / design_height)
    elif width:
        scale = width / design_width
    elif height:
        scale = height / design_height
    else:
        scale = 1.0
    scale = clamp(scale, 0.02, 12.0)
    out_width = max(1, int(round(design_width * scale)))
    out_height = max(1, int(round(design_height * scale)))
    if out_width * out_height > MAX_PREVIEW_PIXELS:
        raise ValueError(f"{out_width}x{out_height} is too large; keep images under "
                         f"{MAX_PREVIEW_PIXELS // 1_000_000} megapixels.")

    needs_states = any(item.get("type") == "entity" or (TOKEN_RE.search(str(item.get("text", ""))) is not None)
                       for item in canvas.get("elements", []))
    lookup = state_lookup(needs_states)
    background = canvas.get("background") or {}
    colour = as_colour(background.get("color"), "#0f172a")
    source = background_image(canvas)
    if source is not None:
        image = fit_background(source, out_width, out_height, str(background.get("fit", "cover")), colour)
    else:
        image = Image.new("RGB", (out_width, out_height), hex_rgb(colour))

    for element in canvas.get("elements", []):
        if not as_flag(element.get("visible"), True):
            continue
        if element.get("type") == "image":
            layer = image_layer(element, scale)
            if layer is None:
                continue  # a missing upload must not lose the rest of the canvas
        else:
            layer = text_layer(element, element_text(element, lookup), scale)
        layer = apply_alpha(layer, clamp_number(element.get("opacity"), 0, 100, 100))
        layer = rotate_layer(layer, number(element.get("rotation"), 0))
        position = (int(round(number(element.get("x"), 0) * scale)), int(round(number(element.get("y"), 0) * scale)))
        image.paste(layer, position, layer)

    buffer = io.BytesIO()
    if image_format.upper() in ("JPG", "JPEG"):
        image.convert("RGB").save(buffer, format="JPEG", quality=88, optimize=True)
        return buffer.getvalue(), "image/jpeg"
    image.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), "image/png"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "KioskCanvas/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(fmt % args, flush=True)

    # -- request plumbing ---------------------------------------------------
    def is_ingress(self) -> bool:
        """True for requests proxied by Home Assistant's ingress (already
        authenticated there)."""
        return bool(self.headers.get("X-Hassio-Ingress") or self.headers.get("X-Forwarded-For")
                    or self.headers.get("X-Forwarded-Host"))

    def authorized(self) -> bool:
        """Ingress passes; direct connections need the shared token, because
        automations, kiosks and media players fetch the image URL without a HA
        session."""
        if self.is_ingress():
            return True
        expected = access_token()
        if not expected:
            return True  # token unavailable (e.g. a dev box): fail open, not locked out
        supplied = parse_qs(urlparse(self.path).query).get("auth", [""])[0] or self.headers.get("X-Access-Token", "")
        return secrets.compare_digest(supplied, expected)

    def query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    def payload(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object.")
        return value

    def send_json(self, value: object, status: int = 200) -> None:
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        # no-store everywhere: an image URL must always show the current entity
        # values, never a picture cached by the display's browser.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, value: str, status: int = 200) -> None:
        self.send_bytes(value.encode(), "text/html; charset=utf-8", status)

    def find(self, identifier: str) -> tuple[list[dict], int]:
        canvases = load_canvases()
        for index, canvas in enumerate(canvases):
            if canvas.get("id") == identifier:
                return canvases, index
        raise KeyError("Canvas not found.")

    def size_params(self) -> tuple[int | None, int | None]:
        query = self.query()
        width = int(clamp(number(query.get("w", query.get("width", [0]))[0], 0), 0, CANVAS_MAX))
        height = int(clamp(number(query.get("h", query.get("height", [0]))[0], 0), 0, CANVAS_MAX))
        return width or None, height or None

    def image_format(self, suffix: str | None = None) -> str:
        requested = (suffix or self.query().get("format", [""])[0] or "").lower()
        return "JPEG" if requested in ("jpg", "jpeg") else "PNG"

    # -- pages --------------------------------------------------------------
    def editor(self) -> None:
        try:
            page = EDITOR_FILE.read_text()
        except FileNotFoundError:
            return self.send_html("<h1>Kiosk Canvas Displays</h1><p>Editor file is missing.</p>", 500)
        token = access_token()
        self.send_html(page.replace("__ACCESS_TOKEN__", token))

    # -- image endpoints ----------------------------------------------------
    def canvas_image(self, identifier: str, suffix: str | None) -> None:
        try:
            _, index = self.find(identifier)
        except KeyError as error:
            return self.send_json({"error": str(error)}, 404)
        canvas = load_canvases()[index]
        width, height = self.size_params()
        image_format = self.image_format(suffix)
        fresh = as_flag(self.query().get("nocache", ["0"])[0])
        key = (identifier, width, height, image_format, json.dumps(canvas, sort_keys=True))
        if fresh:
            # Force the next render to re-read entity states as well, so a caller
            # can demand a genuinely current picture (tests, or right after an event).
            with STATE_LOCK:
                STATE_CACHE.pop("states", None)
        if not fresh:
            with RENDER_LOCK:
                cached = RENDER_CACHE.get(key)
                if cached and time.time() - cached[0] < RENDER_TTL:
                    return self.send_bytes(cached[1][0], cached[1][1])
        try:
            data, content_type = render_canvas(canvas, width, height, image_format)
        except ValueError as error:
            return self.send_json({"error": str(error)}, 502)
        with RENDER_LOCK:
            if len(RENDER_CACHE) > 40:
                RENDER_CACHE.clear()
            RENDER_CACHE[key] = (time.time(), (data, content_type))
        self.send_bytes(data, content_type)

    def preview(self) -> None:
        """Render unsaved editor state, so the preview in the editor is the real
        server-rendered picture (font metrics, wrapping, entity values)."""
        try:
            canvas = clean_canvas(self.payload())
        except (ValueError, json.JSONDecodeError) as error:
            return self.send_json({"error": str(error)}, 400)
        width, height = self.size_params()
        try:
            data, content_type = render_canvas(canvas, width, height, self.image_format())
        except ValueError as error:
            return self.send_json({"error": str(error)}, 502)
        self.send_bytes(data, content_type)

    # -- canvases -----------------------------------------------------------
    def canvases(self) -> None:
        self.send_json(load_canvases())

    def create_canvas(self) -> None:
        try:
            canvases = load_canvases()
            canvas = clean_canvas(self.payload())
            if any(item.get("id") == canvas["id"] for item in canvases):
                raise ValueError("A canvas with this name already exists.")
            canvases.append(canvas)
            save_canvases(canvases)
            self.send_json(canvas, HTTPStatus.CREATED)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def update_canvas(self, identifier: str) -> None:
        try:
            canvases, index = self.find(identifier)
            canvases[index] = clean_canvas(self.payload(), canvases[index])
            save_canvases(canvases)
            self.send_json(canvases[index])
        except (KeyError, ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def delete_canvas(self, identifier: str) -> None:
        try:
            canvases, index = self.find(identifier)
            canvases.pop(index)
            save_canvases(canvases)
            self.send_json({"ok": True})
        except KeyError as error:
            self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)

    # -- entities -----------------------------------------------------------
    def entities(self) -> None:
        query = self.query().get("q", [""])[0]
        limit = int(clamp(number(self.query().get("limit", [400])[0], 400), 1, 2000))
        try:
            self.send_json({"items": entity_listing(query, limit)})
        except ValueError as error:
            self.send_json({"error": str(error)}, 502)

    def entity_value(self, entity_id: str) -> None:
        query = self.query()
        attribute = query.get("attribute", [""])[0]
        decimals = int(clamp(number(query.get("decimals", [-1])[0], -1), -1, 6))
        show_unit = as_flag(query.get("show_unit", ["true"])[0], True)
        try:
            state = states().get(entity_id)
        except ValueError as error:
            return self.send_json({"error": str(error)}, 502)
        if state is None:
            return self.send_json({"error": "Unknown entity."}, 404)
        attributes = state.get("attributes") or {}
        self.send_json({
            "entity_id": entity_id,
            "name": as_text(attributes.get("friendly_name") or entity_id, 120),
            "state": as_text(state.get("state"), 200),
            "unit": as_text(attributes.get("unit_of_measurement") or "", 24),
            "value": format_value(state, attribute, decimals, show_unit),
            "attributes": {key: value for key, value in list(attributes.items())[:40]
                           if isinstance(value, (str, int, float, bool))},
        })

    # -- uploaded images ----------------------------------------------------
    def uploads(self) -> None:
        try:
            names = sorted(item.name for item in UPLOAD_DIR.iterdir() if item.is_file())
        except FileNotFoundError:
            names = []
        self.send_json({"items": names})

    def save_upload(self) -> None:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        extension = UPLOAD_TYPES.get(content_type)
        if not extension:
            return self.send_json({"error": "Upload a PNG, JPG, WEBP or GIF image."}, 400)
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return self.send_json({"error": "The upload was empty."}, 400)
        if length > MAX_UPLOAD:
            return self.send_json({"error": f"Images must be smaller than {MAX_UPLOAD // 1_000_000} MB."}, 413)
        data = self.rfile.read(length)
        if not data:
            return self.send_json({"error": "The upload was empty."}, 400)
        wanted = unquote(self.query().get("name", [""])[0])
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.splitext(wanted)[0]).strip("-._")[:60] or "image"
        name = f"{stem}{extension}"
        try:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            if (UPLOAD_DIR / name).exists():
                name = f"{stem}-{uuid.uuid4().hex[:6]}{extension}"  # never overwrite a used image
            (UPLOAD_DIR / name).write_bytes(data)
        except OSError as error:
            return self.send_json({"error": f"Could not store the image: {error}"}, 500)
        self.send_json({"name": name, "url": f"uploads/{name}"}, HTTPStatus.CREATED)

    def send_upload(self, name: str) -> None:
        path = upload_path(name)
        if not path or not path.exists():
            return self.send_json({"error": "No such image."}, 404)
        content_type = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "webp": "image/webp", "gif": "image/gif"}.get(path.suffix.lstrip(".").lower(),
                                                                     "application/octet-stream")
        self.send_bytes(path.read_bytes(), content_type)

    def delete_upload(self, name: str) -> None:
        path = upload_path(name)
        if not path or not path.exists():
            return self.send_json({"error": "No such image."}, 404)
        in_use = [canvas.get("id") for canvas in load_canvases()
                  if any(element.get("file") == name for element in canvas.get("elements", []))
                  or (canvas.get("background") or {}).get("image") == name]
        if in_use:
            return self.send_json({"error": f"That image is still used by: {', '.join(in_use)}."}, 409)
        try:
            path.unlink()
        except OSError as error:
            return self.send_json({"error": f"Could not delete the image: {error}"}, 500)
        self.send_json({"ok": True})

    # -- routing ------------------------------------------------------------
    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path).rstrip("/") or "/"
        if path == "/health":
            return self.send_json({"ok": True, "engine": "pillow" if HAVE_PIL else None})
        if not self.authorized():
            return self.send_json({"error": "Unauthorized — append ?auth=<access token> (see the app's editor page)."}, 401)
        if path == "/":
            return self.editor()
        if path == "/api/canvases":
            return self.canvases()
        if path == "/api/entities":
            return self.entities()
        if path == "/api/uploads":
            return self.uploads()
        canvas_match = re.fullmatch(r"/canvas/([a-z0-9-]+)(?:\.(png|jpg|jpeg))?", path)
        if canvas_match:
            return self.canvas_image(canvas_match.group(1), canvas_match.group(2))
        upload_suffix = re.fullmatch(r"/(?:uploads|api/uploads)/([A-Za-z0-9._-]+)", path)
        if upload_suffix:
            return self.send_upload(upload_suffix.group(1))
        entity_match = re.fullmatch(r"/api/entity/([A-Za-z0-9_.]+)", path)
        if entity_match:
            return self.entity_value(entity_match.group(1).lower())
        self.send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        if not self.authorized():
            return self.send_json({"error": "Unauthorized."}, 401)
        if path == "/api/canvases":
            return self.create_canvas()
        if path == "/api/preview":
            return self.preview()
        if path == "/api/uploads":
            return self.save_upload()
        self.send_json({"error": "Not found"}, 404)

    def do_PUT(self) -> None:
        if not self.authorized():
            return self.send_json({"error": "Unauthorized."}, 401)
        match = re.fullmatch(r"/api/canvases/([a-z0-9-]+)", urlparse(self.path).path.rstrip("/"))
        if match:
            return self.update_canvas(match.group(1))
        self.send_json({"error": "Not found"}, 404)

    def do_DELETE(self) -> None:
        if not self.authorized():
            return self.send_json({"error": "Unauthorized."}, 401)
        path = urlparse(self.path).path.rstrip("/")
        canvas_match = re.fullmatch(r"/api/canvases/([a-z0-9-]+)", path)
        if canvas_match:
            return self.delete_canvas(canvas_match.group(1))
        upload_match = re.fullmatch(r"/api/uploads/([A-Za-z0-9._-]+)", path)
        if upload_match:
            return self.delete_upload(unquote(upload_match.group(1)))
        self.send_json({"error": "Not found"}, 404)


if __name__ == "__main__":
    print(f"Starting Kiosk Canvas Displays on port 8097 (Pillow: {HAVE_PIL}, "
          f"Supervisor token: {'yes' if SUPERVISOR_TOKEN else 'no'})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8097), Handler).serve_forever()
