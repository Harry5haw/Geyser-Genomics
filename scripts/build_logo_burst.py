"""The header logo's eruption: per theme, an empty geyser and an animated one, made from the committed lockups.

From geyser-lockup{,-dark,-retro,-contrast}.svg (scripts/build_logo.py's output) it writes, per theme
(light, dark, retro, contrast), to web/samples/static/samples/:
- geyser-empty-<theme>.svg: the lockup without its water jets, droplets and helices (the cone and the wordmark);
- geyser-erupt-<theme>.svg: the whole lockup, animated with SMIL (<animateTransform>/<animate>, begin="0s",
  fill="freeze"): the jets surge up from the vent, the logo's own five helices fly up from the vent with a spin,
  overshoot, rebound and land, the droplets pop in. About 1.3 s, played once when the image loads, then it holds
  the full logo. No <style> element or style attribute (the hosted site's CSP would block those inside the SVG
  too); SMIL attributes and presentation attributes aren't style.

The header keeps the static lockup <img> (the LCP element; the no-JS and reduced-motion logo). geyser.js swaps its
src to the empty one once the page has loaded (first visit in a session, motion allowed), then to the animated
one on the first hover/focus (or after 0.7 s on a screen without hover). The two URLs ride on the <img>'s
data-erupt attribute: hashed static names that a script can't work out. Deterministic: run it again after
build_logo.py.

    python -I scripts/build_logo_burst.py
"""

import os
import re

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
STATIC = os.path.join(ROOT, "web", "samples", "static", "samples")
THEMES = {"light": "", "dark": "-dark", "retro": "-retro", "contrast": "-contrast"}
VENT = (492, 606)  # the jets' base at the vent, in the lockup's source units (the cone's opening)

JETS = re.compile(r'<g fill="url\(#jetGrad[^"]*\)">.*?</g>', re.S)
DROPS = re.compile(r"<g>\s*(?:<circle [^>]+/>\s*)+</g>")
USES = re.compile(r'<use href="#helix-[^"]+"[^>]*/>')

# Timing (seconds) and easing, as the CSS version the user approved: jets .45 s; helices .8 s each from .15 s,
# 0.08 s apart; droplets .5 s from .25 s. keySplines are the CSS cubic-bezier()s, one per keyframe interval.
JET_DUR, JET_EASE = 0.45, ".2 .9 .3 1"
POP_DUR, POP_START, POP_STAGGER, POP_EASE = 0.8, 0.15, 0.08, ".2 .8 .3 1"
POP_TIMES = "0;.6;.78;.9;1"
DROP_START, DROP_DUR, DROP_EASE = 0.25, 0.5, "0 0 .58 1"


def parts(svg):
    """The lockup's moving parts (jets, droplets, helix uses), and the lockup without them."""
    jets, drops, uses = JETS.findall(svg), DROPS.findall(svg), USES.findall(svg)
    assert len(jets) == 1 and len(drops) == 1 and len(uses) == 5, (len(jets), len(drops), len(uses))
    empty = USES.sub("", DROPS.sub("", JETS.sub("", svg)))
    # Drop the defs nothing uses any more (the jets' gradient, the helix templates).
    empty = re.sub(r'<linearGradient id="jetGrad[^"]*".*?</linearGradient>', "", empty, flags=re.S)
    empty = re.sub(r'<g id="helix-\w+-\w+">.*?\n    </g>', "", empty, flags=re.S)
    return jets[0], drops[0], uses, empty


def _s(t):
    return f"{t:.2f}".rstrip("0").rstrip(".") + "s"


def _anim(kind, values, dur, begin, ease, times=None, additive=False):
    """One SMIL animation of transform (kind = translate/rotate/scale) or of opacity (kind = None)."""
    n = len(values.split(";"))
    times = times or ";".join(f"{k / (n - 1):g}" for k in range(n))
    attrs = (
        f'values="{values}" keyTimes="{times}" calcMode="spline" keySplines="{";".join([ease] * (n - 1))}" '
        f'dur="{_s(dur)}" begin="{_s(begin)}" fill="freeze"'
    )
    if kind is None:
        return f'<animate attributeName="opacity" {attrs}/>'
    sum_ = ' additive="sum"' if additive else ""
    return f'<animateTransform attributeName="transform" type="{kind}" {attrs}{sum_}/>'


