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
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, features
    HAVE_PIL = True
    HAVE_WEBP = features.check("webp")
except ImportError:  # pragma: no cover - depends on the runtime image
    HAVE_PIL = False
    HAVE_WEBP = False

DATA_FILE = Path("/data/canvases.json")
UPLOAD_DIR = Path("/data/uploads")
EDITOR_FILE = Path("/app/web/editor.html")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "").strip()
SUPERVISOR_API = "http://supervisor/core/api"

CANVAS_MIN, CANVAS_MAX = 64, 4096
ELEMENT_TYPES = ("text", "image", "entity")
# Per-element attention animations. "none" renders the element as designed; every
# other kind returns to the design pose at the start of each cycle, so the still
# PNG/JPG (frame 0) is always the untouched layout.
ANIMATIONS = ("none", "jump", "pulse", "blink", "shake", "wobble", "slide")
ANIMATION_DEFAULTS = {"jump": 26, "shake": 14, "slide": 180, "wobble": 9, "pulse": 16, "blink": 0}
ANIMATION_FRAMES = 30           # frames per cycle when a caller asks for a count
ANIMATION_FPS = 25              # default smoothness: smooth without bloating the file
ANIMATION_FPS_RANGE = (5, 50)   # browsers clamp sub-20ms delays up to 100ms, so 50 fps is the ceiling
ANIMATED_FORMATS = ("GIF", "WEBP")
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

# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #
# Font files are discovered from the system font directories rather than being
# hard-coded per family: the picker offers whatever the image ships (the
# Dockerfile installs DejaVu, Liberation, Noto and Roboto), and adding another
# package needs no code change.
# The editor is served these exact files over /fonts/<key>, which is what makes
# the editor preview and the rendered image agree: same typeface, same metrics.
FONT_DIRS = (
    "/usr/share/fonts", "/usr/local/share/fonts",
    "/System/Library/Fonts/Supplemental", "/Library/Fonts", "/System/Library/Fonts",
)
FONT_SUFFIXES = (".ttf", ".otf")
# Picker order: the sans faces people reach for first, then serif, then mono.
FONT_PREFERENCE = ("dejavu-sans", "liberation-sans", "noto-sans", "roboto", "dejavu-serif",
                   "liberation-serif", "dejavu-sans-mono", "liberation-mono",
                   "inconsolata", "jetbrains-mono")
# A filename cannot tell "DejaVu Sans" from "Deja Vu Sans" — and these keys are
# what FONT_ALIASES points at and what canvases store, so the families the
# image ships get their canonical name and key here. Anything else falls back to
# a slug derived from the filename, which only has to be stable.
FONT_CANONICAL = {
    "dejavusansmono": ("dejavu-sans-mono", "DejaVu Sans Mono"),
    "dejavusanscondensed": ("dejavu-sans-condensed", "DejaVu Sans Condensed"),
    "dejavusans": ("dejavu-sans", "DejaVu Sans"),
    "dejavuserif": ("dejavu-serif", "DejaVu Serif"),
    "liberationsans": ("liberation-sans", "Liberation Sans"),
    "liberationserif": ("liberation-serif", "Liberation Serif"),
    "liberationmono": ("liberation-mono", "Liberation Mono"),
    "notosansmono": ("noto-sans-mono", "Noto Sans Mono"),
    "notoserif": ("noto-serif", "Noto Serif"),
    "notosans": ("noto-sans", "Noto Sans"),
    "robotomono": ("roboto-mono", "Roboto Mono"),
    "robotocondensed": ("roboto-condensed", "Roboto Condensed"),
    "roboto": ("roboto", "Roboto"),
    "inconsolata": ("inconsolata", "Inconsolata"),
    "jetbrainsmono": ("jetbrains-mono", "JetBrains Mono"),
    "ubuntumono": ("ubuntu-mono", "Ubuntu Mono"),
    "ubuntu": ("ubuntu", "Ubuntu"),
    "arialnarrow": ("arial-narrow", "Arial Narrow"),
    "arial": ("arial", "Arial"),
    "timesnewroman": ("times-new-roman", "Times New Roman"),
    "couriernew": ("courier-new", "Courier New"),
    "georgia": ("georgia", "Georgia"),
    "verdana": ("verdana", "Verdana"),
    "tahoma": ("tahoma", "Tahoma"),
    "helveticaneue": ("helvetica-neue", "Helvetica Neue"),
}
# Canvas documents written before the font list existed used these names.
FONT_ALIASES = {"system": "dejavu-sans", "sans": "dejavu-sans", "sans-serif": "dejavu-sans",
                "serif": "dejavu-serif", "mono": "dejavu-sans-mono", "monospace": "dejavu-sans-mono"}
