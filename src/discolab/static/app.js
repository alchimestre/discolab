const mapPane = document.querySelector(".map-pane");
const listPane = document.querySelector(".list-pane");
const PAGE_SIZE = 40;
const MAP_PADDING = 40;
const LABEL_PX = 11;
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 24;
const WHEEL_RATE = 0.0016;
const DRAG_THRESHOLD_PX = 4;
const EVERY_AREA = "#/";

let graph;
let areas;
let listed = [];
let shown = 0;

const youtubeReady = new Promise(resolve => {
  window.onYouTubeIframeAPIReady = resolve;
});

load();
window.addEventListener("hashchange", showSelection);
listPane.addEventListener("click", event => {
  const listen = event.target.closest(".listen");
  if (listen) play(listen.closest(".card"));
  const more = event.target.closest(".more");
  if (more) more.outerHTML = nextCards();
});

async function load() {
  const response = await fetch("/api/graph");
  if (!response.ok) {
    listPane.textContent = `The database could not be read (${response.status}).`;
    return;
  }
  graph = await response.json();
  areas = new Map(graph.areas.map(area => [area.id, area]));
  graph.discoveries.forEach((discovery, index) => {
    discovery.rank = index + 1;
  });
  if (!graph.areas.length) {
    listPane.innerHTML = "<h1>Nothing to show yet</h1><p class='lede'>Run <code>discolab ingest</code>, then <code>discolab guess</code>.</p>";
    return;
  }
  mapPane.innerHTML = mapView();
  panAndZoom(mapPane.querySelector("svg.map"));
  showSelection();
}

// The selected areas live in the address, so back and reload keep them.
function selectedAreas() {
  const ids = location.hash.slice(2).split(",").filter(Boolean).map(Number);
  return ids.filter(id => areas.has(id)).map(id => areas.get(id));
}

// The address that adds an unselected area, or drops a selected one.
function toggleLink(id) {
  const ids = selectedAreas().map(area => area.id);
  const toggled = ids.includes(id) ? ids.filter(other => other !== id) : [...ids, id];
  return EVERY_AREA + toggled.join(",");
}

function showSelection() {
  const selected = selectedAreas();
  for (const bubble of mapPane.querySelectorAll("svg.map a")) {
    const id = Number(bubble.dataset.area);
    bubble.toggleAttribute("aria-current", selected.some(area => area.id === id));
    bubble.setAttribute("href", toggleLink(id));
  }
  mapPane.querySelector("svg.map").classList.toggle("selecting", selected.length > 0);
  listPane.innerHTML = selected.length ? selectionList(selected) : everyList();
  listPane.scrollTop = 0;
}

function everyList() {
  return `<h1>Between your areas</h1>
    <p class="lede">${graph.discoveries.length} artists you do not have, each one reached from
    two separate areas of your taste. Ranked by the share of the weaker area's seeds that lead there.
    Click bubbles to see what they share.</p>
    ${cards(graph.discoveries)}`;
}

function selectionList(selected) {
  // An artist's weakest support among the selected areas; zero when one of them does not vote.
  const weakest = discovery => Math.min(...selected.map(area =>
    discovery.votes.find(vote => vote.area === area.id)?.support ?? 0));
  const common = graph.discoveries.filter(discovery => weakest(discovery) > 0);
  const several = selected.length > 1;
  if (several) common.sort((a, b) => weakest(b) - weakest(a));
  const heading = several
    ? `<h1>Where ${selected.length} areas meet</h1>`
    : `<h1 class="tinted" style="--tint:${selected[0].colour}">${escapeHtml(areaName(selected[0]))}</h1>`;
  const count = several
    ? `${common.length} artists every one of them leads to`
    : `${common.length} artists shared with another area`;
  return `${heading}
    <p class="picked">${selected.map(areaChip).join("")}</p>
    <p class="lede">${count} · <a href="${EVERY_AREA}">Show every area</a></p>
    ${selected.map(memberList).join("")}
    ${cards(common) || "<p class='quiet'>Nothing is reached from all of these at once.</p>"}`;
}

