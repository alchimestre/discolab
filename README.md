# discolab

Builds a taste graph from local music metadata, then ranks the artists that sit between otherwise separate parts of it.

Reads audio files from disk. No network access, no account, no external service.

## Requirements

- Python 3.11 or newer, and `uv`
- A container runtime for Neo4j: Docker or OrbStack

## Setup

1. `uv sync`
2. `cp .env.example .env`, then set `DISCOLAB_LIBRARY_ROOTS` to your music folders separated by `:`
3. `docker compose up -d`
4. `uv run discolab run --root ~/Music/Tracks`
5. `uv run discolab check`

Neo4j Browser is at `http://localhost:7474`.

## Commands

| Command | Does |
|---|---|
| `discolab ingest --root PATH [--root PATH] [--recreate] [--lastfm] [--scrobbles N]` | Read providers, resolve entities, load observed nodes and edges |
| `discolab derive [--resolution F] [--min-artists N] [--max-artists N]` | Build artist edges, communities, islands, bridge scores |
| `discolab candidates [--limit N] [--min-degree N] [--min-bridge F] [--no-tracks]` | Rank bridging artists with the tracks you own |
| `discolab spotify-login [--container]` | Authorise against Spotify once, caching the refresh token |
| `discolab enrich [--rekordbox PATH] [--deezer] [--min-agreement N] [--workers N]` | Attach curated metadata: BPM and key from rekordbox, genres and labels from Deezer |
| `discolab expand [--per-cluster N] [--per-seed N] [--genre TAGS]` | Expand from cluster centres to artists you do not have |
| `discolab check [--source bridge\|external] [--min-areas N] [--limit N] [--tracks N] [--out PATH] [--youtube/--no-youtube] [--cache PATH] [--refresh]` | Write a local listening page |
| `discolab votes [--path PATH]` | Summarise the ticks and read the criterion |
| `discolab stats [--islands N]` | Node, edge and island counts |
| `discolab reset --yes` | Delete every node and relationship |
| `discolab run --root PATH` | Ingest, derive, then candidates |

```
uv run discolab run --root "$HOME/Music/Tracks"
uv run discolab candidates --limit 30 --min-degree 1
uv run discolab check --limit 25 --out out/checklist.html
uv run discolab stats --islands 15
```

## Checking the candidates

`discolab check` writes one HTML page, one section per artist, with links to Spotify, YouTube, Bandcamp, Discogs and MusicBrainz. Tick the ones worth playing again and the header counts them.

Each section embeds one official track. Candidates are found by searching the artist name, then the titles of tracks the library already holds, which is what turns a thin and repetitive pool into the actual uploads.

| Rule | Reason |
|---|---|
| Single tracks only | Sets, mixes and full albums are rejected on title, or on a length above 15 minutes |
| The artist's own channel, or their distributor's Topic channel | A name search returns fan re-uploads first |
| A verified channel only when the title opens with the artist's name, and only when the artist has no channel of their own | Verified businesses re-upload anything |
| Many uploads per artist, ranked by channel trust then by whether the library holds the track | Uploaders can block embedding, so the page needs somewhere to go next |

Each embed is tagged `new` or `already held`, matched by title against the tracks in the library. The tag updates if the page moves to another upload.

The header reports how many embeds are tracks you already hold. Ticking those tests recognition rather than discovery, so they do not count towards the criterion.

The page listens for the YouTube player's error event and loads the next candidate, so an upload that blocks embedding is replaced rather than becoming a dead frame. Only when every candidate is blocked does it fall back to a thumbnail and a link. It records how many it tried, which distinguishes an artist who blocks embedding channel-wide from a retry loop that failed.

An artist with no official upload gets no embed rather than a re-upload. `Humanoids` on the sample library is one.

The kill criterion is on that page, and the ticks are written to `out/votes.json` as they are made, so closing the tab loses nothing and the boxes come back ticked on the next build. `discolab votes` prints the reading:

```
$ uv run discolab votes
kept 6 of 25 candidates
  new tracks: 3
  already known: 3
criterion not met: fewer than 3 new tracks kept
```

Only ticks on tracks you do not already hold count towards the criterion, so `discolab votes` separates them.

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
| `LASTFM_API_KEY` | none | Enables `--lastfm`. The key alone is enough; the shared secret is not used |
| `LASTFM_USERNAME` | none | The account whose scrobbles to read |
| `SPOTIFY_CLIENT_ID` | none | Enables `--spotify` |
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

`--lastfm` reads scrobble history and gives two things the files cannot.

Play counts, from `user.gettoptracks`, written to `Track.plays`. That is the first real play signal in the graph, so unplayed becomes definable.

Listening sessions, from `user.getrecenttracks`. Scrobbles are grouped into sessions split by a thirty minute gap, and each session becomes a container. Containers are what the co-occurrence edges are built from, so a session turns real listening into artist to artist edges. A library of loose single tracks has almost no albums, which is why own-data co-occurrence contributed one edge before this.

