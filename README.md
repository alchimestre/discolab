# discolab

Builds a taste graph from what you listen to, then ranks the artists that sit between otherwise separate parts of it.

Three sources are aggregated into one graph: the audio files on disk, Last.fm scrobbles, and Spotify playlists and liked songs. Deezer then adds genres and labels.

## Requirements

- Python 3.11 or newer, and `uv`
- A container runtime for Neo4j: Docker or OrbStack
- `sqlcipher`, for the rekordbox step: `brew install sqlcipher`. Without it that step is skipped and every other source still runs.

## Setup

1. `uv sync`
2. `cp .env.example .env`, then set `DISCOLAB_LIBRARY_ROOTS` to your music folders separated by `:`
3. `docker compose up -d`
4. `uv run discolab spotify-login`, once, if you want Spotify in the graph
5. `uv run discolab ingest`

Neo4j Browser is at `http://localhost:7474`.

## The loop

| Step | Command | What it does |
|---|---|---|
| 1 | `discolab ingest` | Aggregate every source, resolve the names, load the graph, then attach rekordbox's BPM, keys and tags |
| 2 | `discolab enrich` | Genres and labels from Deezer |
| 3 | `discolab guess` | Derive taste communities, expand past them, write the page |

```
uv run discolab ingest
uv run discolab enrich
uv run discolab guess
```

Every source is read unless it is switched off or unconfigured, so `ingest` needs no flags. `--no-lastfm` and `--no-spotify` skip one. A source missing its credentials is reported and skipped rather than stopping the run.

The three steps are not optional and not ordered by taste: `guess` reads communities that `ingest` produced, and `expand` inside it needs Last.fm.

| Command | Does |
|---|---|
| `discolab derive` | Rebuild artist edges, communities and islands on their own |
| `discolab expand` | Ask Last.fm for artists similar to each cluster centre |
| `discolab check` | Write one listening page, scoped by cluster or by source |
| `discolab clusters --pages` | One index page and one frontier page per cluster |
| `discolab map` | Draw the graph as a bubble map, suggestions scoped to a cluster |
| `discolab stats` | Node, edge and island counts |
| `discolab prune` | Drop unplayed tracks and the artists left with none |
| `discolab reset --yes` | Delete every node and relationship |
| `discolab spotify-login` | Authorise against Spotify once, caching the refresh token |

## What rekordbox adds

`ingest` reads the BPM, the musical key and the DJ's own tags out of `master.db` and writes them onto the tracks already in the graph.

It is local and it costs nothing beyond one binary: no network, no account, and the only requirement is `sqlcipher`, since rekordbox 6 encrypts the database with a published constant key. Without it the step reports why and the rest of the run continues.

| Written | From |
|---|---|
| `Track.bpm` | `djmdContent.BPM`, stored as a hundredth so 12700 is 127.00 |
| `Track.musical_key` | `djmdContent.KeyID` through `djmdKey.ScaleName` |
| `Track.rating`, `Track.dj_plays` | `djmdContent.Rating`, `DJPlayCount` |
| `Tag` nodes, `(Track)-[:TAGGED]->(Tag)` | `djmdMyTag` through `djmdSongMyTag` |
| `(Artist)-[:TAGGED {tracks}]->(Tag)` | the union of its tracks, weighted by count |

The join is on the file path, folded to NFC first. The filesystem returns decomposed accents and rekordbox composed ones, so without that fold an exact join matches nothing while looking correct.

Measured on one library: 1,832 rows, every one with a BPM and a key, and 1,543 carrying at least one tag.

The tags are a curated taxonomy spanning style and region, so they filter far more sharply than Last.fm's: `Minimal / Deep Tech`, `Raw / Deep / Hypnotic`, `Peak Time / Driving`, `Hard Techno`, `Maghreb & Mashriq`. With BPM and key alongside them, a mix-oriented query is a filter rather than a guess.

`--no-rekordbox` skips the step.

## Looking at the shape of it

`discolab map` draws the graph as bubbles and scopes suggestions to one of them.

