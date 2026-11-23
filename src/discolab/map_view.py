"""Where the taste areas sit on the map, and the genre family that tints each.

Areas are sized by how many artists they hold and placed so that areas sharing
discoveries settle near one another.
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass

WIDTH = 1000.0
HEIGHT = 1000.0
PADDING = 16.0
ITERATIONS = 500
SEED = 7

# Raw labels come sixty to a graph and several of them mean one thing to a
# listener: techno, house, trance and electro are one family. Naming a cluster
# after its top raw label spends sixty names on six ideas, so the labels are
# folded into families first, and the family is what tints the heat. Raw labels
# stay in the panel, where there is room for them.
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

# Matching is on whole words. A substring test reads "rai" inside "rain" and
# "ost" inside "post-rock", which put field recordings in Folk & World and
# post-rock in Soundtrack.
_SEPARATORS = re.compile(r"[^\w&']+")


def _phrase(text: str) -> str:
    return " " + _SEPARATORS.sub(" ", text.casefold().strip()) + " "


FAMILY_RULES = tuple(
    (family, tuple(_phrase(needle) for needle in needles))
    for family, needles in (
        ("Hip hop & Soul", ("rap", "hip hop", "hip-hop", "hiphop", "r&b", "rnb",
                            "soul", "funk", "grime", "drill", "trap", "boom bap",
                            "dancehall", "ragga", "g-funk", "neo soul")),
        ("Classical", ("classique", "classical", "baroque", "opéra", "opera",
                       "renaissance", "médiéval", "medieval", "early music",
                       "chamber", "orchestral", "carnatic", "hindustani")),
        ("Soundtrack", ("films", "bandes originales", "jeux vidéo", "comédie",
                        "livres audio", "soundtrack", "score", "video game", "ost")),
        ("Jazz & Blues", ("jazz", "blues", "bossa", "bebop", "swing", "ragtime")),
        # Before Pop and Electronic: "arabic pop" is a world record, not a chart
        # one, and the specific family has to be asked first to say so.
        ("Folk & World", ("folk", "country", "singer", "latino", "africaine",
                          "african", "arabe", "arabic", "indienne", "brésilienne",
                          "bollywood", "reggae", "world", "maghreb", "mashriq",
                          "anatolia", "levant", "occitan", "sub-saharan", "sahel",
                          "andes", "balkans", "cajun", "lament", "lullaby",
                          "balearic", "tuareg", "gnawa", "sufi", "qawwali", "rai",
                          "desert blues", "celtic", "greek", "flamenco",
                          "reggaeton", "oriental", "andalusian", "morna")),
        ("Pop", ("pop", "variété", "chanson", "jeunesse", "comptines",
                 "singer-songwriter")),
        ("Electronic", ("electro", "techno", "house", "dance", "trance", "disco",
                        "dubstep", "club", "edm", "dub", "hardgroove", "acidcore",
                        "schranz", "idm", "breakbeat", "jungle", "ambient", "ebm",
                        "synth", "wave", "psytrance", "hardstyle", "rave", "gabber",
                        "dnb", "drum and bass", "minimal", "chillout", "downtempo",
                        "trip-hop", "trip hop", "industrial", "glitch", "drone",
                        "dark ambient", "bass music", "garage")),
        ("Rock", ("rock", "metal", "alternative", "indie", "indé", "punk",
                  "psychedelic", "shoegaze", "grunge", "stoner", "sludge", "doom",
                  "krautrock", "post-rock", "hardcore", "emo", "screamo",
                  "gothic")),
        ("Experimental", ("experimental", "avant-garde", "avant garde", "free jazz",
                          "noise", "musique concrète", "electroacoustic")),
    )
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
    "Experimental": "#a3a3f5",
    "Other": "#9aa0aa",
}


def family_of(genre: str) -> str:
    """Fold one raw label into a family."""
    key = genre.casefold().strip()
    if key in FAMILY_EXACT:
        return FAMILY_EXACT[key]
    words = _phrase(genre)
    for family, needles in FAMILY_RULES:
        if any(needle in words for needle in needles):
            return family
    return "Other"


@dataclass
class Area:
    key: int
    size: int
    genres: tuple[tuple[str, int], ...] = ()
    x: float = 0.0
    y: float = 0.0
    r: float = 0.0

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
        """The heaviest family that names a sound.

        Unknown labels are counted with the rest, and on Last.fm's long tail they
        outnumber any one genre: eight clusters were tinting grey while holding
        hundreds of techno and house mentions between them.
        """
        for name, _ in self.families:
            if name != "Other":
                return name
        return "Other"

    @property
    def colour(self) -> str:
        """Tint from the area's family."""
        return FAMILY_COLOUR.get(self.family, FAMILY_COLOUR["Other"])


def radius_for(size: int, largest: int) -> float:
    """Square root, so area carries the count rather than the radius."""
    return 9.0 + 52.0 * math.sqrt(size / largest)


def layout(clusters: list[Area], edges: list[tuple[int, int, int]]) -> None:
    """Place the bubbles, in place.

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
