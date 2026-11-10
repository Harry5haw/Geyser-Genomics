"""Split the header lockups for the logo eruption: an empty geyser per theme, the parts that erupt, and the markup.

From the four committed lockups (scripts/build_logo.py's output) it writes:
- web/samples/static/samples/geyser-lockup-empty{,-dark,-retro,-contrast}.svg: the lockup without its water jets,
  droplets and helices (the cone and the wordmark), shown by the header's <img>s;
- web/samples/static/samples/geyser-burst.svg: those parts, all in <defs> (it draws nothing on its own): the jets per
  theme (their gradient is the theme's), the droplets, and one helix shape (the rungs and the two strands; each use
  sets its colour). A static file, so it doesn't count against the pages' HTML+CSS+JS budget;
- web/samples/templates/samples/_logo_burst.html: the inline, decorative SVG over the <img>: <use>s of those parts in
  the lockup's coordinates, which the page's CSS animates. Together with the <img> it is the full lockup.
The helices and droplets keep Google's four colours in every theme; the jets are one <use> per theme (CSS shows one).
The lockup's helix shadow and highlight strokes are left out: at the header's 40 px a helix is ~6 px tall.

The eruption itself is CSS (_site.css) armed by _theme_boot.js. Deterministic: run it again after build_logo.py.

    python -I scripts/build_logo_burst.py
"""

import os
import re

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
STATIC = os.path.join(ROOT, "web", "samples", "static", "samples")
PARTIAL = os.path.join(ROOT, "web", "samples", "templates", "samples", "_logo_burst.html")
ART = os.path.join(STATIC, "geyser-burst.svg")
THEMES = {"light": "", "dark": "-dark", "retro": "-retro", "contrast": "-contrast"}
VENT = (492, 598)  # the vent's centre in the source's units (the cone's opening ellipse)

JETS = re.compile(r'<g fill="url\(#jetGrad[^"]*\)">.*?</g>', re.S)
DROPS = re.compile(r"<g>\s*(?:<circle [^>]+/>\s*)+</g>")
USES = re.compile(r'<use href="#helix-[^"]+"[^>]*/>')


def parts(svg):
    """The lockup's moving parts, and the lockup without them."""
    jets, drops, uses = JETS.findall(svg), DROPS.findall(svg), USES.findall(svg)
    assert len(jets) == 1 and len(drops) == 1 and len(uses) == 5, (len(jets), len(drops), len(uses))
    empty = USES.sub("", DROPS.sub("", JETS.sub("", svg)))
    # Drop the defs nothing uses any more (the jets' gradient, the helix templates).
    empty = re.sub(r'<linearGradient id="jetGrad[^"]*".*?</linearGradient>', "", empty, flags=re.S)
    empty = re.sub(r'<g id="helix-\w+-\w+">.*?\n    </g>', "", empty, flags=re.S)
    return jets[0], drops[0], uses, empty


def jet_gradient(svg):
    return re.search(r'<linearGradient id="jetGrad[^"]*".*?</linearGradient>', svg, re.S).group(0)


def jet_stops(svg):
    return re.findall(r'stop-color="(#[0-9A-Fa-f]{6})"', jet_gradient(svg))


def _templates(light):
    templates = re.findall(r'<g id="helix-\w+-light">.*?\n    </g>', light, re.S)
    assert len(templates) == 4
    return templates


def colours(light):
    """Each helix colour's main stroke (the template's main layer)."""
    out = {}
    for t in _templates(light):
        name = re.search(r'id="helix-(\w+)-light"', t).group(1)
        out[name] = re.search(r'<g stroke="(#[0-9A-Fa-f]{6})" stroke-linecap="round" fill="none">', t).group(1)
    return out


def art(lockups):
    """geyser-burst.svg: every erupting part in <defs>, by id (jets-<theme>, drops, hx)."""
    light = lockups["light"]
    blue = next(t for t in _templates(light) if 'id="helix-blue-light"' in t)
    layer = re.search(r'<g stroke="#[0-9A-Fa-f]{6}" stroke-linecap="round" fill="none">(.*?)</g>', blue, re.S).group(1)
    strokes = [p for p in re.findall(r"<path [^>]*/>", layer) if " stroke=" not in p and "opacity" not in p]
    assert len(strokes) == 3, strokes  # the rungs and the two strands
    defs = ['<g id="hx" stroke-linecap="round" fill="none">' + "".join(strokes) + "</g>"]
    for theme, svg in lockups.items():
        jets = parts(svg)[0]
        grad = re.sub(r'id="jetGrad[^"]*"', f'id="jet-{theme}"', jet_gradient(svg))
        defs.append(
            grad + re.sub(r'<g fill="url\(#jetGrad[^"]*\)">', f'<g id="jets-{theme}" fill="url(#jet-{theme})">', jets)
        )
    drops = parts(light)[1].replace("<g>", '<g id="drops">', 1)
    defs.append(drops)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 160">'
        "<!-- Geyser Genomics logo parts for the header eruption (scripts/build_logo_burst.py). Defs only. -->"
        f"<defs>{''.join(defs)}</defs></svg>\n"
    )
    return re.sub(r">\s+<", "><", svg)


def burst(light):
    """The inline SVG: <use>s of geyser-burst.svg's parts, placed as in the lockup ({{ art }} is its static URL)."""
    _, _, uses, _ = parts(light)
    group = re.search(r'<g transform="(translate\([^)]*\) scale\([^)]*\))">', light).group(1)
    stroke = colours(light)
    jets = "".join(f'<use class="burst-jets jt-{t}" href="{{{{ art }}}}#jets-{t}"/>' for t in THEMES)
    helices = []
    for k, u in enumerate(uses):
        name = re.search(r'href="#helix-(\w+)-light"', u).group(1)
        place = re.search(r'transform="([^"]+)"', u).group(1)
        # Each helix in its own group: the CSS moves the group from the vent (--x/--y: the vent minus its place).
        helices.append(
            f'<g class="burst-h h{k}"><use href="{{{{ art }}}}#hx" stroke="{stroke[name]}" transform="{place}"/></g>'
        )
    return (
        '<svg class="burst" viewBox="0 0 720 160" aria-hidden="true" focusable="false">'
        + f'<g transform="{group}">{jets}<use class="burst-d" href="{{{{ art }}}}#drops"/>{"".join(helices)}</g></svg>'
    )


def offsets(light):
    """--x/--y per helix (CSS): from its place back to the vent, in the source's units."""
    _, _, uses, _ = parts(light)
    out = []
    for u in uses:
        x, y = map(float, re.search(r"translate\(([\d.]+), ([\d.]+)\)", u).groups())
        out.append((round(VENT[0] - x), round(VENT[1] - y)))
    return out


def main():
    lockups = {}
    for theme, suffix in THEMES.items():
        with open(os.path.join(STATIC, f"geyser-lockup{suffix}.svg"), encoding="utf-8") as fh:
            lockups[theme] = fh.read()
        with open(os.path.join(STATIC, f"geyser-lockup-empty{suffix}.svg"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(parts(lockups[theme])[3])
    with open(ART, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(art(lockups))
    with open(PARTIAL, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            "{# Generated by scripts/build_logo_burst.py from the lockups: the parts that erupt (decorative). #}\n"
            "{% load static %}{% static 'samples/geyser-burst.svg' as art %}" + burst(lockups["light"]) + "\n"
        )
    print("helix offsets", offsets(lockups["light"]))


if __name__ == "__main__":
    main()
