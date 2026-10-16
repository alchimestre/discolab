"""A bubble map of the taste graph, with suggestions scoped to a cluster.

Answers one question: where is my taste dense, and which areas touch each other.
Bubbles are communities, sized by how many artists they hold and placed so that
communities sharing artists settle near one another. Clicking one scopes the
suggestions beside it.

Rendered as one file with no network use beyond the YouTube embeds the panel
optionally carries, so the page works from the local server the same way.
"""

from __future__ import annotations

import html
import json
import math
import random
from dataclasses import dataclass, field

WIDTH = 1000.0
HEIGHT = 1000.0
PADDING = 16.0
ITERATIONS = 500
SEED = 7
PANEL_SUGGESTIONS = 14

# Deezer answers in French with sixty-odd labels, several of which mean one thing
# to a listener: Electro, Dance, Techno/House and Trance are one family. Naming a
# cluster after its top raw label spends sixty names on six ideas, so the labels
# are folded into families first, and the family is what names and colours the
# bubble. Raw labels stay in the panel, where there is room for them.
FAMILY_EXACT = {
    "electro pop/electro rock": "Electronic",
    "pop indé/folk": "Pop",
    "rock indé/pop rock": "Rock",
    "rock indé": "Rock",
    "electro hip-hop": "Hip hop & Soul",
    "variété internationale": "Pop",
    "pop internationale": "Pop",
    "chanson française": "Pop",
    "rap français": "Hip hop & Soul",
    "singer & songwriter": "Folk & World",
    "soul & funk": "Hip hop & Soul",
    "soul contemporaine": "Hip hop & Soul",
    "musique africaine": "Folk & World",
    "musique arabe": "Folk & World",
    "musique indienne": "Folk & World",
    "musique brésilienne": "Folk & World",
    "musiques de jeux vidéo": "Soundtrack",
    "musiques de films": "Soundtrack",
    "films/jeux vidéo": "Soundtrack",
    "bandes originales": "Soundtrack",
    "bo tv": "Soundtrack",
    "période classique": "Classical",
    "chicago blues": "Jazz & Blues",
    "blues électrique": "Jazz & Blues",
    "rock & roll/rockabilly": "Rock",
    "rock français": "Rock",
    "hard rock": "Rock",
    "dancehall/ragga": "Hip hop & Soul",
    "comptines/chansons": "Pop",
    "jeunesse": "Pop",
    "livres audio": "Soundtrack",
    "comédie": "Soundtrack",
    "médiéval": "Classical",
    "renaissance": "Classical",
    "baroque": "Classical",
    "opéra": "Classical",
}

FAMILY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Hip hop & Soul", ("rap", "hip hop", "r&b", "rnb", "soul", "funk", "grime",
                        "dancehall", "ragga")),
    ("Classical", ("classique", "baroque", "opéra", "renaissance", "médiéval")),
    ("Soundtrack", ("films", "bandes originales", "jeux vidéo", "comédie",
                    "livres audio")),
    ("Jazz & Blues", ("jazz", "blues")),
    ("Pop", ("pop", "variété", "chanson", "jeunesse", "comptines")),
    ("Electronic", ("electro", "techno", "house", "dance", "trance", "disco",
                    "dubstep", "electro", "club", "edm", "dub")),
    ("Rock", ("rock", "metal", "alternative", "indie", "indé", "punk")),
    ("Folk & World", ("folk", "country", "singer", "latino", "africaine", "arabe",
                      "indienne", "brésilienne", "bollywood", "reggae", "world",
                      "maghreb", "mashriq", "anatolia", "levant", "occitan",
                      "sub-saharan", "sahel", "andes", "balkans", "cajun",
                      "lament", "lullaby", "balearic", "tuareg")),
)

FAMILY_COLOUR = {
    "Electronic": "#5b9bff",
    "Hip hop & Soul": "#ff9440",
    "Rock": "#ff6b5e",
    "Pop": "#ff6fc4",
    "Jazz & Blues": "#f0b429",
    "Classical": "#b18cff",
    "Folk & World": "#3fc47c",
    "Soundtrack": "#3fc0d8",
    "Other": "#9aa0aa",
}


def family_of(genre: str) -> str:
    """Fold one raw label into a family."""
    key = genre.casefold().strip()
    if key in FAMILY_EXACT:
        return FAMILY_EXACT[key]
    for family, needles in FAMILY_RULES:
        if any(needle in key for needle in needles):
            return family
    return "Other"