function memberList(area) {
  const members = area.artists.map(name => `<li>${escapeHtml(name)}</li>`).join("");
  return `<details>
    <summary>${area.artists.length} of your artists in ${escapeHtml(areaName(area))}</summary>
    <ul class="members">${members}</ul>
  </details>`;
}

function areaChip(area) {
  return `<a class="chip" style="--tint:${area.colour}" href="${toggleLink(area.id)}">${escapeHtml(areaName(area))}</a>`;
}

function mapView() {
  const left = Math.min(...graph.areas.map(area => area.x - area.r)) - MAP_PADDING;
  const top = Math.min(...graph.areas.map(area => area.y - area.r)) - MAP_PADDING;
  const right = Math.max(...graph.areas.map(area => area.x + area.r)) + MAP_PADDING;
  const bottom = Math.max(...graph.areas.map(area => area.y + area.r)) + MAP_PADDING;
  const heaviest = Math.max(1, ...graph.links.map(link => link.shared));
  const families = new Map(graph.areas.map(area => [area.family, area.colour]));
  const legend = [...families].map(([family, colour]) =>
    `<span style="--tint:${colour}">${escapeHtml(family)}</span>`).join("");
  return `<div class="legend">${legend}</div>
    <svg class="map" viewBox="${left} ${top} ${right - left} ${bottom - top}" role="img" aria-label="Map of your taste areas">
      ${graph.links.map(link => mapLine(link, heaviest)).join("")}
      ${graph.areas.map(mapBubble).join("")}
    </svg>`;
}

function mapLine(link, heaviest) {
  const from = areas.get(link.a);
  const to = areas.get(link.b);
  return `<line x1="${from.x}" y1="${from.y}" x2="${to.x}" y2="${to.y}"
    stroke-width="${1 + 11 * link.shared / heaviest}"><title>${link.shared} shared</title></line>`;
}

function mapBubble(area) {
  return `<a data-area="${area.id}" style="--tint:${area.colour}">
    <circle cx="${area.x}" cy="${area.y}" r="${area.r}"/>
    <text class="style" x="${area.x}" y="${area.y}" dy="-0.2em">${escapeHtml(area.styles[0] || "")}</text>
    <text x="${area.x}" y="${area.y}" dy="1em">${escapeHtml(area.artists[0])}</text>
    <title>${area.artists.length} artists · ${escapeHtml(area.artists.slice(0, 5).join(", "))}</title>
  </a>`;
}

function panAndZoom(svg) {
  const home = svg.viewBox.baseVal;
  const size = { width: home.width, height: home.height };
  const rest = { x: home.x, y: home.y, zoom: 1 };
  const view = { ...rest };
  let drag = null;
  let dragged = false;

  // Screen pixels per graph unit, taken from the drawn map: the box is letterboxed.
  const scale = () => svg.getScreenCTM().a;

  function apply() {
    svg.setAttribute("viewBox", `${view.x} ${view.y} ${size.width / view.zoom} ${size.height / view.zoom}`);
    // Names are drawn in graph units, so the font scales the other way to stay one size on screen.
    svg.style.setProperty("--area-label", `${(LABEL_PX / scale()).toFixed(2)}px`);
  }

  svg.addEventListener("wheel", event => {
    event.preventDefault();
    const before = svg.getScreenCTM();
    // The graph point under the cursor, held still while everything scales around it.
    const graphX = (event.clientX - before.e) / before.a;
    const graphY = (event.clientY - before.f) / before.d;
    view.zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, view.zoom * Math.exp(-event.deltaY * WHEEL_RATE)));
    apply();
    const after = svg.getScreenCTM();
    view.x += graphX - (event.clientX - after.e) / after.a;
    view.y += graphY - (event.clientY - after.f) / after.d;
    apply();
  }, { passive: false });

  svg.addEventListener("pointerdown", event => {
    drag = { x: event.clientX, y: event.clientY, id: event.pointerId, travelled: 0 };
    dragged = false;
  });
  svg.addEventListener("pointermove", event => {
    if (!drag || event.pointerId !== drag.id) return;
    const stepX = event.clientX - drag.x;
    const stepY = event.clientY - drag.y;
    drag.travelled += Math.abs(stepX) + Math.abs(stepY);
    if (!dragged) {
      if (drag.travelled <= DRAG_THRESHOLD_PX) return;
      dragged = true;
      // Capture only once this is a drag: capturing on pointerdown takes the click from the bubble.
      // Capture can throw if the pointer is already gone, and that must not abort the pan.
      try { svg.setPointerCapture(event.pointerId); } catch { /* keep panning */ }
    }
    // The view moves against the hand so the map follows it.
    view.x -= stepX / scale();
    view.y -= stepY / scale();
    drag.x = event.clientX;
    drag.y = event.clientY;
    apply();
  });
  svg.addEventListener("pointerup", () => { drag = null; });
  svg.addEventListener("pointercancel", () => { drag = null; });
  // A drag that ends over a bubble must not select its area.
  svg.addEventListener("click", event => {
    if (dragged) event.preventDefault();
  }, true);
  // Resets only from the empty map: a second click on a bubble is an unselect.
  svg.addEventListener("dblclick", event => {
    if (event.target.closest("a")) return;
    Object.assign(view, rest);
    apply();
  });
  apply();
}

