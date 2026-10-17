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
    x: float = 0.0
    y: float = 0.0
    r: float = 0.0
    tag: str = ""
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
        """Tint from the cluster's family, which is also what names it."""
        return FAMILY_COLOUR.get(self.family, FAMILY_COLOUR["Other"])

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


# How far a cluster's glow reaches past its own radius.
HALO = 1.9


def _colour(family: str) -> str:
    return FAMILY_COLOUR.get(family, FAMILY_COLOUR["Other"])


def _heatmap_svg(clusters: list[Cluster]) -> str:
    """Draw the taste as a heat field: density, no outlines, no names at rest.

    Every cluster is a soft radial blob at its layout position, tinted by the
    family it is named for. Where blobs meet the colour deepens, so the reader
    sees where the library is thick without a single boundary. Names arrive with
    the zoom, because a map that names everything at rest names nothing.
    """
    families: list[str] = []
    for cluster in clusters:
        family = cluster.family
        if family not in families:
            families.append(family)
    tint = {family: f"heat-{index}" for index, family in enumerate(families)}
    defs = "".join(
        f'<radialGradient id="{tint[family]}">'
        f'<stop offset="0" stop-color="{_colour(family)}" stop-opacity="0.60"/>'
        f'<stop offset="0.50" stop-color="{_colour(family)}" stop-opacity="0.32"/>'
        f'<stop offset="1" stop-color="{_colour(family)}" stop-opacity="0"/>'
        f"</radialGradient>"
        for family in families
    )

    heat: list[str] = []
    hits: list[str] = []
    for cluster in clusters:
        family = cluster.family
        title = f"{cluster.family} · {cluster.size:,} artists"
        heat.append(
            f'<circle class="heat" data-key="{cluster.key}" '
            f'cx="{cluster.x:.1f}" cy="{cluster.y:.1f}" '
            f'r="{cluster.r * HALO:.1f}" fill="url(#{tint[family]})"/>'
        )
        hits.append(
            f'<circle class="hit" data-key="{cluster.key}" tabindex="0" '
            f'role="button" aria-label="{html.escape(title)}" '
            f'cx="{cluster.x:.1f}" cy="{cluster.y:.1f}" r="{cluster.r:.1f}">'
            f"<title>{html.escape(title)}</title></circle>"
        )

    # The viewBox has to hold the halos, not the cores, or the heat is cut off at
    # the edge of the pane.
    reach = [cluster.r * HALO for cluster in clusters]
    left = min(cluster.x - extent for cluster, extent in zip(clusters, reach))
    right = max(cluster.x + extent for cluster, extent in zip(clusters, reach))
    top = min(cluster.y - extent for cluster, extent in zip(clusters, reach))
    bottom = max(cluster.y + extent for cluster, extent in zip(clusters, reach))
    pad = 12.0
    return (
        f'<svg id="map" viewBox="{left - pad:.1f} {top - pad:.1f} '
        f'{right - left + pad * 2:.1f} {bottom - top + pad * 2:.1f}" '
        f'preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">'
        f"<defs>{defs}</defs>"
        f'<g id="heat">{"".join(heat)}</g>'
        f'<g id="hits">{"".join(hits)}</g>'
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
                    "family": cluster.family,
                    "colour": cluster.colour,
                    "sub_genre": cluster.sub_genre,
                    "genres": [{"name": name, "n": n} for name, n in cluster.genres],
                    "labels": [{"name": name, "n": n} for name, n in cluster.labels],
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
  .heat { pointer-events: none; transition: opacity .12s; }
  .heat.dimmed { opacity: .22; }
  /* The selected and linked rings set fill: none, and an unpainted SVG shape
     receives no pointer events, so without this a selected cluster could not be
     clicked again to deselect it. */
  .hit { fill: transparent; cursor: pointer; pointer-events: all; }
  .hit:hover { fill: color-mix(in srgb, currentColor 7%, transparent); }
  .hit.active { fill: none; stroke: currentColor; stroke-width: 1.5;
                stroke-dasharray: 7 5; vector-effect: non-scaling-stroke; }
  .hit.linked { fill: none; stroke: currentColor; stroke-width: 1;
                stroke-dasharray: 3 6; stroke-opacity: .5;
                vector-effect: non-scaling-stroke; }
  .hit:focus { outline: none; }
  .hit:focus-visible { fill: none; stroke: currentColor; stroke-width: 2;
                       vector-effect: non-scaling-stroke; }
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
  <p id="hint">Hover to inspect. Click a region to scope the suggestions.</p>
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
const heats = [...document.querySelectorAll('.heat')];
const hits = [...document.querySelectorAll('.hit')];
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
  for (const heat of heats) {
    const key = Number(heat.dataset.key);
    const on = picked.has(key);
    const linked = partners !== null && partners.has(key);
    heat.classList.toggle('dimmed', picked.size > 0 && !on && !linked);
  }
  for (const hit of hits) {
    const key = Number(hit.dataset.key);
    hit.classList.toggle('active', picked.has(key));
    hit.classList.toggle('linked', partners !== null && partners.has(key));
  }
}