FONT_STYLE_WORDS = ("bolditalic", "boldoblique", "semibolditalic", "bold", "semibold",
                    "italic", "oblique", "regular", "book", "medium", "light")
DEFAULT_FONT = "dejavu-sans"
FONT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,119}$")
FONT_REGISTRY: dict[str, dict] = {}
FONT_REGISTRY_LOCK = threading.Lock()


def font_label(family: str) -> str:
    """DejaVuSans -> 'DejaVu Sans', NotoSans[wdth,wght] -> 'Noto Sans'."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(family))
    spaced = re.sub(r"[\[\](){}]", " ", spaced)
    # Variable-font axis lists are packaging detail, not part of the name.
    spaced = re.sub(r"\b(wdth|wght|ital|opsz|slnt|grad|xtra|full)\b", " ", spaced, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", spaced.replace(",", " ")).strip()


def font_identity(family: str) -> tuple[str, str]:
    """(key, label) for a family token, using the canonical name where known so
    that DejaVuSans-Bold.ttf keys as 'dejavu-sans' — the key FONT_ALIASES and
    FONT_PREFERENCE are written against."""
    token = re.sub(r"[^a-z0-9]+", "", str(family).lower())
    if token in FONT_CANONICAL:
        return FONT_CANONICAL[token]
    label = font_label(family)
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-"), label or str(family)


def font_category(label: str) -> str:
    lowered = label.lower()
    if "mono" in lowered or "courier" in lowered:
        return "monospace"
    if "serif" in lowered and "sans" not in lowered:
        return "serif"
    return "sans-serif"


def discover_fonts() -> dict[str, dict]:
    """Font key -> {'label': str, 'files': {(bold, italic): path}}."""
    with FONT_REGISTRY_LOCK:
        if FONT_REGISTRY:
            return FONT_REGISTRY
        found: dict[str, dict] = {}
        for directory in FONT_DIRS:
            root = Path(directory)
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in FONT_SUFFIXES:
                    continue  # .ttc collections are skipped: indexing them is guesswork
                stem = path.stem
                lowered = stem.lower()
                bold = "bold" in lowered
                italic = "italic" in lowered or "oblique" in lowered
                family = stem.replace(" ", "-")
                for word in FONT_STYLE_WORDS:
                    family = re.sub(word, "", family, flags=re.IGNORECASE)
                family = re.sub(r"-{2,}", "-", family).strip("-") or stem
                key, label = font_identity(family)
                if not key:
                    continue
                entry = found.setdefault(key, {"label": label, "files": {}})
                entry["files"].setdefault((bold, italic), str(path))
        FONT_REGISTRY.update(found)
        return FONT_REGISTRY


def font_choices() -> list[dict]:
    """What the editor offers, installed-and-preferred families first."""
    registry = discover_fonts()
    def order(key: str) -> tuple:
        return (FONT_PREFERENCE.index(key), "") if key in FONT_PREFERENCE else (len(FONT_PREFERENCE), key)
    return [{"key": key, "label": entry["label"], "category": font_category(entry["label"]),
             "bold": any(style[0] for style in entry["files"]),
             "italic": any(style[1] for style in entry["files"])}
            for key, entry in sorted(registry.items(), key=lambda item: order(item[0]))]


def font_file(key: str, style: str = "regular") -> Path | None:
    """Resolve a font key (or legacy alias) plus a style name to a file."""
    registry = discover_fonts()
    key = key if key in registry else FONT_ALIASES.get(str(key).lower(), "")
    entry = registry.get(key)
    if not entry or not entry["files"]:
        return None
    wanted = {"bold": (True, False), "italic": (False, True), "bolditalic": (True, True),
              "bold-italic": (True, True)}.get(str(style).lower(), (False, False))
    # Fall back through the closest styles rather than refusing to render.
    for candidate in (wanted, (wanted[0], False), (False, wanted[1]), (False, False)):
        if entry["files"].get(candidate):
            return Path(entry["files"][candidate])
    return Path(sorted(entry["files"].values())[0])


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
    # Animation applies to every element kind.
    animation = str(payload.get("animation", existing.get("animation", "none"))).strip().lower()
    element.update({
        "animation": animation if animation in ANIMATIONS else "none",
        "animation_speed": clamp_number(payload.get("animation_speed", existing.get("animation_speed")), 0.2, 10, 1.2),
        "animation_amount": clamp_number(payload.get("animation_amount", existing.get("animation_amount")), 0, 600, 0),
    })
    if kind == "image":
        element.update({
            "file": as_text(payload.get("file", existing.get("file", "")), 128),
            "width": clamp_number(payload.get("width", existing.get("width")), 0, CANVAS_MAX, 240),
            "height": clamp_number(payload.get("height", existing.get("height")), 0, CANVAS_MAX, 0),
        })
        return element
    family = str(payload.get("font", existing.get("font", DEFAULT_FONT))).strip().lower()
    alignment = str(payload.get("align", existing.get("align", "left"))).strip().lower()
    element.update({
        "size": clamp_number(payload.get("size", existing.get("size")), 6, 512, 48),
        "colour": as_colour(payload.get("colour", existing.get("colour")), "#f8fafc"),
        "font": family if FONT_KEY_RE.match(family) else DEFAULT_FONT,
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
        # Smoothness of the animated GIF/WebP output, in frames per second.
        "animation_fps": int(clamp_number(payload.get("animation_fps", existing.get("animation_fps")),
                                          ANIMATION_FPS_RANGE[0], ANIMATION_FPS_RANGE[1], ANIMATION_FPS)),
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


def load_font(name: str, size: int, bold: bool, italic: bool):
    """A font file for the requested family/style, falling back to the closest
    available style and finally to Pillow's built-in font, so a missing package
    degrades instead of failing the render."""
    key = (str(name), max(6, int(size)), bool(bold), bool(italic))
    cached = FONT_CACHE.get(key)
    if cached is not None:
        return cached
    font = None
    registry = discover_fonts()
    resolved = registry.get(str(name)) or registry.get(FONT_ALIASES.get(str(name).lower(), ""))
    if resolved:
        wanted = (bool(bold), bool(italic))
        for candidate in (wanted, (wanted[0], False), (False, wanted[1]), (False, False)):
            path = resolved["files"].get(candidate)
            if path:
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
    elif bold or italic:
        # Some packages ship one variable font for every weight/slant (Alpine's
        # Noto is like this). Ask the file for the named instance we want; static
        # fonts and Pillow builds without that API simply stay as they are.
        wanted = "Bold Italic" if (bold and italic) else ("Bold" if bold else "Italic")
        try:
            names = [name.decode() if isinstance(name, bytes) else str(name)
                     for name in font.get_variation_names()]
            for candidate in (wanted, "Bold", "Italic"):
                if candidate in names:
                    font.set_variation_by_name(candidate)
                    break
        except Exception:
            pass  # not a variable font, or no variation support: nothing to do
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

    Returns (layer, (content_width, content_height)) — the layer carries
    symmetric padding for the shadow, so the layer centre *is* the element's
    centre; the caller needs the content size to rotate and place it the way CSS
    does.

    Vertical placement mirrors a CSS line box: the leftover leading is split
    above the ascender, which is exactly where Pillow anchors text (anchor 'la').
    Without this, text sits a few pixels off from the editor at large sizes."""
    size = max(6, int(round(number(element.get("size"), 48) * scale)))
    font = load_font(str(element.get("font", DEFAULT_FONT)), size, as_flag(element.get("bold"), True),
                     as_flag(element.get("italic"), False))
    line_height = max(0.6, number(element.get("line_height"), 1.15))
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    wrap = number(element.get("wrap"), 0) * scale
    lines = wrap_lines(measure, content, font, int(wrap)) if wrap > 0 else content.split("\n")
    if not lines:
        lines = [""]
    widths = [text_width(measure, line, font) for line in lines]
    block_width = max(1, max(widths))
    line_step = max(1, int(round(size * line_height)))
    try:
        ascent, descent = font.getmetrics()
    except AttributeError:  # Pillow's bitmap fallback
        ascent, descent = int(size * 0.8), int(size * 0.2)
    leading = (line_step - (ascent + descent)) / 2
    block_height = line_step * len(lines)
    padding = max(2, int(round(size * 0.12)))
    layer = Image.new("RGBA", (block_width + padding * 2, block_height + padding * 2), (0, 0, 0, 0))
    body = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    painter = ImageDraw.Draw(body)
    colour = hex_rgb(as_colour(element.get("colour"), "#f8fafc")) + (255,)
    align = str(element.get("align", "left"))
    positions = []
    for index, line in enumerate(lines):
        if align == "center":
            x = padding + (block_width - widths[index]) / 2
        elif align == "right":
            x = padding + (block_width - widths[index])
        else:
            x = padding
        positions.append((x, padding + index * line_step + leading))
    for (x, y), line in zip(positions, lines):
        painter.text((x, y), line, font=font, fill=colour)
    if as_flag(element.get("shadow"), True):
        # A soft shadow, the equivalent of the editor's CSS '0 1px 3px': blur the
        # glyphs underneath instead of stamping a hard offset copy.
        shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        softener = ImageDraw.Draw(shadow)
        off_y = max(1, int(round(size * 0.02)))
        for (x, y), line in zip(positions, lines):
            softener.text((x, y + off_y), line, font=font, fill=(0, 0, 0, 205))
        shadow = shadow.filter(ImageFilter.GaussianBlur(max(1.0, size * 0.035)))
        layer = Image.alpha_composite(shadow, body)
    else:
        layer = body
    return layer, (block_width, block_height)


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
    return source, source.size


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