Reading more history reads more sessions: `--scrobbles N` sets how many recent scrobbles to read, and `LastfmProvider.top_pages` caps the top-tracks pagination.

## What Spotify adds

`--spotify` reads every playlist plus liked songs and takes two things from them.

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
uv run discolab expand --per-cluster 3
uv run discolab check --source external --genre "electronic,techno,house,psytrance"
```

`--per-cluster` takes the top artists of every cluster rather than the loudest artists overall, so the seeds spread across the whole taste instead of concentrating in the largest island.

Expansion stores each seed's Last.fm tags on the seed, and `check --genre` counts agreement only from seeds carrying one of the listed tags. Filtering at expansion time is not enough on its own: the graph keeps every earlier run, so a seed expanded before the filter existed still counts. That is the difference between a filter on what gets fetched and a filter on what gets shown.

Then build the page from those artists rather than from your own:

```
uv run discolab check --source external
```

One seed suggesting an artist says almost nothing, and that is most of a similarity list. Several seeds agreeing says more, and seeds in different clusters agreeing says most. The page is ordered by how many seeds agree, most first, and shows how many areas they came from.

Measured over 200 seeds: 3,778 suggestions, 1,028 already in your corpus, and agreement of 5 seeds for 4 artists, 4 for 8, 3 for 46, 2 for 238, and 1 for 2,079. The single seed tail is the low quality material and it does not reach the page.

## Enriching the nodes

```
brew install sqlcipher
uv run discolab enrich
```

Rekordbox holds the BPM, the musical key and the DJ's own tags for every file, and that is the highest signal metadata available here: it is the owner's classification of their own records rather than a service's guess.

Rekordbox 6 encrypts `master.db` with SQLCipher, so reading it needs the `sqlcipher` binary. Every row is local and the key is a published constant.

| Written | From |
|---|---|
| `Track.bpm` | `djmdContent.BPM`, stored as a hundredth so 12700 is 127.00 |
| `Track.musical_key` | `djmdContent.KeyID` through `djmdKey.ScaleName` |
| `Track.rating`, `Track.dj_plays` | `djmdContent.Rating`, `DJPlayCount` |
| `Tag` nodes, `(Track)-[:TAGGED]->(Tag)` | `djmdMyTag` through `djmdSongMyTag` |
| `(Artist)-[:TAGGED {tracks}]->(Tag)` | the union of its tracks, weighted by count |

The join is on the file path, and it must be normalised to NFC first: the filesystem returns decomposed accents and rekordbox composed ones, which made an otherwise exact join match nothing. Measured against the sample library, 1,820 of 1,821 paths match, all 1,820 carry a BPM and a key, and 878 artists end up with tags.

The tags are a curated taxonomy spanning style and region, so they filter far more sharply than Last.fm's. `Raw / Deep / Hypnotic`, `Peak Time / Driving`, `Minimal / Deep Tech`, `Hard Techno`, `Progressive Psy-Trance`, `Maghreb & Mashriq`, `Tuareg Desert Blues`, `Occitan & French Folk`. With BPM and key alongside them, a mix-oriented query is a filter rather than a guess.

### Deezer

```
uv run discolab enrich --deezer --min-agreement 0 --limit 6000 --workers 10
```

Deezer supplies genres and label names, written as properties rather than as graph nodes.

| Written | From |
|---|---|
| `Artist.styles` | the genres of the artist's albums |
| `Artist.labels` | the label on those albums |
| `Artist.enriched` | whether the artist has been asked about |

It resumes: every artist asked about is marked, so the next run continues where the last stopped. It is concurrent because Deezer allows it — measured 300 requests in 3.7 seconds with no rejection, where Discogs allows one a second and would need a hundred minutes for the same graph.

**The route matters more than the query.** `search/album` matches album *titles*, so it returns compilations and soundtracks that merely share a name: Rome resolved to a video-game score and Quelza to a Brazilian record. The correct route is `search/artist`, then that artist's own albums, then each album for its genres and label. That returns `Warp Records` for Squarepusher, `Dekmantel` for Polygonia and `MORD` for Quelza.

Deezer answers a near miss with a different artist, `Rome` resolving to `Romé`, so a result is rejected unless the name matches exactly.

Genres are coarser than Discogs styles — `Electro`, `Techno/House`, `Dance` rather than `IDM` and `Breakbeat` — and they are right, which the coarse alternatives were not.

**Labels are a property, not a graph.** Affinity through labels measured at zero: 1,770 distinct labels across 406 artists, not one holding two of the same collection's artists. A collection of one release per small label cannot express signing affinity. The label name stays as one string on the page because it is the only curated human context there: `Ostgut Ton` says what Shed sounds like faster than any score.

`enrich` defaults to `--min-agreement 2`, asking only about artists two or more seeds agree on, because those are the only ones that can reach a page. `--min-agreement 0` walks everything.

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
