"""Build Geyser Genomics' web logo files from the user's stacked SVG (made with Gemini), as data.

Reads the source's parts (gradients, helix templates, cone and jets, droplets, placed helices) by text, recolours
them per site theme (logo brief: geyser-data/handovers/logo-brief-for-gemini.md), outlines the wordmark from Outfit
(SIL Open Font License 1.1, https://github.com/Outfitio/Outfit-Fonts) so no font is fetched, and writes standalone
SVGs with no <style>, no script and no external reference.

    python -I build_logo.py SOURCE.svg OUTFIT.ttf OUT_DIR
"""

import re
import sys

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

SRC, FONT, OUT = sys.argv[1:4]
src = open(SRC, encoding="utf-8").read()

defs = re.search(r"<defs>(.*?)</defs>", src, re.S).group(1)
defs = re.sub(r"<!--.*?-->", "", defs, flags=re.S)
cone = src[src.index("<!-- Volcanic") : src.index("<!-- Coloured Droplets")]
cone = re.sub(r"<!--.*?-->", "", cone, flags=re.S)
droplets = re.findall(r"<circle [^>]+/>", src)
helices = re.findall(
    r'<use href="#helix-(\w+)"\s+transform="translate\(([\d.]+), ([\d.]+)\) rotate\(([-\d.]+)\) scale\(([\d.]+)\)"/>',
    src,
)
assert len(droplets) == 21 and len(helices) == 22, (len(droplets), len(helices))

# --- theme recolouring (the brief's table) ------------------------------------------------------------------------
THEMES = {
    "light": {"map": {}, "geyser": "#174EA6", "genomics": "#174EA6"},
    "dark": {
        "map": {
            "#1A5BC4": "#8AB4F8",
            "#174EA6": "#669DF6",
            "#0D3373": "#4285F4",
            "#2B7DE9": "#D2E3FC",
            "#1A73E8": "#8AB4F8",
            "#669DF6": "#D2E3FC",
        },
        "geyser": "#8AB4F8",
        "genomics": "#E3E3E3",
    },
    "retro": {  # flat ink-on-paper: one blue, black lines, white highlights
        "map": {
            "#1A5BC4": "#0842A0",
            "#174EA6": "#0842A0",
            "#0D3373": "#000000",
            "#2B7DE9": "#FFFFFF",
            "#1A73E8": "#0842A0",
            "#669DF6": "#0842A0",
            "#D2E3FC": "#FFFFFF",
        },
        "geyser": "#000000",
        "genomics": "#000000",
    },
    "contrast": {  # white on black, no gradients in effect (every stop one colour), thick outline
        "map": {
            "#1A5BC4": "#000000",
            "#174EA6": "#000000",
            "#0D3373": "#FFFFFF",
            "#2B7DE9": "#FFFFFF",
            "#1A73E8": "#FFFFFF",
            "#669DF6": "#FFFFFF",
            "#D2E3FC": "#FFFFFF",
        },
        "geyser": "#FFFFFF",
        "genomics": "#FFFFFF",
        "cone_stroke": "9",
    },
}


def recolour(text, mapping):
    # Only the cone, jets and gradients; the helix templates keep Google's four colours in every theme.
    return re.sub(r"#[0-9A-Fa-f]{6}", lambda m: mapping.get(m.group(0).upper(), m.group(0)), text)


def theme_parts(name, suffix):
    t = THEMES[name]
    grads = re.findall(r"<(?:linear|radial)Gradient.*?</(?:linear|radial)Gradient>", defs, re.S)
    templates = re.findall(r'<g id="helix-\w+">.*?\n    </g>', defs, re.S)
    assert len(grads) == 3 and len(templates) == 4, (len(grads), len(templates))
    g = recolour("".join(grads), t["map"])
    c = recolour(cone, t["map"])
    if name == "contrast":  # the cone's outline thick and white; the opening ring too
        c = c.replace('stroke="#FFFFFF" stroke-width="3"', f'stroke="#FFFFFF" stroke-width="{t["cone_stroke"]}"', 1)
        c = c.replace('stroke-width="3.5"', 'stroke-width="7"')
    if name == "retro":
        c = c.replace('stroke="#000000" stroke-width="3"', 'stroke="#000000" stroke-width="6"', 1)
    if name == "contrast":  # no fine shading: drop the cone's translucent shading shapes (shadow, highlights)
        c = re.sub(r'<(?:path|ellipse)\b[^>]*\bopacity="0\.\d+"[^>]*/>', "", c)
    if name == "contrast":  # no gradients: every stop one colour (a black cone, white jets and vent)
        for ident, colour in (("coneGrad", "#000000"), ("jetGrad", "#FFFFFF"), ("ventGlow", "#FFFFFF")):
            g = re.sub(
                rf'(<(?:linear|radial)Gradient id="{ident}".*?</(?:linear|radial)Gradient>)',
                lambda m, col=colour: re.sub(r'stop-color="#[0-9A-Fa-f]{6}"', f'stop-color="{col}"', m.group(1)),
                g,
                flags=re.S,
            )
    # Unique ids per file variant (several may sit in one page as <img>, but keep them distinct anyway).
    body = g + "".join(templates)
    for ident in ("coneGrad", "ventGlow", "jetGrad", "helix-blue", "helix-red", "helix-yellow", "helix-green"):
        body = body.replace(f'id="{ident}"', f'id="{ident}-{suffix}"')
        c = c.replace(f"url(#{ident})", f"url(#{ident}-{suffix})")
    return body, c


def use(h, suffix):
    colour, x, y, r, s = h
    return f'<use href="#helix-{colour}-{suffix}" transform="translate({x}, {y}) rotate({r}) scale({s})"/>'