@dataclass
class Cluster:
    key: int
    size: int
    corpus: int
    tagged: int = 0
    genres: tuple[tuple[str, int], ...] = ()
    tags: tuple[tuple[str, int], ...] = ()
    labels: tuple[tuple[str, int], ...] = ()
    artists: tuple[str, ...] = ()
    x: float = 0.0
    y: float = 0.0
    r: float = 0.0
    tag: str = ""
    name: str = ""
    named_from: str = ""
    candidates: list[str] = field(default_factory=list)
    options: list[tuple[str, str]] = field(default_factory=list)
    suggestions: list[dict] = field(default_factory=list)
    inner: list[dict] = field(default_factory=list)

    @property
    def families(self) -> tuple[tuple[str, int], ...]:
        """Genre families by weight, heaviest first."""
        totals: dict[str, int] = {}
        for name, count in self.genres:
            family = family_of(name)
            totals[family] = totals.get(family, 0) + count
        return tuple(sorted(totals.items(), key=lambda item: (-item[1], item[0])))

    @property
    def family(self) -> str:
        found = self.families
        return found[0][0] if found else "Other"

    @property
    def colour(self) -> str:
        """Tint from the genre the cluster is named for, not its heaviest one.

        Colouring by the heaviest genre while naming by the most distinctive one
        painted Rap/Hip Hop blue, because Electro was its largest single genre.
        The bubble then disagreed with its own label.
        """
        return FAMILY_COLOUR.get(
            family_of(self.named_from or self.family), FAMILY_COLOUR["Other"]
        )

    @property
    def label(self) -> str:
        """The assigned name, or the least it can honestly say."""
        if self.name:
            return self.name
        if self.tagged < 8:
            return f"cluster {self.key}"
        return self.family

    @property
    def pool(self) -> tuple[tuple[str, int], ...]:
        """Every label the cluster carries, its own tags included."""
        return tuple(self.genres) + tuple(self.tags)

    @property
    def sub_genre(self) -> str | None:
        """The owner's own label, when the tags agree on one for this cluster."""
        return self.tags[0][0] if self.tags else None


def radius_for(size: int, largest: int) -> float:
    """Square root, so area carries the count rather than the radius."""
    return 9.0 + 52.0 * math.sqrt(size / largest)


def layout(clusters: list[Cluster], edges: list[tuple[int, int, int]]) -> None:
    """Place the bubbles in place, in place.

    Communities that share artists pull together; every pair pushes apart, with a
    hard push once two bubbles would overlap. Positions are seeded, so the same
    graph always draws the same way and a cluster stays where it was last time.
    """
    rng = random.Random(SEED)
    for cluster in clusters:
        angle = rng.random() * math.tau
        reach = math.sqrt(rng.random()) * WIDTH * 0.36
        cluster.x = WIDTH / 2 + math.cos(angle) * reach
        cluster.y = HEIGHT / 2 + math.sin(angle) * reach

    at = {cluster.key: index for index, cluster in enumerate(clusters)}
    links = [
        (at[a], at[b], weight)
        for a, b, weight in edges
        if a in at and b in at and a != b
    ]
    heaviest = max((weight for _, _, weight in links), default=1)
    count = len(clusters)

    for step in range(ITERATIONS):
        cooling = 1.0 - step / ITERATIONS
        push_x = [0.0] * count
        push_y = [0.0] * count

        for i in range(count):
            a = clusters[i]
            for j in range(i + 1, count):
                b = clusters[j]
                vx, vy = a.x - b.x, a.y - b.y
                distance = math.hypot(vx, vy) or 0.01
                overlap = distance - (a.r + b.r + PADDING)
                if overlap < 0:
                    # Negative overlap is a gap shortfall, so the force has to be
                    # negated to push apart. Left as-is it pulled them together.
                    force = -overlap * 0.55
                else:
                    force = (a.r * b.r) / (distance * distance) * 26.0
                ux, uy = vx / distance, vy / distance
                push_x[i] += ux * force
                push_y[i] += uy * force
                push_x[j] -= ux * force
                push_y[j] -= uy * force

        for i, j, weight in links:
            a, b = clusters[i], clusters[j]
            vx, vy = b.x - a.x, b.y - a.y
            distance = math.hypot(vx, vy) or 0.01
            desired = a.r + b.r + PADDING * 2.6
            pull = (distance - desired) * 0.012 * (0.35 + weight / heaviest)
            ux, uy = vx / distance, vy / distance
            push_x[i] += ux * pull
            push_y[i] += uy * pull
            push_x[j] -= ux * pull
            push_y[j] -= uy * pull

        for i, cluster in enumerate(clusters):
            cluster.x += max(-34.0, min(34.0, push_x[i])) * cooling
            cluster.y += max(-34.0, min(34.0, push_y[i])) * cooling
            cluster.x = min(WIDTH - cluster.r, max(cluster.r, cluster.x))
            cluster.y = min(HEIGHT - cluster.r, max(cluster.r, cluster.y))

    # A last pass that only separates: the pull along links can still leave two
    # bubbles sitting on top of each other, and the labels then collide too.
    for _ in range(160):
        push_x = [0.0] * count
        push_y = [0.0] * count
        for i in range(count):
            a = clusters[i]
            for j in range(i + 1, count):
                b = clusters[j]
                vx, vy = a.x - b.x, a.y - b.y
                distance = math.hypot(vx, vy) or 0.01
                overlap = distance - (a.r + b.r + PADDING)
                if overlap >= 0:
                    continue
                force = -overlap * 0.5
                ux, uy = vx / distance, vy / distance
                push_x[i] += ux * force
                push_y[i] += uy * force
                push_x[j] -= ux * force
                push_y[j] -= uy * force
        moved = 0.0
        for i, cluster in enumerate(clusters):
            step_x = max(-16.0, min(16.0, push_x[i]))
            step_y = max(-16.0, min(16.0, push_y[i]))
            cluster.x = min(WIDTH - cluster.r, max(cluster.r, cluster.x + step_x))
            cluster.y = min(HEIGHT - cluster.r, max(cluster.r, cluster.y + step_y))
            moved = max(moved, abs(step_x) + abs(step_y))
        if moved < 0.05:
            break


