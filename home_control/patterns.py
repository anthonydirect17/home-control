"""Light patterns.

A pattern turns (time in seconds, number of segments, params) into one RGB
color per segment. The effect engine calls it a few times a second and sends
the result to the light, so a pattern never talks to hardware itself.

Every pattern is listed in PATTERNS with its parameters, so the web page can
build its controls from this file alone.
"""

import colorsys
import math
import random

RGB = tuple  # (r, g, b), each 0-255


def hex_to_rgb(value: str) -> RGB:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        raise ValueError(f"not a hex color: {value!r}")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb: RGB) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(_clamp(c) for c in rgb))


def _clamp(v) -> int:
    return max(0, min(255, int(round(v))))


def _mix(a: RGB, b: RGB, t: float) -> RGB:
    return tuple(_clamp(x + (y - x) * t) for x, y in zip(a, b))


def _scale(c: RGB, k: float) -> RGB:
    return tuple(_clamp(x * k) for x in c)


def _colors(params, key="colors", default=("#FF0000", "#00FF00")):
    raw = params.get(key) or list(default)
    if isinstance(raw, str):
        raw = [part for part in raw.split(",") if part.strip()]
    return [hex_to_rgb(c) for c in raw] or [hex_to_rgb(default[0])]


def solid(t, n, p):
    c = hex_to_rgb(p.get("color", "#FFB46B"))
    return [c] * n


def breathe(t, n, p):
    c = hex_to_rgb(p.get("color", "#FF0000"))
    period = max(0.5, float(p.get("period", 4)))
    low = float(p.get("low", 0.15))
    k = low + (1 - low) * (0.5 - 0.5 * math.cos(2 * math.pi * t / period))
    return [_scale(c, k)] * n


def color_cycle(t, n, p):
    cols = _colors(p, default=("#FF0000", "#00FF00", "#FFFFFF"))
    hold = max(0.2, float(p.get("seconds_per_color", 3)))
    pos = t / hold
    i = int(pos) % len(cols)
    frac = pos - int(pos)
    fade = min(1.0, max(0.0, float(p.get("fade", 0.5))))
    k = 0.0 if frac < 1 - fade else (frac - (1 - fade)) / fade if fade else 0.0
    return [_mix(cols[i], cols[(i + 1) % len(cols)], k)] * n