function cards(discoveries) {
  listed = discoveries;
  shown = 0;
  return nextCards();
}

function nextCards() {
  const page = listed.slice(shown, shown + PAGE_SIZE);
  shown += page.length;
  const more = shown < listed.length
    ? `<button type="button" class="more">Show more · ${listed.length - shown} left</button>`
    : "";
  return page.map(card).join("") + more;
}

function card(discovery) {
  const page = discovery.url && discovery.url.startsWith("https://")
    ? discovery.url
    : `https://www.last.fm/music/${encodeURIComponent(discovery.name)}`;
  return `<article class="card" data-name="${escapeHtml(discovery.name)}">
    <h2><span class="rank">${discovery.rank}</span><a href="${escapeHtml(page)}" target="_blank" rel="noopener">${escapeHtml(discovery.name)}</a></h2>
    <p class="quiet">${escapeHtml(discovery.styles.slice(0, 4).join(" · "))}</p>
    <ul class="votes">${discovery.votes.map(voteLine).join("")}</ul>
    <div class="player"><button type="button" class="listen">▶ Listen</button></div>
  </article>`;
}

function voteLine(vote) {
  const area = areas.get(vote.area);
  return `<li>${areaChip(area)}
    <span class="quiet">${Math.round(vote.support * 100)}% of its seeds · via ${escapeHtml(vote.seeds.join(", "))}</span></li>`;
}

async function play(card) {
  const slot = card.querySelector(".player");
  const name = card.dataset.name;
  slot.innerHTML = "<p class='quiet'>Searching YouTube…</p>";
  const response = await fetch(`/api/videos?name=${encodeURIComponent(name)}`).catch(() => null);
  if (!response?.ok) {
    giveUp(slot, name, "The search could not be run.");
    return;
  }
  const videos = await response.json();
  if (!videos.length) {
    giveUp(slot, name, "No official upload found.");
    return;
  }
  await youtubeReady;
  slot.innerHTML = "<div class='frame'></div><p class='quiet'></p>";
  const caption = slot.querySelector("p");
  let playing = 0;
  caption.textContent = videos[playing].title;
  new YT.Player(slot.querySelector(".frame"), {
    videoId: videos[playing].video_id,
    playerVars: { autoplay: 1, origin: location.origin },
    events: {
      // An uploader that blocks embedding raises an error, so the next upload is tried.
      onError: event => {
        playing += 1;
        if (playing === videos.length) {
          giveUp(slot, name, "Every official upload blocks embedding.");
          return;
        }
        caption.textContent = videos[playing].title;
        event.target.loadVideoById(videos[playing].video_id);
      },
    },
  });
}

function giveUp(slot, name, reason) {
  const search = `https://www.youtube.com/results?search_query=${encodeURIComponent(name)}`;
  slot.innerHTML = `<p class="quiet">${reason} <a href="${search}" target="_blank" rel="noopener">Search YouTube</a></p>`;
}

function areaName(area) {
  const style = area.styles[0] ? `${area.styles[0]} · ` : "";
  return style + area.artists.slice(0, 2).join(", ");
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, char => `&#${char.charCodeAt(0)};`);
}
