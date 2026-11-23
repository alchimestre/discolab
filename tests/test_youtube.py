from discolab.youtube import Video, resolve_artist


def test_resolve_artist_prefers_new_upload_at_same_trust_rank(monkeypatch) -> None:
    videos = [
        Video("held", "Artist - Go", "Artist - Topic"),
        Video("new", "Artist - Good", "Artist - Topic"),
    ]
    monkeypatch.setattr("discolab.youtube.search_videos", lambda _: videos)
    assert resolve_artist("Artist", ["Go"], limit=1) == [videos[1]]