def offsets(svg):
    """Per helix: its place (x, y) and the move from there back to the vent (dx, dy), in source units."""
    out = []
    for u in parts(svg)[2]:
        x, y = map(float, re.search(r"translate\(([\d.]+), ([\d.]+)\)", u).groups())
        out.append(((x, y), (round(VENT[0] - x), round(VENT[1] - 8 - y))))
    return out


def erupting(svg):
    """The lockup with its jets, droplets and helices animated (SMIL only)."""
    jets, drops, uses, _ = parts(svg)
    vx, vy = VENT
    # Jets: scale up from the vent (scaleY 0 -> 1 about the vent's base), held at 0 until then.
    new_jets = (
        f'<g transform="translate({vx} {vy})"><g transform="scale(1 0)">'
        + _anim("scale", "1 0;1 1", JET_DUR, 0, JET_EASE)
        + f'<g transform="translate({-vx} {-vy})">{jets}</g></g></g>'
    )
    # Droplets: fade and grow in about their middle.
    cs = [tuple(map(float, c)) for c in re.findall(r'cx="([\d.]+)" cy="([\d.]+)"', drops)]
    mx, my = round(sum(c[0] for c in cs) / len(cs)), round(sum(c[1] for c in cs) / len(cs))
    new_drops = (
        f'<g transform="translate({mx} {my})"><g opacity="0" transform="scale(.3)">'
        + _anim(None, "0;1", DROP_DUR, DROP_START, DROP_EASE)
        + _anim("scale", ".3;1", DROP_DUR, DROP_START, DROP_EASE)
        + f'<g transform="translate({-mx} {-my})">{drops}</g></g></g>'
    )
    # Helices: each from the vent to its place, spinning, overshooting (up 40, down 12, up 4), then still.
    new_uses = []
    for k, (u, ((x, y), (dx, dy))) in enumerate(zip(uses, offsets(svg), strict=True)):
        rest = re.sub(r"translate\([\d.]+, [\d.]+\)\s*", "", re.search(r'transform="([^"]+)"', u).group(1))
        inner = re.sub(r'transform="[^"]+"', f'transform="{rest}"', u)
        begin = POP_START + k * POP_STAGGER
        new_uses.append(
            f'<g transform="translate({x:g} {y:g})"><g opacity="0" transform="translate({dx} {dy})">'
            + _anim(None, "0;1;1", POP_DUR, begin, POP_EASE, "0;.2;1")
            + _anim("translate", f"{dx} {dy};0 -40;0 12;0 -4;0 0", POP_DUR, begin, POP_EASE, POP_TIMES)
            + _anim("rotate", "-30;6;0;0;0", POP_DUR, begin, POP_EASE, POP_TIMES, additive=True)
            + _anim("scale", ".3;1.06;.98;1;1", POP_DUR, begin, POP_EASE, POP_TIMES, additive=True)
            + f"{inner}</g></g>"
        )
    out = svg.replace(jets, new_jets, 1).replace(drops, new_drops, 1)
    for u, n in zip(uses, new_uses, strict=True):
        out = out.replace(u, n, 1)
    return out


def main():
    for theme, suffix in THEMES.items():
        with open(os.path.join(STATIC, f"geyser-lockup{suffix}.svg"), encoding="utf-8") as fh:
            svg = fh.read()
        for name, body in (("empty", parts(svg)[3]), ("erupt", erupting(svg))):
            with open(os.path.join(STATIC, f"geyser-{name}-{theme}.svg"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(body)
    print("helix moves", [d for _, d in offsets(svg)])


if __name__ == "__main__":
    main()
