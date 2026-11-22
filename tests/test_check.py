import pytest

from discolab.check import Video, library_keys, tag_for, track_plays


@pytest.mark.parametrize(
    ("video_title", "known_title", "library_title", "expected"),
    [
        ("Artist - Good", "Go", "", "new"),
        ("Artist - Ongoing", "Going", "", "new"),
        ("Good Artist - (Go)", "Go", "", "already held"),
        ("Artist - Another Beautiful Daylight", "", "Beautiful Day", "new"),
        ("Artist - Beautiful Day (Live)", "", "Beautiful Day", "already held"),
        ("Artist - Go (Live)", "", "Go", "new"),
    ],
)
def test_embed_badge_requires_whole_title_words(
    video_title: str, known_title: str, library_title: str, expected: str
) -> None:
    video = Video("video", video_title)
    badge, _ = tag_for(video, [(known_title, 3)], library_keys([library_title]))
    assert badge == expected


def test_play_count_ignores_partial_words() -> None:
    video = Video("video", "Artist - Good (Live)")
    assert track_plays(video, [("Go", 9), ("Good", 3)]) == 3