# --- the wordmark, outlined ---------------------------------------------------------------------------------------
font = instancer.instantiateVariableFont(TTFont(FONT), {"wght": 800})
glyphs, cmap, upem = font.getGlyphSet(), font.getBestCmap(), font["head"].unitsPerEm
hmtx = font["hmtx"]


def text_width(text, size, spacing):
    return sum(hmtx[cmap[ord(ch)]][0] * size / upem + spacing for ch in text) - spacing


def outline(text, x, y, size, spacing, fill, anchor="start"):
    """One <path> for the string, baseline at y, as the source's <text> (no kerning; letter-spacing as given)."""
    if anchor == "middle":
        x -= text_width(text, size, spacing) / 2
    k, d = size / upem, []
    for ch in text:
        name = cmap[ord(ch)]
        pen = SVGPathPen(glyphs)
        glyphs[name].draw(TransformPen(pen, (k, 0, 0, -k, x, y)))
        d.append(pen.getCommands())
        x += hmtx[name][0] * k + spacing
    return f'<path fill="{fill}" d="{" ".join(p for p in d if p)}"/>'


def svg(view, content, w, h, label="Geyser Genomics", extra=""):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" width="{w}" height="{h}" role="img" '
        f'aria-label="{label}" fill="none">{extra}<title>{label}</title>{content}</svg>\n'
    )


NOTICE = (
    "<!-- Geyser Genomics logo (from the user's stacked design). Wordmark outlined from Outfit ExtraBold, "
    "SIL Open Font License 1.1, Copyright 2021 The Outfit Project Authors. -->"
)


def stacked_inner(name, suffix):
    body, c = theme_parts(name, suffix)
    t = THEMES[name]
    words = outline("Geyser", 492, 842, 134, -1.5, t["geyser"], "middle") + outline(
        "Genomics", 492, 966, 134, -3.2, t["genomics"], "middle"
    )
    marks = f"<g>{''.join(droplets)}</g>" + "".join(use(h, suffix) for h in helices)
    return f"<defs>{body}</defs>", c + marks + words


def write(name, text):
    for bad in ("<style", "<script", "@import", 'href="http', "url(http", "style="):
        assert bad not in text, (name, bad)
    with open(f"{OUT}/{name}", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(name, len(text))


# The stacked logo per theme (1600 x 1600, as the source) and the 2:1 banner and social preview (light).
for name in THEMES:
    d, inner = stacked_inner(name, name)
    suffix = "" if name == "light" else f"-{name}"
    write(
        f"geyser-stacked{suffix}.svg",
        svg(
            "0 0 1600 1600", d + f'<g transform="translate(32, 10) scale(1.536)">{inner}</g>', 1600, 1600, extra=NOTICE
        ),
    )
d, inner = stacked_inner("light", "b")
write(
    "geyser-banner.svg",
    svg(
        "0 0 2560 1280",
        d
        + f'<g transform="translate(640, 0) scale(0.8)"><g transform="translate(32, 10) scale(1.536)">{inner}</g></g>',
        2560,
        1280,
        extra=NOTICE,
    ),
)
write(
    "geyser-social.svg",
    svg(
        "0 0 1280 640",
        d
        + '<rect width="1280" height="640" fill="#FAF8F3"/>'
        + f'<g transform="translate(330, 18) scale(0.385)"><g transform="translate(32, 10) scale(1.536)">{inner}</g></g>',
        1280,
        640,
        extra=NOTICE,
    ),
)

# The header lockup per theme: the mark with a compact burst (5 helices, nearby droplets) left, one-line wordmark right.
BURST = [
    h
    for h in helices
    if (h[1], h[2]) in {("496", "422"), ("432", "345"), ("568", "342"), ("334", "324"), ("514", "245")}
]
NEAR = [
    c
    for c in droplets
    if 300 <= float(re.search(r'cx="([\d.]+)"', c).group(1)) <= 680
    and float(re.search(r'cy="([\d.]+)"', c).group(1)) >= 280
]
assert len(BURST) == 5
for name in THEMES:
    body, c = theme_parts(name, name)
    t = THEMES[name]
    mark = c + f"<g>{''.join(NEAR)}</g>" + "".join(use(h, name) for h in BURST)
    # mark box in source units: x 250..740, y 195..720 -> 150 px tall at the left of a 720 x 160 lockup
    k = 150 / 525
    words = outline("Geyser", 176, 103, 62, -1.2, t["geyser"]) + outline(
        " Genomics", 176 + text_width("Geyser", 62, -1.2) - 1.2, 103, 62, -1.2, t["genomics"]
    )
    suffix = "" if name == "light" else f"-{name}"
    write(
        f"geyser-lockup{suffix}.svg",
        svg(
            "0 0 720 160",
            f"<defs>{body}</defs>"
            + f'<g transform="translate({5 - 250 * k:.2f}, {5 - 195 * k:.2f}) scale({k:.5f})">{mark}</g>'
            + words,
            720,
            160,
            extra=NOTICE,
        ),
    )

# The icon: the cone and three helices (blue, red, yellow), light theme (favicon and app icon), 512 x 512.
ICON = [
    ("blue", "500", "300", "4", "1.25"),
    ("red", "596", "372", "34", "1.05"),
    ("yellow", "392", "378", "-30", "1.05"),
]
body, c = theme_parts("light", "i")
k = 492 / 500
write(
    "geyser-icon.svg",
    svg(
        "0 0 512 512",
        f"<defs>{body}</defs>"
        + f'<g transform="translate({10 - 242 * k:.2f}, {10 - 225 * k:.2f}) scale({k:.5f})">'
        + c
        + "".join(use(h, "i") for h in ICON)
        + "</g>",
        512,
        512,
        extra=NOTICE,
    ),
)
