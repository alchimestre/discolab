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
| `discolab check [--limit N] [--tracks N] [--out PATH] [--youtube/--no-youtube] [--cache PATH] [--refresh]` | Write a local listening page |
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

The kill criterion is on that page. Fewer than 3 of 25 kept means the query is wrong, the data is too thin, or blank space between clusters is not fertile. Write the outcome down before changing anything.

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

## Known limits

- Edges come only from title credits and shared containers. A library of single-artist albums produces almost none.
- Container edges are ignored for containers holding fewer than 2 or more than 80 distinct artists, since the wide ones make everything co-occur.
- Entity resolution is name-based. Aliases such as `Sterac aka Steve Rachmad` and `Sterac, Steve Rachmad` remain separate artists.
- No play counts are read, so unplayed cannot yet be defined.
- A verified channel that curates rather than releases is accepted when the title opens with the artist's name. Telling a label from a curator needs label data the graph does not hold yet.
- Measured on a 1824 file library of loose tracks: 450 of 1225 artists connected, 775 isolated, 157 islands, largest island 24 artists.

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
