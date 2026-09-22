# -*- coding: utf-8 -*-
# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License, version 3, as published by
# the Free Software Foundation. See LICENSE for the full text.
# Commercial licensing is available — see COMMERCIAL.md.
# SPDX-License-Identifier: AGPL-3.0-only
"""The single place the tag rule lives; app.py writes tags, scan.py only keeps
the table in step with the photos.

Tags are yours, like the `mark` rating: the scanner never writes one. With the
rating that had to be arranged by hand, by keeping `mark` out of `scan.py:COLUMNS`
— here it follows from the shape of the data. The scanner rewrites columns of
`photos` and knows nothing of this table, so there is no pass that could wipe
what you typed.
"""

import re
import unicodedata

# Three is the whole point of the limit: a photo with a dozen tags is a photo
# nobody will ever find again, and three fit on one line everywhere they are
# shown — in a list row, over a thumbnail and in the card.
MAX_TAGS = 3
MAX_LEN = 24              # longer than this and the chips wrap the layout apart

# Letters and digits of any alphabet, and nothing else — no spaces, no hyphens,
# no punctuation. `\w` would let the underscore through, so it is excluded by
# hand rather than by listing every alphabet that is allowed in.
VALID = re.compile(r"\A[^\W_]+\Z", re.UNICODE)

# The table is separate, so `photos` stays what it is: one row per file, with
# every column a dimension the panels group by. A tag is not that shape.
#
# The trigger is what keeps the two in step. The scanner drops rows for files
# that are gone from the disk (scan.py, "Dropped from the database"), and
# without the trigger their tags would sit here forever, counted by a panel that
# can no longer show a single photo behind the number. A foreign key would do the
# same job only if every connection in both programs remembered to switch
# `PRAGMA foreign_keys` on, which is off by default and per connection; the
# trigger is stored in the database and cannot be forgotten.
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS photo_tags (
           photo_id INTEGER NOT NULL,
           tag      TEXT NOT NULL,
           PRIMARY KEY (photo_id, tag)
       )""",
    "CREATE INDEX IF NOT EXISTS ix_tag ON photo_tags(tag, photo_id)",
    """CREATE TRIGGER IF NOT EXISTS photo_tags_gone
       AFTER DELETE ON photos BEGIN
           DELETE FROM photo_tags WHERE photo_id = OLD.id;
       END""",
]


def ensure_schema(con):
    """Both programs call this: either of them may meet a database that has never
    had a tag in it, and the trigger has to be there before the scanner deletes
    its first row."""
    for stmt in SCHEMA:
        con.execute(stmt)
    con.commit()


def normalize(raw):
    """One tag as it is stored, or None if it is not a tag at all.

    Two things happen here that must not happen anywhere else. Case is folded in
    Python, with casefold rather than lower — SQLite's own `lower()` folds ASCII
    and leaves every other alphabet alone, which is the same trap `dupkey` walked
    into over Cyrillic file names. Because the stored form is already folded,
    every comparison in SQL is a plain `=` and the trap does not arise.

    The other is NFC. "й" is either one code point or "и" with a combining breve
    after it; the two look identical and are different strings, so without
    composing them one tag would quietly become two.
    """
    s = unicodedata.normalize("NFC", (raw or "").strip().casefold())
    if not s or len(s) > MAX_LEN or not VALID.match(s):
        return None
    return s


def prefix(raw):
    """What the suggestion list searches for.

    The same folding as a tag, but characters a tag may not hold are dropped
    instead of refusing the lot: the list has to keep up while a word is being
    typed, not go blank because a hyphen was hit by mistake. It also means no
    `%` or `_` can reach the LIKE the suggestions are found with.
    """
    s = unicodedata.normalize("NFC", (raw or "").strip().casefold())
    return "".join(c for c in s if VALID.match(c))[:MAX_LEN]


def rule_text():
    """How the rule is worded for whoever typed something that is not a tag."""
    return (f"A tag is letters and digits only, no more than {MAX_LEN} characters"
            f"; up to {MAX_TAGS} per photo")
