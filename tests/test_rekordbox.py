from pathlib import Path

from discolab.rekordbox import RekordboxReader, join_key


def test_join_key_folds_decomposed_accents():
    """macOS hands back decomposed accents and rekordbox composed ones.

    Without the fold the two spellings of one path never compare equal, and the
    join returns nothing while looking correct.
    """
    decomposed = "/Music/Cafe\u0301 - track.aiff"
    composed = "/Music/Caf\u00e9 - track.aiff"
    assert decomposed != composed
    assert join_key(decomposed) == join_key(composed)


def test_reader_says_why_when_there_is_no_database():
    reader = RekordboxReader(db_path=Path("/nonexistent/rekordbox/master.db"))
    reason = reader.unavailable_reason()
    assert reason
    assert "master.db" in reason or "sqlcipher" in reason