def assign_names(clusters: list[Cluster]) -> None:
    """Give every cluster a name of its own, that also describes it.

    Naming by the heaviest genre made six bubbles say Electronic, which reads as
    one area rather than six. Naming by the most distinctive one gave a
    2,325-artist cluster the name Blues, off thirteen of its artists, and called
    an electronic cluster Musique arabe. A name has to be both: present enough to
    describe the cluster, and over-represented enough to tell it from the others.

    So a label must hold at least eight per cent of the cluster before it can
    name it, and the score multiplies that share by how much the cluster leans on
    it. Collisions fall through to the runner-up, then to the pair, until every
    name is unique.
    """
    counts: dict[str, int] = {}
    for cluster in clusters:
        for name, count in cluster.pool:
            counts[name] = counts.get(name, 0) + count
    overall = sum(counts.values()) or 1

    for cluster in clusters:
        here = sum(count for _, count in cluster.pool) or 1
        scored = []
        for name, count in cluster.pool:
            share = count / here
            if count < 3 or share < 0.08:
                continue
            # Share times distinctiveness: size keeps it descriptive, the ratio
            # keeps it distinguishing.
            scored.append((share * (share / (counts[name] / overall)), name))
        scored.sort(key=lambda item: (-item[0], item[1]))
        singles = [name for _, name in scored]
        # Each candidate keeps the genre it was drawn from, so the bubble can be
        # tinted for the name it ended up with.
        pairs: list[tuple[str, str]] = []
        if len(singles) > 1:
            pairs.append((f"{singles[0]} & {singles[1]}", singles[0]))
        options: list[tuple[str, str]] = [(name, name) for name in singles]
        options[1:1] = pairs
        options += [(name, name) for name, _ in cluster.pool if name not in singles]
        options.append((cluster.family, cluster.family))
        cluster.options = options

    taken: set[str] = set()
    # Largest first, so the biggest clusters get the best of contested names.
    for cluster in sorted(clusters, key=lambda c: (-c.size, c.key)):
        chosen = ""
        source = ""
        for candidate, from_genre in cluster.options:
            if candidate and candidate not in taken:
                chosen, source = candidate, from_genre
                break
        if not chosen:
            chosen, source = f"cluster {cluster.key}", ""
        cluster.name = chosen
        cluster.named_from = source
        taken.add(chosen)


def place_inner(cluster: Cluster, groups: list[list[tuple[str, str]]]) -> None:
    """Lay a cluster's sub-groups out inside its own circle.

    Positions are a ring, sized by membership, so the sub-groups read as parts of
    the bubble rather than as a second map. Revealed on zoom, because at rest the
    cluster is the unit that matters.
    """
    if not groups:
        return
    biggest = max(len(group) for group in groups)
    ring = cluster.r * 0.42
    placed = []
    for index, group in enumerate(groups):
        angle = (index / len(groups)) * math.tau - math.pi / 2
        radius = max(4.0, cluster.r * 0.34 * math.sqrt(len(group) / biggest))
        # The name is the group's most connected member, which is the one that
        # says most about what the sub-group is.
        placed.append(
            {
                "name": group[0][1],
                "size": len(group),
                "x": cluster.x + math.cos(angle) * ring,
                "y": cluster.y + math.sin(angle) * ring,
                "r": radius,
                "colour": cluster.colour,
            }
        )
    cluster.inner = placed


def _short(label: str, limit: int = 26) -> str:
    """Trim to a word boundary, so a bubble never reads like a typo."""
    if len(label) <= limit:
        return label
    cut = label[:limit]
    space = cut.rfind(" ")
    if space > 8:
        cut = cut[:space]
    return cut.rstrip(" /&") + "…"


