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


@dataclass
class Cluster:
    key: int
    size: int
    corpus: int
    tagged: int = 0
    genres: tuple[tuple[str, int], ...] = ()
    artists: tuple[str, ...] = ()
    x: float = 0.0
    y: float = 0.0
    r: float = 0.0
    suggestions: list[dict] = field(default_factory=list)

    @property
    def label(self) -> str:
        """A genre name, but only where the cluster actually has one.

        The largest cluster holds 16% of the graph across 42 genres, its top one
        covering 27% of its tagged artists. Naming it after that genre would be
        an invention the map states as fact, so it says mixed instead.
        """
        if not self.genres:
            return f"cluster {self.key}"
        # A name needs enough tagged artists behind it to mean anything, but not
        # a fraction of the whole cluster: only six per cent of artists are
        # tagged at all, so a coverage threshold would name nothing. Eight is a
        # floor on the evidence, and the panel states the sample it came from.
        if self.tagged < 8:
            return f"cluster {self.key}"
        name, count = self.genres[0]
        if count >= self.tagged * 0.5:
            return name
        return "mixed"


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
            f"{html.escape(label[:20])}</text>"
            if show
            else ""
        )
        bubbles.append(
            f'<g class="bubble" data-key="{cluster.key}" tabindex="0" role="button" '
            f'aria-label="{html.escape(title)}">'
            f"<title>{html.escape(title)}</title>"
            f'<circle cx="{cluster.x:.1f}" cy="{cluster.y:.1f}" r="{cluster.r:.1f}"/>'
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
  .bubble circle { fill: var(--fill); stroke: var(--line); stroke-width: 1; cursor: pointer; }
  .bubble text { fill: var(--ink); pointer-events: none; opacity: .82; }
  .bubble:hover circle, .bubble:focus-visible circle { stroke: var(--hot); stroke-width: 2; }
  .bubble:hover text, .bubble:focus-visible text { opacity: 1; }
  .bubble.active circle { fill: color-mix(in srgb, CanvasText 24%, transparent);
                          stroke: var(--hot); stroke-width: 2.5; }
  .bubble.dimmed { opacity: .22; }
  .bubble:focus { outline: none; }
  #hint { position: absolute; left: 18px; bottom: 14px; font-size: 12px;
          color: var(--quiet); pointer-events: none; }
  aside { border-left: 1px solid var(--line); overflow-y: auto; padding: 16px 18px 40px; }
  #scope h2 { font-size: 17px; margin: 0 0 2px; }
  #scope .meta { font-size: 12px; color: var(--quiet); margin-bottom: 10px; }
  #scope .genres { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 14px; }
  #scope .genres span { font-size: 11px; padding: 1px 7px; border-radius: 9px;
                        border: 1px solid var(--line); color: var(--quiet); }
  #scope .in-cluster { font-size: 12px; color: var(--quiet); margin-bottom: 14px; }
  ol.suggestions { list-style: none; margin: 0; padding: 0; counter-reset: s; }
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
  <div id="scope">
    <h2>Pick a cluster</h2>
    <p class="meta">Bubbles are groups of artists that occur together. Size is how many artists the group holds.</p>
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
let locked = null;

document.getElementById('stat').textContent =
  DATA.clusters.length + ' clusters · ' +
  DATA.clusters.reduce((n, c) => n + c.size, 0).toLocaleString() + ' artists';

function esc(s) { return String(s).replace(/[&<>"]/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

function emphasise(key) {
  for (const b of bubbles) {
    const same = b.dataset.key === String(key);
    b.classList.toggle('dimmed', key !== null && !same);
  }
  for (const e of edges) {
    const on = key === null || e.dataset.a === String(key) || e.dataset.b === String(key);
    e.style.strokeOpacity = on ? '' : '0.02';
  }
}

function render(key) {
  const c = byKey.get(key);
  if (!c) return;
  const genres = c.genres.length
    ? c.genres.map(g => '<span>' + esc(g.name) + ' ' + g.n + '</span>').join('') +
      '<span>' + c.tagged + ' of ' + c.size.toLocaleString() + ' tagged</span>'
    : '<span>nothing recorded for this group yet</span>';
  const inside = c.artists.length
    ? '<div class="in-cluster">Already yours: ' + c.artists.map(esc).join(', ') + '</div>'
    : '';
  const list = c.suggestions.length
    ? '<ol class="suggestions">' + c.suggestions.map(s => {
        const tags = (s.styles || []).slice(0, 3).map(esc).join(' · ');
        return '<li><div class="name">' + esc(s.name) + '</div>' +
          '<div class="why">' + s.agreement + ' artist' + (s.agreement === 1 ? '' : 's') +
          ' in this group point at it' +
          (s.via ? ' · ' + esc(s.via) : '') + '</div>' +
          (tags ? '<div class="tags">' + tags + '</div>' : '') + '</li>';
      }).join('') + '</ol>'
    : '<p class="empty">Nothing reaches this group yet. Expanding from it would find candidates.</p>';
  scope.innerHTML =
    '<h2>' + esc(c.label) + '</h2>' +
    '<p class="meta">' + c.size.toLocaleString() + ' artists · ' +
    c.corpus.toLocaleString() + ' of them yours · cluster ' + c.key + '</p>' +
    '<div class="genres">' + genres + '</div>' + inside + list;
}

for (const b of bubbles) {
  const key = Number(b.dataset.key);
  // Hovering inspects, clicking scopes, and the two are separate: the list stays
  // on the cluster chosen while the pointer moves over the rest. Returning early
  // on a lock made every other cluster unreadable once one was picked.
  b.addEventListener('mouseenter', () => {
    emphasise(key);
    const c = byKey.get(key);
    tip.textContent = c.label + ' · ' + c.size.toLocaleString() + ' artists';
  });
  b.addEventListener('focus', () => b.dispatchEvent(new Event('mouseenter')));
  b.addEventListener('mouseleave', () => {
    emphasise(locked);
    tip.textContent = '';
  });
  b.addEventListener('click', () => {
    locked = locked === key ? null : key;
    for (const other of bubbles) {
      other.classList.toggle('active', Number(other.dataset.key) === locked);
    }
    emphasise(locked);
    render(locked ?? biggest);
  });
}

const biggest = DATA.clusters.slice().sort((a, b) => b.size - a.size)[0].key;
render(biggest);
</script>
</html>
"""


def render_page(title: str, clusters: list[Cluster], edges: list[tuple[int, int, int]]) -> str:
    return (
        PAGE.replace("__TITLE__", html.escape(title))
        .replace("__SVG__", _cluster_svg(clusters, edges))
        .replace("__PAYLOAD__", _payload(clusters))
    )
