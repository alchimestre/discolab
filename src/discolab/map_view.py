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
                      "indienne", "brésilienne", "bollywood", "reggae", "world")),
)

FAMILY_COLOUR = {
    "Electronic": "#4d8df6",
    "Hip hop & Soul": "#f2872f",
    "Rock": "#e2574c",
    "Pop": "#dd5fae",
    "Jazz & Blues": "#d4a11c",
    "Classical": "#9b7bd4",
    "Folk & World": "#35a56a",
    "Soundtrack": "#33a3bb",
    "Other": "#8a8f98",
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
    artists: tuple[str, ...] = ()
    x: float = 0.0
    y: float = 0.0
    r: float = 0.0
    tag: str = ""
    name: str = ""
    candidates: list[str] = field(default_factory=list)
    suggestions: list[dict] = field(default_factory=list)

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
        return FAMILY_COLOUR.get(self.family, FAMILY_COLOUR["Other"])

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
        names = [name for _, name in scored]
        if len(names) > 1:
            names.insert(1, f"{names[0]} & {names[1]}")
        names += [name for name, _ in cluster.pool if name not in names]
        names += [cluster.family]
        cluster.candidates = names

    taken: set[str] = set()
    # Largest first, so the biggest clusters get the best of contested names.
    for cluster in sorted(clusters, key=lambda c: (-c.size, c.key)):
        chosen = ""
        for candidate in cluster.candidates:
            if candidate and candidate not in taken:
                chosen = candidate
                break
        if not chosen:
            chosen = f"cluster {cluster.key}"
        cluster.name = chosen
        taken.add(chosen)


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
            f'fill="{cluster.colour}" fill-opacity="0.18" stroke="{cluster.colour}"/>'
            f"{text}</g>"
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
        f"</svg>"
    )


def _payload(clusters: list[Cluster]) -> str:
    return json.dumps(
        {
            "clusters": [
                {
                    "key": cluster.key,
                    "size": cluster.size,
                    "corpus": cluster.corpus,
                    "tagged": cluster.tagged,
                    "label": cluster.label,
                    "family": cluster.family,
                    "colour": cluster.colour,
                    "sub_genre": cluster.sub_genre,
                    "genres": [{"name": name, "n": n} for name, n in cluster.genres],
                    "artists": list(cluster.artists),
                    "suggestions": cluster.suggestions,
                }
                for cluster in clusters
            ]
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
  .bubble circle { stroke-width: 1.2; cursor: pointer; transition: fill-opacity .12s; }
  .bubble text { fill: var(--ink); pointer-events: none; opacity: .9; }
  .bubble:hover circle, .bubble:focus-visible circle { stroke-width: 2.6; fill-opacity: .38; }
  .bubble:hover text, .bubble:focus-visible text { opacity: 1; }
  .bubble.active circle { stroke-width: 3.2; fill-opacity: .52; }
  .bubble.dimmed { opacity: .2; }
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
  ol.suggestions { list-style: none; margin: 0; padding: 0; }
  p.tier { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
           color: var(--quiet); margin: 16px 0 2px; }
  p.tier:first-of-type { margin-top: 4px; }
  ol.suggestions .from { color: var(--quiet); }
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
  const any = picked.size > 0;
  for (const b of bubbles) {
    const on = picked.has(Number(b.dataset.key));
    b.classList.toggle('active', on);
    b.classList.toggle('dimmed', any && !on);
  }
  for (const e of edges) {
    const on = !any || picked.has(Number(e.dataset.a)) || picked.has(Number(e.dataset.b));
    e.style.strokeOpacity = on ? '' : '0.02';
  }
}

// One list for however many clusters are on, read the way the selection reads:
// first what a single selected cluster points at, then what several point at,
// which is the ground between them. Within a tier, the artist more of the
// selection points at comes first.
//
// The pool is the union of every selected cluster's own list, not a top slice of
// it, so an artist that is middling in two clusters surfaces where it would miss
// the cut in either alone.
function combined() {
  const totals = new Map();
  for (const key of picked) {
    const c = byKey.get(key);
    if (!c) continue;
    for (const s of c.suggestions) {
      const row = totals.get(s.name) ||
        { name: s.name, pointers: 0, styles: s.styles, via: s.via, fromKeys: [], from: [] };
      row.pointers += s.agreement;
      if (!row.fromKeys.includes(c.key)) {
        row.fromKeys.push(c.key);
        row.from.push(c.label);
      }
      totals.set(s.name, row);
    }
  }
  const rows = [...totals.values()];
  const byPointers = (a, b) => b.pointers - a.pointers;
  return {
    direct: rows.filter(r => r.fromKeys.length === 1).sort(byPointers),
    between: rows.filter(r => r.fromKeys.length > 1)
      .sort((a, b) => b.fromKeys.length - a.fromKeys.length || byPointers(a, b)),
  };
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
  const tiers = combined();
  const suggest = s => {
    const tags = (s.styles || []).slice(0, 3).map(esc).join(' · ');
    const where = s.from.length > 1
      ? ' across ' + s.from.length + ' selections: ' + s.from.map(esc).join(', ')
      : ' from ' + esc(s.from[0] || '');
    return '<li><div class="name">' + esc(s.name) + '</div>' +
      '<div class="why">' + s.pointers + ' pointing artists' + where +
      (s.via ? ' · ' + esc(s.via) : '') + '</div>' +
      (tags ? '<div class="tags">' + tags + '</div>' : '') + '</li>';
  };
  const parts = [];
  if (tiers.direct.length) {
    if (picked.size > 1) {
      parts.push('<p class="tier">Directly from one selected cluster</p>');
    }
    parts.push('<ol class="suggestions">' +
      tiers.direct.slice(0, 20).map(suggest).join('') + '</ol>');
  }
  if (tiers.between.length) {
    parts.push('<p class="tier">Between them, pointed at by more than one</p>');
    parts.push('<ol class="suggestions">' +
      tiers.between.slice(0, 20).map(suggest).join('') + '</ol>');
  }
  const body = parts.length
    ? parts.join('')
    : '<p class="empty">Nothing reaches these clusters yet.</p>';
  scope.innerHTML = head + '<div class="picked">' + chips + '</div>' + body;
  for (const button of scope.querySelectorAll('[data-drop]')) {
    button.addEventListener('click', () => {
      picked.delete(Number(button.dataset.drop));
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
    if (picked.has(key)) picked.delete(key); else picked.add(key);
    emphasise();
    renderPanel();
  });
}

emphasise();
renderPanel();
</script>
</html>
"""


def render_page(title: str, clusters: list[Cluster], edges: list[tuple[int, int, int]]) -> str:
    return (
        PAGE.replace("__TITLE__", html.escape(title))
        .replace("__SVG__", _cluster_svg(clusters, edges))
        .replace("__PAYLOAD__", _payload(clusters))
    )