def _cluster_svg(clusters: list[Cluster], edges: list[tuple[int, int, int]]) -> str:
    at = {cluster.key: cluster for cluster in clusters}
    heaviest = max((weight for _, _, weight in edges), default=1)

    # Only the edges that carry real weight. Seventy-one nodes with every pair
    # drawn is a hairball, and the strong links stop reading as strong.
    floor = max(2.0, heaviest * 0.06)
    lines = []
    for a, b, weight in edges:
        one, two = at.get(a), at.get(b)
        if one is None or two is None or one is two or weight < floor:
            continue
        alpha = 0.10 + 0.40 * (weight / heaviest)
        lines.append(
            f'<line class="edge" data-a="{a}" data-b="{b}" '
            f'x1="{one.x:.1f}" y1="{one.y:.1f}" x2="{two.x:.1f}" y2="{two.y:.1f}" '
            f'stroke-width="{0.8 + 4.0 * (weight / heaviest):.2f}" '
            f'stroke-opacity="{alpha:.3f}"/>'
        )

    # A label on every bubble collides and none of them read. The largest get
    # text, and only where the bubble is big enough to hold it; the rest are
    # identified by hovering, which every bubble answers to.
    ranked = sorted(clusters, key=lambda c: (-c.size, c.key))
    labelled = {c.key for c in ranked[:22]}

    bubbles = []
    for cluster in clusters:
        label = cluster.label
        title = f"{label} · {cluster.size:,} artists"
        show = cluster.key in labelled and cluster.r >= 15.0
        text = (
            f'<text x="{cluster.x:.1f}" y="{cluster.y:.1f}" text-anchor="middle" '
            f'dominant-baseline="central" '
            f'font-size="{max(11.0, min(18.0, cluster.r * 0.40)):.0f}">'
            f"{html.escape(_short(label))}</text>"
            if show
            else ""
        )
        bubbles.append(
            f'<g class="bubble" data-key="{cluster.key}" '
            f'data-family="{html.escape(cluster.family)}" tabindex="0" role="button" '
            f'aria-label="{html.escape(title)}">'
            f"<title>{html.escape(title)}</title>"
            f'<circle cx="{cluster.x:.1f}" cy="{cluster.y:.1f}" r="{cluster.r:.1f}" '
            f'fill="{cluster.colour}" fill-opacity="0.30" stroke="{cluster.colour}"/>'
            f"{text}</g>"
        )

    # Sub-groups, drawn but hidden until the map is zoomed far enough to read them.
    inner = []
    for cluster in clusters:
        for part in cluster.inner:
            inner.append(
                f'<g class="inner-bubble">'
                f"<title>{html.escape(part['name'])} · {part['size']} artists</title>"
                f'<circle cx="{part["x"]:.1f}" cy="{part["y"]:.1f}" r="{part["r"]:.1f}" '
                f'fill="{part["colour"]}" fill-opacity="0.55" stroke="{part["colour"]}"/>'
                f'<text x="{part["x"]:.1f}" y="{part["y"]:.1f}" text-anchor="middle" '
                f'dominant-baseline="central">'
                f"{html.escape(part['name'][:16])}</text></g>"
            )

    # Fit the viewBox to the bubbles rather than to a square, so the map uses the
    # whole pane instead of sitting in it with margins.
    left = min(c.x - c.r for c in clusters)
    right = max(c.x + c.r for c in clusters)
    top = min(c.y - c.r for c in clusters)
    bottom = max(c.y + c.r for c in clusters)
    pad = 12.0
    return (
        f'<svg id="map" viewBox="{left - pad:.1f} {top - pad:.1f} '
        f'{right - left + pad * 2:.1f} {bottom - top + pad * 2:.1f}" '
        f'preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">'
        f'<g id="edges">{"".join(lines)}</g>'
        f'<g id="bubbles">{"".join(bubbles)}</g>'
        f'<g id="inner" style="display:none">{"".join(inner)}</g>'
        f"</svg>"
    )


def _payload(clusters: list[Cluster], candidates: list[dict]) -> str:
    return json.dumps(
        {
            "clusters": [
                {
                    "key": cluster.key,
                    "size": cluster.size,
                    "corpus": cluster.corpus,
                    "tagged": cluster.tagged,
                    "label": cluster.label,
                    "named_from": cluster.named_from,
                    "family": cluster.family,
                    "colour": cluster.colour,
                    "sub_genre": cluster.sub_genre,
                    "genres": [{"name": name, "n": n} for name, n in cluster.genres],
                    "labels": [{"name": name, "n": n} for name, n in cluster.labels],
                    "artists": list(cluster.artists),
                }
                for cluster in clusters
            ],
            "candidates": candidates,
        },
        ensure_ascii=False,
    )


PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="icon" href="data:,">
<style>
  :root { color-scheme: light dark;
    --ink: CanvasText; --quiet: color-mix(in srgb, CanvasText 58%, transparent);
    --line: color-mix(in srgb, CanvasText 14%, transparent);
    --fill: color-mix(in srgb, CanvasText 10%, transparent);
    --hot: color-mix(in srgb, CanvasText 78%, transparent); }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body { font: 14px/1.5 system-ui, sans-serif; display: grid;
         grid-template-columns: 2fr minmax(320px, 1fr); overflow: hidden; }
  #stage { position: relative; min-width: 0; display: flex; flex-direction: column; }
  #toolbar { display: flex; gap: 16px; align-items: baseline; flex-wrap: wrap;
             padding: 12px 18px; border-bottom: 1px solid var(--line); }
  #toolbar h1 { font-size: 15px; margin: 0; font-weight: 600; }
  #toolbar .stat { font-size: 12px; color: var(--quiet); }
  #graph { flex: 1; min-height: 0; padding: 8px; }
  svg#map { width: 100%; height: 100%; display: block; }
  .edge { stroke: currentColor; color: var(--ink); }
  .bubble circle { stroke-width: 1.6; cursor: pointer; transition: fill-opacity .12s; }
  .bubble text { fill: var(--ink); pointer-events: none; opacity: .95;
                 paint-order: stroke; stroke: Canvas; stroke-width: 2.5px; }
  .bubble:hover circle, .bubble:focus-visible circle { stroke-width: 3; fill-opacity: .58; }
  .bubble:hover text, .bubble:focus-visible text { opacity: 1; }
  .bubble.active circle { stroke-width: 3.6; fill-opacity: .72; }
  .bubble.dimmed { opacity: .45; }
  .bubble.linked circle { stroke-width: 2.8; stroke-dasharray: 7 5; fill-opacity: .42; }
  .bubble.linked text { opacity: 1; }
  .bubble.open.linked circle { fill-opacity: .18; }
  .bubble.open circle { fill-opacity: .12; }
  .bubble.open text { display: none; }
  .inner-bubble circle { stroke-width: 1.2; pointer-events: none; }
  .inner-bubble text { fill: var(--ink); opacity: .95; pointer-events: none;
                       font-size: var(--inner-label, 10px);
                       paint-order: stroke; stroke: Canvas; stroke-width: 2px; }
  .bubble:focus { outline: none; }
  #hint { position: absolute; left: 18px; bottom: 14px; font-size: 12px;
          color: var(--quiet); pointer-events: none; }
  aside { border-left: 1px solid var(--line); overflow-y: auto; padding: 16px 18px 40px; }
  #legend { display: flex; flex-wrap: wrap; gap: 4px 10px; margin: 0 0 14px;
            font-size: 11px; color: var(--quiet); }
  #legend span { display: inline-flex; align-items: center; gap: 4px; }
  #legend i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  #scope h2 { font-size: 17px; margin: 0 0 2px; }
  #scope .meta { font-size: 12px; color: var(--quiet); margin-bottom: 10px; }
  #scope .genres { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 14px; }
  #scope .genres span { font-size: 11px; padding: 1px 7px; border-radius: 9px;
                        border: 1px solid var(--line); color: var(--quiet); }
  #scope .own-tag { font-size: 12px; margin: 0 0 12px; }
  #scope .in-cluster { font-size: 12px; color: var(--quiet); margin-bottom: 14px; }
  #scope .picked { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 12px; }
  #scope .picked button { font: inherit; font-size: 11px; padding: 1px 8px; cursor: pointer;
                          border-radius: 9px; border: 1px solid var(--line);
                          background: transparent; color: var(--ink); }
  p.aside { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
            color: var(--quiet); margin: 14px 0 6px; }
  #scope .partners { display: flex; flex-wrap: wrap; gap: 5px; margin: 0 0 6px; }
  #scope .partners button { font: inherit; font-size: 11px; padding: 2px 8px; cursor: pointer;
                            border-radius: 9px; border: 1px dashed var(--line);
                            background: transparent; color: var(--ink); }
  #scope .partners button:hover { border-style: solid; }
  #scope .partners b { color: var(--quiet); font-weight: 500; }
  ol.suggestions { list-style: none; margin: 0; padding: 0; }
  ol.suggestions .from { color: var(--quiet); }
  .card { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px;
          margin: 0 0 14px; background: color-mix(in srgb, CanvasText 4%, transparent); }
  .card-title { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
                color: var(--quiet); margin: 0 0 7px; }
  .labels { display: flex; flex-wrap: wrap; gap: 5px 8px; font-size: 12px; }
  .labels span { color: var(--ink); }
  .labels b { color: var(--quiet); font-weight: 500; }
  ol.suggestions li { padding: 9px 0; border-top: 1px solid var(--line); }
  ol.suggestions .name { font-weight: 600; }
  ol.suggestions .why { font-size: 12px; color: var(--quiet); }
  ol.suggestions .tags { font-size: 11px; color: var(--quiet); }
  .empty { font-size: 13px; color: var(--quiet); }
  @media (max-width: 900px) {
    body { grid-template-columns: 1fr; grid-template-rows: 62vh 1fr; overflow: auto; }
    aside { border-left: 0; border-top: 1px solid var(--line); }
  }
