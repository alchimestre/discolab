from __future__ import annotations

from discolab.map_view import Cluster, assign_names


def cluster(key: int, size: int, genres, tags=(), labels=()) -> Cluster:
    return Cluster(
        key=key, size=size, corpus=size, genres=genres, tags=tags, labels=labels
    )


def test_a_genre_over_the_floor_names_its_cluster():
    group = cluster(0, 100, (("Techno", 60), ("House", 40)))
    assign_names([group])
    assert group.name == "Techno"


def test_a_cluster_with_no_dominant_genre_never_claims_one():
    # Eleven per cent rap named a cluster whose recommendations were electro, and
    # the reader took the name for the whole. It may still be named, but marked.
    group = cluster(
        0,
        100,
        (
            ("Rap/Hip Hop", 11),
            ("Electro", 9),
            ("Rock", 8),
            ("Pop", 7),
            ("Jazz", 7),
            ("Folk", 7),
            ("Blues", 7),
            ("Metal", 7),
            ("Soul", 7),
            ("Funk", 7),
        ),
    )
    assign_names([group])
    assert group.name.startswith("mixed · ")


def test_labels_two_artists_carry_do_not_name_a_cluster():
    # A share floor alone lets a small pool through: two mentions out of four is
    # half of it, and says nothing about the four hundred artists present.
    group = cluster(0, 400, (("Rock", 2), ("Pop", 2)))
    assign_names([group])
    assert group.name == "cluster 0"


def test_two_clusters_sharing_a_top_genre_are_still_named_apart():
    first = cluster(0, 100, (("Techno", 60), ("House", 40)))
    second = cluster(1, 100, (("Techno", 60), ("House", 40)))
    assign_names([first, second])
    assert first.name == "Techno"
    assert second.name == "Techno & House"
