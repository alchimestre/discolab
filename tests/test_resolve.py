from __future__ import annotations

from discolab.model import RawTrack
from discolab.resolve import (
    build_catalog,
    normalise,
    parse_credits,
    split_artists,
)


def raw(path: str, artist: str | None, title: str, **kwargs) -> RawTrack:
    return RawTrack(
        source="test",
        path=path,
        artist_raw=artist,
        title_raw=title,
        album_raw=kwargs.get("album"),
        duration_ms=kwargs.get("duration_ms"),
        containers=kwargs.get("containers", ()),
    )


def test_normalise_folds_accents_case_and_punctuation():
    assert normalise("El Búho") == "el buho"
    assert normalise("  La   Vipère Rouge ") == "la vipere rouge"
    assert normalise("Damien Raud; Mashup") == "damien raud mashup"
    assert normalise("A.B.C.") == "a b c"


def test_normalise_keeps_non_latin_names_distinct():
    assert normalise("Молчат Дома") == "молчат дома"
    assert normalise("شيرين") == "شيرين"
    # NFKD folds the Greek final sigma onto sigma, so both spellings share a key.
    assert normalise("Λένα Πλάτωνος") == "λένα πλάτωνοσ"
    assert normalise("Λένα Πλάτωνος") == normalise("Λένα Πλάτωνοσ")
    assert normalise("Молчат Дома") != normalise("Мечта")


def test_split_artists_handles_non_latin_separators():
    assert split_artists("Λένα Πλάτωνος & Σαβίνα Γιαννάτου") == [
        "Λένα Πλάτωνος",
        "Σαβίνα Γιαννάτου",
    ]


def test_split_artists_uses_explicit_separators():
    assert split_artists("Abraxsophia; Nagham Saleh") == ["Abraxsophia", "Nagham Saleh"]
    assert split_artists("Terrafractyl vs Spacey Koala") == ["Terrafractyl", "Spacey Koala"]
    assert split_artists("Olkan & La Vipère Rouge") == ["Olkan", "La Vipère Rouge"]


def test_split_artists_keeps_ampersand_inside_comma_names():
    assert split_artists("Earth, Wind & Fire") == ["Earth, Wind & Fire"]


def test_split_artists_drops_placeholder_and_keeps_the_real_name():
    assert split_artists("Various Artists, Moving Still") == ["Moving Still"]
    assert split_artists("Various Artists") == []
    assert split_artists("") == []


def test_split_artists_drops_scrobble_placeholders():
    assert split_artists("Anonymous") == []
    assert split_artists("[unknown]") == []
    assert split_artists("Unknown Artist") == []


def test_parse_credits_reads_remixers():
    assert parse_credits("Pulo NDJ", "Dabadji Am Alcorama (El Búho remix)") == [
        ("El Búho", "remix")
    ]
    assert parse_credits("Damien Raud", "By The Way You Move [Tres Puntos Remix]") == [
        ("Tres Puntos", "remix")
    ]
    assert parse_credits("Adam Beyer", "Track (Adam Beyer & Bart Skils Remix)") == [
        ("Bart Skils", "remix")
    ]


def test_parse_credits_drops_version_labels_years_and_self_credits():
    assert parse_credits("X", "Track (Original Mix)") == []
    assert parse_credits("X", "Track (Extended Mix)") == []
    assert parse_credits("X", "Track (2022 Remix)") == []
    assert parse_credits("Chamos", "Dameek Majeb (Chamos Edit)") == []


def test_parse_credits_strips_version_words_from_remixers():
    assert parse_credits("Fluid", "Fluid (feat Iris Gold - Trutopia extended remix)") == [
        ("Iris Gold", "feature"),
        ("Trutopia", "feature"),
        ("Iris Gold", "remix"),
        ("Trutopia", "remix"),
    ]


def test_parse_credits_reads_features():
    assert parse_credits("A", "Song (feat. B)") == [("B", "feature")]


def test_build_catalog_merges_the_same_track_across_roots():
    catalog = build_catalog(
        [
            raw("/lossless/Artist - Song.aiff", "Artist", "Song", duration_ms=300_000),
            raw("/lossy/Artist - Song.mp3", "Artist", "Song", duration_ms=299_000),
        ]
    )
    assert len(catalog.tracks) == 1
    assert len(catalog.tracks[0].paths) == 2
    assert len(catalog.artists) == 1


def test_build_catalog_turns_second_artist_into_a_credit():
    catalog = build_catalog([raw("/x/a - b.flac", "Olkan & La Vipère Rouge", "Tango")])
    assert len(catalog.tracks) == 1
    assert len(catalog.artists) == 2
    assert [(credit.artist_name, credit.role) for credit in catalog.credits] == [
        ("La Vipère Rouge", "collab")
    ]


def test_build_catalog_skips_tracks_with_no_artist():
    catalog = build_catalog([raw("/x/loose.flac", "Various Artists", "Some Song")])
    assert len(catalog.tracks) == 0
    assert catalog.skipped == (("/x/loose.flac", "no artist"),)


def test_build_catalog_records_container_membership():
    catalog = build_catalog(
        [
            raw("/x/a - one.flac", "A", "One", containers=(("folder", "Crate"),)),
            raw("/x/b - two.flac", "B", "Two", containers=(("folder", "Crate"),)),
        ]
    )
    assert len(catalog.containers) == 1
    assert len(catalog.memberships) == 2