</style>

<div id="stage">
  <div id="toolbar">
    <h1>__TITLE__</h1>
    <span class="stat" id="stat"></span>
    <span class="stat" id="tip"></span>
  </div>
  <div id="graph">__SVG__</div>
  <p id="hint">Hover to inspect. Click a bubble to scope the suggestions.</p>
</div>

<aside>
  <div id="legend"></div>
  <div id="scope">
    <h2>Pick a cluster</h2>
    <p class="meta">Bubbles are groups of artists that occur together. Size is how many artists the group holds. Click several to combine them.</p>
    <p class="empty">Nothing selected yet.</p>
  </div>
</aside>

<script>
const DATA = __PAYLOAD__;
const byKey = new Map(DATA.clusters.map(c => [c.key, c]));
const labelOf = new Map(DATA.candidates.map(c => [c.name, c]));

// Which clusters share candidates, and how many. Five hundred and seventy-four of
// the eight hundred and sixty-one pairs share nothing, so the map says up front
// which pair would answer instead of leaving that to be discovered by guessing.
const neighbours = new Map();
for (const candidate of DATA.candidates) {
  const keys = Object.keys(candidate.clusters).map(Number).filter(k => byKey.has(k));
  for (let i = 0; i < keys.length; i++) {
    for (let j = i + 1; j < keys.length; j++) {
      for (const [self, other] of [[keys[i], keys[j]], [keys[j], keys[i]]]) {
        if (!neighbours.has(self)) neighbours.set(self, new Map());
        const row = neighbours.get(self);
        row.set(other, (row.get(other) || 0) + 1);
      }
    }
  }
}
const bubbles = [...document.querySelectorAll('.bubble')];
const edges = [...document.querySelectorAll('.edge')];
const scope = document.getElementById('scope');
const tip = document.getElementById('tip');
const picked = new Set();

document.getElementById('stat').textContent =
  DATA.clusters.length + ' clusters · ' +
  DATA.clusters.reduce((n, c) => n + c.size, 0).toLocaleString() + ' artists';