| Element | Meaning |
|---|---|
| A bubble | One cluster, sized by how many artists it holds, placed so clusters that share artists settle near each other |
| Its label | The cluster's genre, but only when one genre covers half its tagged artists. Otherwise it says `mixed` |
| Hovering | Dims everything else, keeps that cluster's links lit, and names it in the toolbar |
| Clicking | Scopes the right-hand list to that cluster, and clicking again releases it |
| The list | Artists that cluster's members point at, most agreed on first, with how many pointed at them and which |

A cluster is named only when the name is true. The largest holds 2,325 artists across 42 genres, its top one covering 27% of its tagged artists, so it says `mixed` rather than claiming to be Electro.

`enrich --per-cluster N` fills in the genres the map needs. Ranking by agreement cannot name a cluster: the artists that define one are the ones it is built around, not the ones most seeds point at.

## Checking the candidates

`discolab check` writes one HTML page, one section per artist, with links to Spotify, YouTube, Bandcamp, Discogs and MusicBrainz.

Each section embeds one official track. Candidates are found by searching the artist name, then the titles of tracks the library already holds, which is what turns a thin and repetitive pool into the actual uploads.

| Rule | Reason |
|---|---|
| Single tracks only | Sets, mixes and full albums are rejected on title, or on a length above 15 minutes |
| The artist's own channel, or their distributor's Topic channel | A name search returns fan re-uploads first |
| A verified channel only when the title opens with the artist's name, and only when the artist has no channel of their own | Verified businesses re-upload anything |
| Many uploads per artist, ranked by channel trust then by whether the library holds the track | Uploaders can block embedding, so the page needs somewhere to go next |

Each embed is tagged `new` or `already held`, matched by title against the tracks in the library. The tag updates if the page moves to another upload.

The header reports how many embeds are tracks you already hold, since those test recognition rather than discovery.

The page listens for the YouTube player's error event and loads the next candidate, so an upload that blocks embedding is replaced rather than becoming a dead frame. Only when every candidate is blocked does it fall back to a thumbnail and a link. It records how many it tried, which distinguishes an artist who blocks embedding channel-wide from a retry loop that failed.

An artist with no official upload gets no embed rather than a re-upload. `Humanoids` on the sample library is one.

The page is written to `out/`, which is not committed, and it lists local file paths.

Resolving the embeds is the only step that touches the network, and `--no-youtube` keeps the whole run offline. The page must be served rather than opened from disk: a `file://` page sends no referrer and YouTube rejects the embed with error 153. Serving is the default.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DISCOLAB_LIBRARY_ROOTS` | none | Library folders, separated by the OS path separator |
| `NEO4J_URI` | `bolt://localhost:7687` | Bolt endpoint |
| `NEO4J_USER` | `neo4j` | |
| `NEO4J_PASSWORD` | `discolab-dev` | Change this outside local development |
| `NEO4J_DATABASE` | `neo4j` | |
| `LASTFM_API_KEY` | none | Skips Last.fm when unset. The key alone is enough; the shared secret is not used |
| `LASTFM_USERNAME` | none | The account whose scrobbles to read |
| `SPOTIFY_CLIENT_ID` | none | Skips Spotify when unset |
| `SPOTIFY_CLIENT_SECRET` | none | |
| `SPOTIFY_REDIRECT_URI` | `http://127.0.0.1:8888/callback` | Must match the app's registered URI exactly |
| `SPOTIFY_CALLBACK_PORT` | `8888` | Must match the port in the redirect URI, since the container publishes it |
| `SPOTIFY_TLS_CERT` | none | Certificate for an https redirect URI |
| `SPOTIFY_TLS_KEY` | none | Its key |

## Graph model

| Node | Key |
|---|---|
| `Artist` | normalised name |
| `Track` | normalised artist plus normalised title |
| `Container` | album tag or subfolder |

Observed relationships, written by `ingest`: `(Track)-[:BY]->(Artist)`, `(Container)-[:CONTAINS]->(Track)`, `(Artist)-[:CREDITED {role}]->(Track)` with role `collab`, `feature`, or `remix`.