def chase(t, n, p):
    cols = _colors(p, default=("#FF0000", "#FFFFFF"))
    width = max(1, int(p.get("width", 2)))
    speed = float(p.get("speed", 4))  # segments per second; negative runs backwards
    shift = int(math.floor(t * speed))
    return [cols[((i - shift) // width) % len(cols)] for i in range(n)]


def rainbow(t, n, p):
    speed = float(p.get("speed", 0.1))  # full hue turns per second
    spread = float(p.get("spread", 1.0))  # how many rainbows fit on the strip
    out = []
    for i in range(n):
        h = (t * speed + spread * i / max(1, n)) % 1.0
        out.append(tuple(_clamp(x * 255) for x in colorsys.hsv_to_rgb(h, 1, 1)))
    return out


def twinkle(t, n, p):
    base = hex_to_rgb(p.get("base", "#1A0F00"))
    sparkle = _colors(p, key="sparkle", default=("#FFFFFF",))
    density = min(1.0, max(0.0, float(p.get("density", 0.2))))
    rate = max(0.2, float(p.get("rate", 2)))  # twinkle steps per second
    step = int(t * rate)
    frac = t * rate - step
    out = []
    for i in range(n):
        rng = random.Random(step * 7919 + i * 104729)
        if rng.random() < density:
            peak = sparkle[rng.randrange(len(sparkle))]
            out.append(_mix(base, peak, math.sin(math.pi * frac)))
        else:
            out.append(base)
    return out


def candy_cane(t, n, p):
    return chase(t, n, {"colors": ["#FF0000", "#FFFFFF"], "width": p.get("width", 2), "speed": p.get("speed", 2)})


def christmas(t, n, p):
    cols = [hex_to_rgb(c) for c in ("#FF0000", "#00C000", "#FFFFFF")]
    period = max(1.0, float(p.get("period", 6)))
    k = 0.5 - 0.5 * math.cos(2 * math.pi * t / period)
    out = []
    for i in range(n):
        a = cols[i % 3]
        b = cols[(i + 1) % 3]
        out.append(_mix(a, b, k))
    return out


def _two(p, a="#FF0000", b="#000000"):
    return hex_to_rgb(p.get("color", a)), hex_to_rgb(p.get("color2", b))


def _level(p):
    return min(1.0, max(0.05, float(p.get("brightness", 1.0))))


def pulse(t, n, p):
    """A quick rise to the first color, then a fade back toward the second: a heartbeat or an alarm."""
    c1, c2 = _two(p)
    rate = max(0.1, float(p.get("rate", 1.5)))  # pulses per second
    frac = (t * rate) % 1.0
    k = frac / 0.15 if frac < 0.15 else math.exp(-4 * (frac - 0.15))
    return [_scale(_mix(c2, c1, k), _level(p))] * n


def wave(t, n, p):
    """The two colors roll along the lights like a wave."""
    c1, c2 = _two(p, "#A77BFF", "#38C8FF")
    speed = float(p.get("speed", 0.5))       # waves passing per second; negative runs backwards
    spread = max(0.1, float(p.get("spread", 1.0)))  # waves across the strip (or across a group)
    out = []
    for i in range(n):
        k = 0.5 + 0.5 * math.sin(2 * math.pi * (spread * i / max(1, n) - t * speed))
        out.append(_scale(_mix(c2, c1, k), _level(p)))
    return out


def strobe(t, n, p):
    """Short flashes, alternating between the two colors. Needs a frame rate at least twice the flash rate."""
    c1, c2 = _two(p, "#FFFFFF", "#000000")
    rate = max(0.2, float(p.get("rate", 2)))   # flashes per second
    duty = min(0.9, max(0.05, float(p.get("on", 0.5))))
    frac = (t * rate) % 1.0
    return [_scale(c1 if frac < duty else c2, _level(p))] * n


# name -> (function, label, [param specs]). Param kinds the page understands:
# color, colors (comma-separated list), number (with min/max/step).
PATTERNS = {
    "solid": (solid, "Solid color", [
        {"key": "color", "kind": "color", "default": "#FFB46B"},
    ]),
    "breathe": (breathe, "Breathe", [
        {"key": "color", "kind": "color", "default": "#FF0000"},
        {"key": "period", "kind": "number", "default": 4, "min": 0.5, "max": 30, "step": 0.5, "label": "Seconds per breath"},
        {"key": "low", "kind": "number", "default": 0.15, "min": 0, "max": 0.9, "step": 0.05, "label": "Dimmest level"},
    ]),
    "color_cycle": (color_cycle, "Color cycle", [
        {"key": "colors", "kind": "colors", "default": "#FF0000,#00FF00,#FFFFFF"},
        {"key": "seconds_per_color", "kind": "number", "default": 3, "min": 0.2, "max": 60, "step": 0.1},
        {"key": "fade", "kind": "number", "default": 0.5, "min": 0, "max": 1, "step": 0.05, "label": "Fade portion"},
    ]),
    "chase": (chase, "Chase", [
        {"key": "colors", "kind": "colors", "default": "#FF0000,#FFFFFF"},
        {"key": "width", "kind": "number", "default": 2, "min": 1, "max": 20, "step": 1, "label": "Block width"},
        {"key": "speed", "kind": "number", "default": 4, "min": -20, "max": 20, "step": 0.5, "label": "Segments per second"},
    ]),
    "rainbow": (rainbow, "Rainbow", [
        {"key": "speed", "kind": "number", "default": 0.1, "min": 0, "max": 2, "step": 0.05, "label": "Turns per second"},
        {"key": "spread", "kind": "number", "default": 1, "min": 0.1, "max": 5, "step": 0.1, "label": "Rainbows on the strip"},
    ]),
    "twinkle": (twinkle, "Twinkle", [
        {"key": "base", "kind": "color", "default": "#1A0F00", "label": "Background"},
        {"key": "sparkle", "kind": "colors", "default": "#FFFFFF", "label": "Sparkle colors"},
        {"key": "density", "kind": "number", "default": 0.2, "min": 0, "max": 1, "step": 0.05},
        {"key": "rate", "kind": "number", "default": 2, "min": 0.2, "max": 10, "step": 0.1, "label": "Twinkles per second"},
    ]),
    "candy_cane": (candy_cane, "Candy cane", [
        {"key": "width", "kind": "number", "default": 2, "min": 1, "max": 20, "step": 1, "label": "Stripe width"},
        {"key": "speed", "kind": "number", "default": 2, "min": -20, "max": 20, "step": 0.5, "label": "Segments per second"},
    ]),
    "pulse": (pulse, "Pulse", [
        {"key": "color", "kind": "color", "default": "#FF0000"},
        {"key": "color2", "kind": "color", "default": "#000000", "label": "Between pulses"},
        {"key": "rate", "kind": "number", "default": 1.5, "min": 0.1, "max": 5, "step": 0.1, "label": "Pulses per second"},
        {"key": "brightness", "kind": "number", "default": 1, "min": 0.05, "max": 1, "step": 0.05},
    ]),
    "wave": (wave, "Wave", [
        {"key": "color", "kind": "color", "default": "#A77BFF"},
        {"key": "color2", "kind": "color", "default": "#38C8FF"},
        {"key": "speed", "kind": "number", "default": 0.5, "min": -5, "max": 5, "step": 0.1, "label": "Waves per second"},
        {"key": "spread", "kind": "number", "default": 1, "min": 0.1, "max": 5, "step": 0.1, "label": "Waves on the strip"},
        {"key": "brightness", "kind": "number", "default": 1, "min": 0.05, "max": 1, "step": 0.05},
    ]),
    "strobe": (strobe, "Strobe", [
        {"key": "color", "kind": "color", "default": "#FFFFFF"},
        {"key": "color2", "kind": "color", "default": "#000000", "label": "Between flashes"},
        {"key": "rate", "kind": "number", "default": 2, "min": 0.2, "max": 8, "step": 0.1, "label": "Flashes per second"},
        {"key": "on", "kind": "number", "default": 0.5, "min": 0.05, "max": 0.9, "step": 0.05, "label": "Share of time on"},
        {"key": "brightness", "kind": "number", "default": 1, "min": 0.05, "max": 1, "step": 0.05},
    ]),
    "christmas": (christmas, "Christmas fade", [
        {"key": "period", "kind": "number", "default": 6, "min": 1, "max": 60, "step": 0.5, "label": "Seconds per cycle"},
    ]),
}


def render(name: str, t: float, n: int, params: dict):
    if name not in PATTERNS:
        raise KeyError(f"unknown pattern {name!r}")
    fn = PATTERNS[name][0]
    return fn(t, max(1, n), params or {})


def is_static(name: str) -> bool:
    """Patterns whose output never changes over time only need sending once."""
    return name == "solid"


def catalog():
    return [{"name": k, "label": v[1], "params": v[2]} for k, v in PATTERNS.items()]
