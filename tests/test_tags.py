from __future__ import annotations

from discolab.tags import LastfmTagger


class FakeTagger(LastfmTagger):
    """Answers a fixed payload without touching the network."""

    def __init__(self, payload):
        super().__init__("key", rate=0)
        self.payload = payload

    def _call(self, method, artist):
        return self.payload


def tags(*pairs):
    return {"toptags": {"tag": [{"name": n, "count": c} for n, c in pairs]}}


def test_keeps_the_genres_and_drops_the_tail():
    tagger = FakeTagger(tags(("hardgroove", 100), ("techno", 40), ("schranz", 2)))
    assert tagger.tags("Stan Christ") == ["hardgroove", "techno"]


def test_drops_demonyms_whatever_their_length():
    # Demonyms are endless, so the suffix rule carries what no list would hold.
    tagger = FakeTagger(
        tags(("lebanese", 100), ("tibetan", 90), ("burmese", 80), ("jazz", 70))
    )
    assert tagger.tags("Charif Megarbane") == ["jazz"]


def test_drops_places_platforms_and_moods():
    tagger = FakeTagger(
        tags(("netherlands", 100), ("bandcamp", 90), ("relaxing", 80), ("techno", 70))
    )
    assert tagger.tags("Someone") == ["techno"]


def test_an_artist_lastfm_does_not_know_has_no_tags():
    assert FakeTagger({"error": 6, "message": "The artist you supplied could not be found"}).tags("Nobody") == []


def test_an_artist_with_only_junk_tags_has_no_tags():
    assert FakeTagger(tags(("french", 100), ("seen live", 50))).tags("Someone") == []


def test_no_more_than_three_tags():
    tagger = FakeTagger(
        tags(("techno", 100), ("minimal", 90), ("dub", 80), ("acid", 70), ("idm", 60))
    )
    assert len(tagger.tags("Someone")) == 3