Derived relationships, written by `derive`: `(Artist)-[:COLLABORATES]-(Artist)`, `(Artist)-[:CO_OCCURS]-(Artist)`.

Derived properties on `Artist`: `degree`, `community`, `component`, `component_size`, `bridge_score`.

## How the edges are found

Filenames are read as `Artist - Title`. Credits are parsed from that text.

| Source | Example | Edge |
|---|---|---|
| Multiple artists in the name | `Dekel; Eukai - Deepnotech` | collaboration |
| Remix or edit credit | `Baiuca - Meigallo (Matias Aguayo remix)` | remix |
| Feature credit | `AMMAR 808 feat. Sofiane Saidi - Degdega` | feature |
| Version words | `(Original Mix)`, `(Extended Mix)` | dropped, not a person |
| Shared container | two artists on one album tag | co-occurrence |
| A listening session | two artists played within thirty minutes | co-occurrence |

## What Last.fm adds

Last.fm scrobbles give two things the files cannot.

Play counts, from `user.gettoptracks`, written to `Track.plays`. That is the first real play signal in the graph, so unplayed becomes definable.

Listening sessions, from `user.getrecenttracks`. Scrobbles are grouped into sessions split by a thirty minute gap, and each session becomes a container. Containers are what the co-occurrence edges are built from, so a session turns real listening into artist to artist edges. A library of loose single tracks has almost no albums, which is why own-data co-occurrence contributed one edge before this.

Reading more history reads more sessions: `--scrobbles N` sets how many recent scrobbles to read, and `LastfmProvider.top_pages` caps the top-tracks pagination.

## What Spotify adds

Spotify contributes every playlist plus liked songs, and two things come from them.

Playlist membership becomes a container, so artists sharing a playlist co-occur. Liked songs is one large container, and it is capped like any other, so it only contributes when it is small enough to mean something.

Ownership. A track in a playlist is a track you know, whether or not a file for it exists on disk.

## The Spotify login

```
uv run discolab spotify-login              # callback served by this process
uv run discolab spotify-login --container  # callback served by the compose service
```

Both need `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`, a registered redirect URI matching `SPOTIFY_REDIRECT_URI` exactly, and your Spotify account added under the app's **Settings → User Management**. A new app is in development mode and only listed users can authorise, so without that step the login fails with a 403.

Spotify requires HTTPS for redirect URIs except on a loopback address, where plain HTTP is permitted, and it rejects `localhost` as a hostname. So `http://127.0.0.1:8888/callback` works without a certificate, and it keeps the callback off the network entirely.

For an HTTPS redirect URI, either point `SPOTIFY_TLS_CERT` and `SPOTIFY_TLS_KEY` at a certificate, or use `--container`, which runs the callback as the `spotify-callback` compose service. The container needs an HTTP or HTTPS port published to match the redirect URI. It binds all interfaces because a published port cannot reach a container's loopback, which is what `SPOTIFY_BIND_HOST` controls.

The refresh token is cached in `out/spotify-token.json`, which is not committed.

Two limits come from Spotify rather than from here.

A development-mode app can read its own playlists but not the contents of a public playlist owned by someone else. Those return 403 on their items, and the count of them is reported at ingest instead of being skipped silently. Reading them needs Extended Quota review. Playlists you own are unaffected.

The playlist contents endpoint is `/playlists/{id}/items`; `/tracks` was withdrawn and now returns 403. Playlist entries wrap the track under `item`, while saved tracks use `track`, and both are read.

## Expanding beyond your own music

Ranking artists that bridge the graph only returns artists you already have, because every node came from your own data. Those bridges are seeds, not answers.

`discolab expand` takes the most central member of each taste cluster, asks Last.fm for similar artists, and writes the ones you do not have as external nodes.

```
uv run discolab check --cluster 13
uv run discolab check --cluster 13 --periphery
```

Narrowing is done by scoping, not by filtering. The graph stays complete and unfiltered, and a cluster is a choice about where to look rather than a value baked into the rank.