function esc(s) { return String(s).replace(/[&<>"]/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

function colourOf(family) {
  const hit = DATA.clusters.find(c => c.family === family);
  return hit ? hit.colour : '#8a8f98';
}

// Colour is the genre family, so the legend is the map's key: two bubbles in the
// same colour hold the same broad kind of music, wherever they sit.
(function buildLegend() {
  const counts = new Map();
  for (const c of DATA.clusters) counts.set(c.family, (counts.get(c.family) || 0) + 1);
  document.getElementById('legend').innerHTML = [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([family, n]) => '<span><i style="background:' + colourOf(family) + '"></i>' +
      esc(family) + ' ' + n + '</span>').join('');
})();

function emphasise() {
  const only = picked.size === 1 ? [...picked][0] : null;
  const partners = only === null ? null : (neighbours.get(only) || new Map());
  for (const b of bubbles) {
    const key = Number(b.dataset.key);
    const on = picked.has(key);
    const linked = partners !== null && partners.has(key);
    b.classList.toggle('active', on);
    b.classList.toggle('linked', linked);
    b.classList.toggle('dimmed', picked.size > 0 && !on && !linked);
  }
  for (const e of edges) {
    const on = picked.size === 0
      || picked.has(Number(e.dataset.a)) || picked.has(Number(e.dataset.b));
    e.style.strokeOpacity = on ? '' : '0.02';
  }
}

// One cluster asks what it points at; several ask what they all point at. Every
// candidate carries the clusters that point at it, so this is a filter over one
// list rather than an intersection of lists that were cut before the question
// was known.
function selectedCandidates() {
  const need = picked.size > 1 ? 2 : 1;
  const rows = [];
  for (const candidate of DATA.candidates) {
    const hits = [];
    for (const key in candidate.clusters) {
      if (picked.has(Number(key))) hits.push(Number(key));
    }
    if (hits.length < need) continue;
    let pointers = 0;
    for (const key of hits) pointers += candidate.clusters[String(key)] || 0;
    rows.push({
      name: candidate.name,
      styles: candidate.styles,
      hits: hits,
      from: hits.map(key => (byKey.get(key) || {}).label || String(key)),
      pointers: pointers,
    });
  }
  rows.sort((a, b) => b.hits.length - a.hits.length || b.pointers - a.pointers);
  return rows;
}

// Where this area's music is released. The labels on the selected clusters' own
// artists say what the area is signed to; the labels on the candidates say where
// the suggestions come from.
function topLabels(rows, chosen) {
  const tally = new Map();
  for (const cluster of chosen) {
    for (const entry of (cluster.labels || [])) {
      tally.set(entry.name, (tally.get(entry.name) || 0) + entry.n);
    }
  }
  for (const row of rows) {
    const candidate = labelOf.get(row.name);
    if (candidate) for (const name of candidate.labels) {
      tally.set(name, (tally.get(name) || 0) + 1);
    }
  }
  return [...tally.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 8);
}

function renderPanel() {
  if (!picked.size) {
    scope.innerHTML = '<h2>Pick a cluster</h2>' +
      '<p class="meta">Bubbles are groups of artists that occur together, coloured by ' +
      'genre family. Click one, or several, to see what they point at.</p>';
    return;
  }
  const chosen = [...picked].map(k => byKey.get(k)).filter(Boolean);
  let head;
  if (chosen.length === 1) {
    const c = chosen[0];
    const genres = c.genres.length
      ? c.genres.slice(0, 5).map(g => '<span>' + esc(g.name) + ' ' + g.n + '</span>').join('') +
        '<span>' + c.tagged + ' of ' + c.size.toLocaleString() + ' tagged</span>'
      : '<span>no genres recorded yet</span>';
    head = '<h2 style="color:' + c.colour + '">' + esc(c.label) + '</h2>' +
      '<p class="meta">' + c.size.toLocaleString() + ' artists · ' +
      c.corpus.toLocaleString() + ' of them yours · cluster ' + c.key + ' · ' +
      esc(c.family) + '</p>' +
      (c.sub_genre ? '<p class="own-tag">Your own tag for it: <strong>' +
        esc(c.sub_genre) + '</strong></p>' : '') +
      '<div class="genres">' + genres + '</div>';
  } else {
    const size = chosen.reduce((n, c) => n + c.size, 0);
    const yours = chosen.reduce((n, c) => n + c.corpus, 0);
    head = '<h2>' + chosen.length + ' clusters combined</h2>' +
      '<p class="meta">' + size.toLocaleString() + ' artists · ' +
      yours.toLocaleString() + ' of them yours</p>';
  }
  const chips = chosen.map(c => '<button data-drop="' + c.key + '">' +
    esc(c.label) + ' ×</button>').join('');
  // With one cluster on, name the clusters that would answer. Choosing blind is
  // how you land on one of the five hundred and seventy-four pairs that share
  // nothing and read it as a broken page.
  let partners = '';
  if (picked.size === 1) {
    const linked = [...(neighbours.get([...picked][0]) || new Map())]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 12);
    partners = linked.length
      ? '<p class="aside">Shares candidates with</p><div class="partners">' +
        linked.map(([key, n]) => '<button data-add="' + key + '">' +
          esc((byKey.get(key) || {}).label || key) + ' <b>' + n + '</b></button>').join('') +
        '</div>'
      : '<p class="aside">Shares no candidates with any other cluster.</p>';
  }
  const rows = selectedCandidates();
  const suggest = s => {
    const tags = (s.styles || []).slice(0, 3).map(esc).join(' · ');
    const where = s.from.length > 1
      ? ' across ' + s.from.length + ' of the selections: ' + s.from.map(esc).join(', ')
      : ' from ' + esc(s.from[0] || '');
    return '<li><div class="name">' + esc(s.name) + '</div>' +
      '<div class="why">' + s.pointers + ' pointing artists' + where +
      '</div>' + (tags ? '<div class="tags">' + tags + '</div>' : '') + '</li>';
  };
  const labels = topLabels(rows, chosen);
  const card = labels.length
    ? '<div class="card"><p class="card-title">Publishing labels here</p><div class="labels">' +
      labels.map(([name, n]) => '<span>' + esc(name) + ' <b>' + n + '</b></span>').join('') +
      '</div></div>'
    : '';
  let body;
  if (rows.length) {
    body = '<ol class="suggestions">' + rows.slice(0, 40).map(suggest).join('') + '</ol>';
  } else if (picked.size > 1) {
    body = '<p class="empty">Nothing is pointed at by more than one of these ' +
      picked.size + ' clusters. Individually they suggest plenty; together they ' +
      'suggest nothing.</p>';
  } else {
    body = '<p class="empty">Nothing reaches this cluster yet. Expanding from it ' +
      'would find candidates.</p>';
  }
  scope.innerHTML = head + card + '<div class="picked">' + chips + '</div>' +
    partners + body;
  for (const button of scope.querySelectorAll('[data-drop]')) {
    button.addEventListener('click', () => {
      picked.delete(Number(button.dataset.drop));
      emphasise();
      renderPanel();
    });
  }
  for (const button of scope.querySelectorAll('[data-add]')) {
    button.addEventListener('click', () => {
      picked.add(Number(button.dataset.add));
      emphasise();
      renderPanel();
    });
  }
}

for (const b of bubbles) {
  const key = Number(b.dataset.key);
  // Hovering inspects and clicking picks, and the two are separate: the list
  // stays on what was picked while the pointer reads the rest. Returning early
  // on a pick made every other cluster unreadable.
  b.addEventListener('mouseenter', () => {
    const c = byKey.get(key);
    tip.textContent = c.label + ' · ' + c.size.toLocaleString() + ' artists · ' + c.family;
  });
  b.addEventListener('focus', () => b.dispatchEvent(new Event('mouseenter')));
  b.addEventListener('mouseleave', () => { tip.textContent = ''; });
  b.addEventListener('click', () => {
    if (moved > 4) return;   // that was a pan, not a pick
    if (picked.has(key)) picked.delete(key); else picked.add(key);
    emphasise();
    renderPanel();
  });
}

// Pan and zoom. A map you cannot move into is a diagram, and the tail of small
// clusters is only legible once you can lean in.
const svg = document.getElementById('map');
const box = svg.getAttribute('viewBox').split(/\s+/).map(Number);
const home = { x: box[0], y: box[1], w: box[2], h: box[3] };
let zoom = 1;
let offset = { x: 0, y: 0 };
let moved = 0;
let drag = null;

function applyView() {
  const w = home.w / zoom;
  svg.setAttribute('viewBox',
    (home.x - offset.x) + ' ' + (home.y - offset.y) + ' ' + w + ' ' + (home.h / zoom));
  // Labels are drawn in graph units, so they grow with the zoom. Scaling the
  // font the other way keeps them the same size on screen at any depth.
  const unitsPerPixel = w / Math.max(1, svg.clientWidth);
  svg.style.setProperty('--inner-label', (11 * unitsPerPixel).toFixed(2) + 'px');
}

// The sub-groups inside a cluster are unreadable at rest and obvious up close,
// so they appear once the zoom can carry them and the parent steps back.
function revealInner() {
  const show = zoom >= 2.4;
  document.getElementById('inner').style.display = show ? '' : 'none';
  for (const b of bubbles) b.classList.toggle('open', show);
}

svg.addEventListener('wheel', event => {
  event.preventDefault();
  const rect = svg.getBoundingClientRect();
  const px = (event.clientX - rect.left) / Math.max(1, rect.width);
  const py = (event.clientY - rect.top) / Math.max(1, rect.height);
  const w0 = home.w / zoom, h0 = home.h / zoom;
  const x0 = home.x - offset.x, y0 = home.y - offset.y;
  // The graph point under the cursor, held still while everything scales around it.
  const gx = x0 + px * w0, gy = y0 + py * h0;
  zoom = Math.min(24, Math.max(0.5, zoom * Math.exp(-event.deltaY * 0.0016)));
  offset.x = home.x - (gx - px * (home.w / zoom));
  offset.y = home.y - (gy - py * (home.h / zoom));
  applyView();
  revealInner();
}, { passive: false });

svg.addEventListener('pointerdown', event => {
  drag = { x: event.clientX, y: event.clientY, id: event.pointerId, panning: false };
  moved = 0;
});
svg.addEventListener('pointermove', event => {
  if (!drag || event.pointerId !== drag.id) return;
  const stepX = event.clientX - drag.x;
  const stepY = event.clientY - drag.y;
  moved += Math.abs(stepX) + Math.abs(stepY);
  // Capture only once this is a drag. Capturing on pointerdown redirects the
  // click away from the bubble, so no bubble ever received one and nothing could
  // be selected.
  if (!drag.panning) {
    if (moved <= 4) return;
    drag.panning = true;
    // Capture can throw if the pointer is already gone, and a failed capture
    // must not abort the pan.
    try { svg.setPointerCapture(event.pointerId); } catch (error) { /* keep panning */ }
  }
  const unit = (home.w / zoom) / Math.max(1, svg.clientWidth);
  // Adding, not subtracting: the view follows the hand. Subtracting moved the
  // map the opposite way from the pointer.
  offset.x += stepX * unit;
  offset.y += stepY * unit;
  drag = { x: event.clientX, y: event.clientY, id: drag.id, panning: true };
  applyView();
});
svg.addEventListener('pointerup', () => { drag = null; });
svg.addEventListener('pointercancel', () => { drag = null; });
svg.addEventListener('dblclick', () => {
  zoom = 1; offset = { x: 0, y: 0 }; applyView(); revealInner();
});

// Nothing is selected on load. Choosing is the point, and a pre-picked cluster
// made the first click remove it.
applyView();
emphasise();
renderPanel();
</script>
</html>
"""


def render_page(
    title: str,
    clusters: list[Cluster],
    edges: list[tuple[int, int, int]],
    candidates: list[dict],
) -> str:
    return (
        PAGE.replace("__TITLE__", html.escape(title))
        .replace("__SVG__", _cluster_svg(clusters, edges))
        .replace("__PAYLOAD__", _payload(clusters, candidates))
    )
