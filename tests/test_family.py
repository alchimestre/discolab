from __future__ import annotations

import pytest

from discolab.map_view import family_of

# The fold is the only thing between a tag and a colour, so a word it does not
# know tints a whole region grey. These are the words the graph actually holds.
CASES = [
    ("techno", "Electronic"),
    ("hardgroove", "Electronic"),
    ("chillout", "Electronic"),
    ("trip-hop", "Electronic"),
    ("ambient", "Electronic"),
    ("Hip-Hop", "Hip hop & Soul"),
    ("rap", "Hip hop & Soul"),
    ("rnb", "Hip hop & Soul"),
    ("Classical", "Classical"),
    ("classique", "Classical"),
    ("early music", "Classical"),
    ("indian classical", "Classical"),
    ("Soundtrack", "Soundtrack"),
    ("jazz", "Jazz & Blues"),
    ("psychedelic rock", "Rock"),
    ("shoegaze", "Rock"),
    ("cambodian rock", "Rock"),
    ("flamenco", "Folk & World"),
    ("arabic pop", "Folk & World"),
    ("world", "Folk & World"),
    ("experimental", "Experimental"),
    ("avant-garde", "Experimental"),
]


@pytest.mark.parametrize("label, family", CASES)
def test_a_label_folds_to_its_family(label, family):
    assert family_of(label) == family


def test_an_unknown_label_folds_to_other():
    assert family_of("field recordings of rain") == "Other"