| Scope | What it shows |
|---|---|
| `--cluster N` | artists outside your library reached from that cluster's seeds |
| `--cluster N --periphery` | that cluster's own artists with the fewest internal edges |
| nothing | the whole taste, ranked by how many seeds agree |

Cluster 13 holds six artists and produced the first track kept from a discovery page. Small clusters are the productive ones.

Then build the page from those artists rather than from your own:

```
uv run discolab check --source external
```

One seed suggesting an artist says almost nothing, and that is most of a similarity list. Several seeds agreeing says more, and seeds in different clusters agreeing says most. The page is ordered by how many seeds agree, most first, and shows how many areas they came from.

Requiring only two areas let a group of relaxation and singing-bowl seeds put ten of the top twenty-five on the page, every one of them bridging that group and nothing else. Three areas removed all ten; four changed nothing further. Three is the default.

Measured over 200 seeds: 3,778 suggestions, 1,028 already in your corpus, and agreement of 5 seeds for 4 artists, 4 for 8, 3 for 46, 2 for 238, and 1 for 2,079. The single seed tail is the low quality material and it does not reach the page.

## Enriching

```
uv run discolab enrich
```

Deezer supplies genres and label names, written as properties rather than as graph nodes.

| Written | From |
|---|---|
| `Artist.styles` | the genres of the artist's albums |
| `Artist.labels` | the label on those albums |
| `Artist.enriched` | whether the artist has been asked about |

It resumes: every artist asked about is marked, so the next run continues where the last stopped. Requests are paced at eight a second across every worker, because Deezer serves fifty requests per five-second window and refuses the rest with HTTP 200 and an error body. That body reads as "no genres" unless it is checked, which is how a rate limit gets written into the graph as a fact. A refusal now stays unasked, and the next run retries it.

**The route matters more than the query.** `search/album` matches album *titles*, so it returns compilations and soundtracks that merely share a name: Rome resolved to a video-game score and Quelza to a Brazilian record. The correct route is `search/artist`, then that artist's own albums, then each album for its genres and label. That returns `Warp Records` for Squarepusher, `Dekmantel` for Polygonia and `MORD` for Quelza.

Deezer answers a near miss with a different artist, `Rome` resolving to `Romé`, so a result is rejected unless the name matches exactly.

Genres are coarser than Discogs styles — `Electro`, `Techno/House`, `Dance` rather than `IDM` and `Breakbeat` — and they are right, which the coarse alternatives were not.

**Labels are a property, not a graph.** Affinity through labels measured at zero: 1,770 distinct labels across 406 artists, not one holding two of the same collection's artists. A collection of one release per small label cannot express signing affinity. The label name stays as one string on the page because it is the only curated human context there: `Ostgut Ton` says what Shed sounds like faster than any score.

`enrich` walks the whole graph by default. `--min-agreement 2` restricts it to the artists two or more seeds agree on, which is the only subset that can reach a page.

## Known limits

- Edges come only from title credits and shared containers. A library of single-artist albums produces almost none.
- Container edges are ignored for containers holding fewer than 2 or more than 80 distinct artists, since the wide ones make everything co-occur.
- Entity resolution is name-based. Aliases such as `Sterac aka Steve Rachmad` and `Sterac, Steve Rachmad` remain separate artists.
- A verified YouTube channel that curates rather than releases is accepted when the title opens with the artist's name.
- Deezer genres are coarse. Fine styles such as `IDM` or `Breakbeat` are not available from any source that answers faster than sixty requests a minute.

## Exploring in Neo4j

```cypher
// Islands and their biggest names
MATCH (a:Artist) WHERE a.component >= 0
RETURN a.component AS island, a.component_size AS island_size,
       collect(a.name)[0..8] AS artists
ORDER BY island_size DESC LIMIT 10
```

```cypher
// Everything one artist is connected to
MATCH (a:Artist {name: 'El Búho'})-[r:CO_OCCURS|COLLABORATES]-(n)
RETURN a.name, type(r), n.name
```

## Development

```
uv run pytest
```