THUMB_CACHE: dict[tuple, bytes] = {}


def upload_thumbnail(path: Path, width: int | None, height: int | None) -> bytes | None:
    """Downscaled PNG preview for the editor's image grid. Never upscales, so a
    small icon is returned as-is; the grid asks for ~120px instead of pulling
    multi-megabyte uploads."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), width, height, stat.st_mtime_ns)
    cached = THUMB_CACHE.get(key)
    if cached:
        return cached
    try:
        with Image.open(path) as source:
            source.load()
            image = source.convert("RGBA")
    except Exception:
        return None
    target_width = width or round(image.width * (height / image.height))
    target_height = height or round(image.height * (width / image.width))
    if image.width > target_width or image.height > target_height:
        ratio = min(target_width / image.width, target_height / image.height)
        image = image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))), RESAMPLE)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    data = buffer.getvalue()
    if len(THUMB_CACHE) > 300:
        THUMB_CACHE.clear()
    THUMB_CACHE[key] = data
    return data


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


def animation_offset(element: dict, progress: float) -> dict:
    """Where an element sits inside its animation cycle (progress 0..1).

    Every kind is back at the design pose at progress 0, so a still render is
    always the canvas exactly as it was laid out, and an animated loop returns to
    it every cycle."""
    kind = str(element.get("animation", "none"))
    if kind not in ANIMATIONS or kind == "none":
        return {"dx": 0.0, "dy": 0.0, "scale": 1.0, "opacity": 1.0, "rotate": 0.0}
    amount = number(element.get("animation_amount"), 0) or ANIMATION_DEFAULTS.get(kind, 0)
    angle = 2 * math.pi * progress
    if kind == "jump":
        return {"dx": 0.0, "dy": -amount * abs(math.sin(math.pi * progress)),
                "scale": 1.0, "opacity": 1.0, "rotate": 0.0}
    if kind == "shake":
        return {"dx": amount * math.sin(2 * angle), "dy": 0.0,
                "scale": 1.0, "opacity": 1.0, "rotate": 0.0}
    if kind == "wobble":
        return {"dx": 0.0, "dy": 0.0, "scale": 1.0, "opacity": 1.0, "rotate": amount * math.sin(angle)}
    if kind == "pulse":
        # amount is a percentage here: 16 grows the element by 16% at the peak
        return {"dx": 0.0, "dy": 0.0, "scale": 1 + (amount / 100) * math.sin(angle),
                "opacity": 1.0, "rotate": 0.0}
    if kind == "blink":
        return {"dx": 0.0, "dy": 0.0, "scale": 1.0,
                "opacity": 1.0 if progress < 0.5 else 0.12, "rotate": 0.0}
    # slide: glide in from the left, then hold until the cycle restarts
    return {"dx": -amount * max(0.0, 1 - min(1.0, progress / 0.45)), "dy": 0.0,
            "scale": 1.0, "opacity": 1.0, "rotate": 0.0}


def animation_cycles(element: dict, period: float) -> int:
    """How many of an element's own cycles fit in the shared animation period.

    Rounding to a whole number is what keeps the animated image seamless when
    elements ask for different speeds."""
    speed = clamp_number(element.get("animation_speed"), 0.2, 10, 1.2)
    return max(1, int(round(period / speed))) if period > 0 else 1


def animation_timing(canvas: dict, period: float, image_format: str,
                     frames: int | None = None, fps: float | None = None) -> tuple[int, int]:
    """(frame count, per-frame delay in ms) for the animated output.

    A caller can ask for an explicit frame count (`?frames=`) or a smoothness in
    frames per second (`?fps=`, or the canvas's own setting). Two constraints keep
    the result playing correctly rather than merely looking right in one viewer:
      * GIF stores delays in 10ms steps, so the delay is snapped to that grid and
        the count derived from it — otherwise the animation drifts off-speed;
      * browsers clamp delays under 20ms up to 100ms, so 50 fps is the ceiling.
    WebP keeps millisecond precision, so it can hold the exact requested rate."""
    gif = image_format.upper() == "GIF"

    def snap(delay_ms: float) -> int:
        if gif:
            return max(20, int(delay_ms / 10.0 + 0.5) * 10)
        return max(10, int(round(delay_ms)))

    if frames:
        # An explicit count is a ceiling: step down to a count whose delay lands
        # on the format's grid, so asking for more frames can never change how
        # fast the animation actually plays.
        wanted = int(clamp(int(frames), 2, 150))
        for count in range(wanted if wanted % 2 == 0 else wanted - 1, 1, -2):
            delay = snap(period * 1000 / count)
            if abs(count * delay - period * 1000) <= 10:
                return count, delay
        return 2, snap(period * 500)
    wanted = clamp_number(fps if fps is not None else canvas.get("animation_fps"),
                          ANIMATION_FPS_RANGE[0], ANIMATION_FPS_RANGE[1], ANIMATION_FPS)
    delay = snap(1000.0 / wanted)
    count = int(clamp(int(round(period * 1000 / delay)), 2, 150))
    if count % 2 == 1:
        count += 1
    return count, snap(period * 1000 / count)


def canvas_animates(canvas: dict) -> bool:
    return any(as_flag(item.get("visible"), True) and str(item.get("animation", "none")) not in ("", "none")
               for item in canvas.get("elements", []))


def animation_period(canvas: dict) -> float:
    speeds = [clamp_number(item.get("animation_speed"), 0.2, 10, 1.2)
              for item in canvas.get("elements", [])
              if as_flag(item.get("visible"), True) and str(item.get("animation", "none")) not in ("", "none")]
    return max(speeds) if speeds else 1.2


def draw_frame(canvas: dict, out_width: int, out_height: int, scale: float,
               elapsed: float, period: float, lookup) -> Image.Image:
    """One frame of the canvas, `elapsed` seconds into the shared period."""
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
        built = (image_layer(element, scale) if element.get("type") == "image"
                 else text_layer(element, element_text(element, lookup), scale))
        if built is None:
            continue  # a missing upload must not lose the rest of the canvas
        layer, (content_width, content_height) = built
        # Progress through *this element's* cycle: the shared period divided by the
        # element's own speed, so a 0.6s hop runs twice inside a 1.2s period.
        cycles = animation_cycles(element, period)
        motion = animation_offset(element, (elapsed * cycles / period) % 1.0 if period else 0.0)
        if motion["scale"] != 1.0:
            layer = layer.resize((max(1, int(round(layer.width * motion["scale"]))),
                                  max(1, int(round(layer.height * motion["scale"])))), RESAMPLE)
            content_width *= motion["scale"]
            content_height *= motion["scale"]
        layer = apply_alpha(rotate_layer(layer, number(element.get("rotation"), 0) + motion["rotate"]),
                            clamp_number(element.get("opacity"), 0, 100, 100) * motion["opacity"])
        # Rotate about the element's own centre and put that centre where the
        # element's box puts it, offset by any animation movement — the same
        # thing CSS does with its default transform-origin.
        centre_x = number(element.get("x"), 0) * scale + content_width / 2 + motion["dx"] * scale
        centre_y = number(element.get("y"), 0) * scale + content_height / 2 + motion["dy"] * scale
        image.paste(layer, (int(round(centre_x - layer.width / 2)), int(round(centre_y - layer.height / 2))), layer)
    return image


def encode_image(image: Image.Image, frames: list[Image.Image], image_format: str,
                 duration_ms: int | None = None) -> tuple[bytes, str]:
    """Encode one image, or a list of frames as an animated GIF/WebP."""
    buffer = io.BytesIO()
    kind = image_format.upper()
    if kind in ANIMATED_FORMATS and frames:
        if kind == "WEBP":
            image.save(buffer, format="WEBP", save_all=True, append_images=frames, duration=duration_ms,
                       loop=0, quality=82, method=4)
            return buffer.getvalue(), "image/webp"
        # Full frames rather than optimised partial ones: partial-frame GIFs need
        # matching disposal handling in every viewer, and some media players get
        # that wrong (visible smearing).
        image.save(buffer, format="GIF", save_all=True, append_images=frames, duration=duration_ms,
                   loop=0, disposal=2)
        return buffer.getvalue(), "image/gif"
    if kind == "GIF":
        image.convert("RGB").save(buffer, format="GIF")           # one frame, still a GIF
        return buffer.getvalue(), "image/gif"
    if kind == "WEBP":
        image.convert("RGB").save(buffer, format="WEBP", quality=88, method=4)
        return buffer.getvalue(), "image/webp"
    if kind in ("JPG", "JPEG"):
        image.convert("RGB").save(buffer, format="JPEG", quality=88, optimize=True)
        return buffer.getvalue(), "image/jpeg"
    image.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), "image/png"


def render_canvas(canvas: dict, width: int | None = None, height: int | None = None,
                  image_format: str = "PNG", frames: int | None = None,
                  fps: float | None = None) -> tuple[bytes, str]:
    """Render a canvas to image bytes. `width`/`height` scale the whole design
    (geometry and font sizes together, so text stays sharp) instead of resizing
    the finished picture.

    Animated elements turn a GIF/WebP request into a looping animation; every
    other format (and a canvas with no animation) yields the rest pose."""
    if not HAVE_PIL:
        raise ValueError("Image rendering needs Pillow, which this app image does not have.")
    design_width = max(1, int(number(canvas.get("width"), 1280)))
    design_height = max(1, int(number(canvas.get("height"), 720)))
    animated = canvas_animates(canvas) and image_format.upper() in ANIMATED_FORMATS
    if animated and not width and not height:
        # Sixteen full frames at 1920px is a multi-megabyte download for a media
        # player; cap the default animated size (override with ?w=/?h=).
        width = min(design_width, 720)
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
    # One state read per render, shared by every frame.
    lookup = state_lookup(needs_states)

    if not animated:
        return encode_image(draw_frame(canvas, out_width, out_height, scale, 0.0, 0.0, lookup), [], image_format)

    period = animation_period(canvas)
    count, duration = animation_timing(canvas, period, image_format, frames=frames, fps=fps)
    rendered = [draw_frame(canvas, out_width, out_height, scale, period * index / count, period, lookup)
                for index in range(count)]
    return encode_image(rendered[0], rendered[1:], image_format, duration)


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
        if requested in ("jpg", "jpeg"): return "JPEG"
        if requested == "gif": return "GIF"
        if requested == "webp": return "WEBP" if HAVE_WEBP else "GIF"   # no libwebp: GIF instead
        return "PNG"

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
        frames_raw = str(self.query().get("frames", [""])[0]).strip()
        fps_raw = str(self.query().get("fps", [""])[0]).strip()
        frames = int(clamp_number(frames_raw, 2, 150, ANIMATION_FRAMES)) if frames_raw else None
        fps = (float(clamp_number(fps_raw, ANIMATION_FPS_RANGE[0], ANIMATION_FPS_RANGE[1], ANIMATION_FPS))
               if fps_raw else None)
        # Frame count and smoothness change the bytes, so they belong in the key.
        key = key + (frames, fps)
        try:
            data, content_type = render_canvas(canvas, width, height, image_format, frames, fps)
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
        frames_raw = str(self.query().get("frames", [""])[0]).strip()
        fps_raw = str(self.query().get("fps", [""])[0]).strip()
        frames = int(clamp_number(frames_raw, 2, 150, ANIMATION_FRAMES)) if frames_raw else None
        fps = (float(clamp_number(fps_raw, ANIMATION_FPS_RANGE[0], ANIMATION_FPS_RANGE[1], ANIMATION_FPS))
               if fps_raw else None)
        try:
            data, content_type = render_canvas(canvas, width, height, self.image_format(), frames, fps)
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

    def send_font(self, key: str) -> None:
        """Serve a discovered font file to the editor so it previews with the
        same typeface the renderer uses."""
        path = font_file(key, str(self.query().get("style", ["regular"])[0]))
        if not path or not path.exists():
            return self.send_json({"error": "No such font."}, 404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "font/otf" if path.suffix.lower() == ".otf" else "font/ttf")
        # Font files never change while the add-on runs, so unlike canvases they
        # are safe (and worth) caching.
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_upload(self, name: str) -> None:
        path = upload_path(name)
        if not path or not path.exists():
            return self.send_json({"error": "No such image."}, 404)
        query = self.query()
        width = int(clamp(number(query.get("w", [0])[0], 0), 0, 2048)) or None
        height = int(clamp(number(query.get("h", [0])[0], 0), 0, 2048)) or None
        if (width or height) and HAVE_PIL:
            thumb = upload_thumbnail(path, width, height)
            if thumb:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                # An upload's name is unique, so its thumbnail never changes.
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(thumb)))
                self.end_headers()
                self.wfile.write(thumb)
                return
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
        if path == "/api/fonts":
            return self.send_json({"items": font_choices(), "aliases": FONT_ALIASES, "default": DEFAULT_FONT})
        canvas_match = re.fullmatch(r"/canvas/([a-z0-9-]+)(?:\.(png|jpg|jpeg|gif|webp))?", path)
        if canvas_match:
            return self.canvas_image(canvas_match.group(1), canvas_match.group(2))
        font_match = re.fullmatch(r"/fonts/([a-z0-9-]+)", path)
        if font_match:
            return self.send_font(font_match.group(1))
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