// One cluster asks what it points at; several ask what they all point at. Every
// candidate carries the clusters that point at it, so this is a filter over one
// list rather than an intersection of lists that were cut before the question
// was known.
function selectedCandidates() {
  // Nothing selected is a question too: what does the whole library suggest.
  // Ranked by how many clusters point at it, so the answer spans the taste
  // rather than sitting inside one corner of it.
  if (picked.size === 0) {
    return DATA.candidates
      .map(c => ({
        name: c.name,
        styles: c.styles,
        hits: Object.keys(c.clusters).map(Number),
        from: [],
        pointers: Object.values(c.clusters).reduce((a, b) => a + b, 0),
      }))
      .sort((a, b) => b.hits.length - a.hits.length || b.pointers - a.pointers)
      .slice(0, 60);
  }
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
      from: hits.map(key => (byKey.get(key) || {}).family || String(key)),
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
  const global = picked.size === 0;
  let head = '';
  const chosen = [...picked].map(k => byKey.get(k)).filter(Boolean);

  if (global) {
    head = '<h2>Everything at once</h2>' +
      '<p class="meta">' + DATA.candidates.length.toLocaleString() + ' artists nobody here ' +
      'has, ranked by how many groups of your taste point at them. Pick a cluster to ' +
      'narrow it.</p>';
  } else if (chosen.length === 1) {
    const c = chosen[0];
    const genres = c.genres.length
      ? c.genres.slice(0, 5).map(g => '<span>' + esc(g.name) + ' ' + g.n + '</span>').join('') +
        '<span>' + c.tagged + ' of ' + c.size.toLocaleString() + ' tagged</span>'
      : '<span>no genres recorded yet</span>';
    head = '<h2 style="color:' + c.colour + '">' + esc(c.family) + '</h2>' +
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
    esc(c.family) + ' ×</button>').join('');
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
          esc((byKey.get(key) || {}).family || key) + ' <b>' + n + '</b></button>').join('') +
        '</div>'
      : '<p class="aside">Shares no candidates with any other cluster.</p>';
  }
  const rows = selectedCandidates();
  const suggest = s => {
    const tags = (s.styles || []).slice(0, 3).map(esc).join(' · ');
    let where;
    if (!s.from.length) {
      where = ' across ' + s.hits.length + ' of your clusters';
    } else if (s.from.length > 1) {
      where = ' across ' + s.from.length + ' of the selections: ' + s.from.map(esc).join(', ');
    } else {
      where = ' from ' + esc(s.from[0]);
    }
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

for (const hit of hits) {
  const key = Number(hit.dataset.key);
  // Hovering inspects and clicking picks, and the two are separate: the list
  // stays on what was picked while the pointer reads the rest. Returning early
  // on a pick made every other cluster unreadable.
  hit.addEventListener('mouseenter', () => {
    const c = byKey.get(key);
    tip.textContent = c.family + ' · ' + c.size.toLocaleString() + ' artists';
  });
  hit.addEventListener('focus', () => hit.dispatchEvent(new Event('mouseenter')));
  hit.addEventListener('mouseleave', () => { tip.textContent = ''; });
  hit.addEventListener('click', () => {
    if (moved > 4) return;   // that was a pan, not a pick
    if (picked.has(key)) picked.delete(key); else picked.add(key);
    emphasise();
    renderPanel();
  });
}

// Pan and zoom. A map you cannot move into is a diagram, and the tail of small
// clusters is only legible once you can lean in.
const svg = document.getElementById('map');
const box = svg.getAttribute('viewBox').trim().split(/[ ,]+/).map(Number);
const home = { x: box[0], y: box[1], w: box[2], h: box[3] };
let zoom = 1;
let offset = { x: 0, y: 0 };
let moved = 0;
let drag = null;

function applyView() {
  const w = home.w / zoom;
  svg.setAttribute('viewBox',
    (home.x - offset.x) + ' ' + (home.y - offset.y) + ' ' + w + ' ' + (home.h / zoom));
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
  zoom = 1; offset = { x: 0, y: 0 }; applyView();
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
    candidates: list[dict],
) -> str:
    return (
        PAGE.replace("__TITLE__", html.escape(title))
        .replace("__SVG__", _heatmap_svg(clusters))
        .replace("__PAYLOAD__", _payload(clusters, candidates))
    )
