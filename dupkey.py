# -*- coding: utf-8 -*-
# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License, version 3, as published by
# the Free Software Foundation. See LICENSE for the full text.
# Commercial licensing is available — see COMMERCIAL.md.
# SPDX-License-Identifier: AGPL-3.0-only
"""The single place where the rule "this is a copy" lives; app.py and scan.py read it.

One rule per program, deliberately. The duplicates panel in the interface and the
folder check in the launcher window have to answer the same question the same way:
if the check says "you already have this file" and the interface then does not count
it as a copy, neither answer can be trusted.
"""

import sqlite3

# What identifies a copy of a file. Name and size are not enough: two different
# frames can easily match on both — one camera repeats file names, and the size
# of a JPEG is dictated by the subject and lands on the same byte count more
# often than you would expect. So the key also takes in the exact capture time:
# two different frames never share the same second, while copies of one file
# always do.
#
# Only a real time counts, the one from EXIF. Where there is none the file date
# stands in — a copy has its own, and real copies would stop being found.
#
# If the `scan.py --hash-only` pass has computed content signatures for every
# file, the signature becomes the key: it answers the question outright and finds
# copies even under other names. While some files still lack one, signatures are
# not used at all — mixing two rules in one key is not allowed, since a signed
# copy would be unable to find an unsigned one.


def name_key(alias=""):
    """Key by name, size and capture time. The index ix_dupkey is built from this
    same text — without it, counting the size of a group walked the whole table for
    every row shown, and the selection list on an archive of forty thousand frames
    took forty seconds to build. Change this expression only together with the
    index."""
    p = alias + "." if alias else ""
    return (f"lower({p}filename) || '|' || {p}size || '|' || "
            f"CASE WHEN {p}date_src = 'exif' THEN {p}taken ELSE '' END")


def dup_key(alias="", by_sig=False):
    """The key expression for a query. by_sig is the result of sigs_complete()."""
    if by_sig:
        return (alias + "." if alias else "") + "sig"
    return name_key(alias)


def available_where(con):
    """Compatibility with catalogues read without running migrations."""
    return ("missing = 0" if any(row[1] == "missing" for row in
                                 con.execute("PRAGMA table_info(photos)")) else "1")


def sigs_complete(con, visible_only=False):
    """Does every photo have a signature?

    A database from an earlier version does not know the sig column yet. That is
    not an error and no reason to fall over: it means the signature pass was never
    run, and the name stays the key.

    The interface checks visible photos; folder checks include physical RAW
    companions too. An unsigned hidden file must not change the JPG duplicate rule.
    """
    table = "visible_photos" if visible_only else "photos"
    try:
        present = available_where(con)
        any_files, unsigned = con.execute(
            f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {present}), "
            f"EXISTS (SELECT 1 FROM {table} WHERE {present} AND sig IS NULL)").fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(any_files and not unsigned)


def rule_name(by_sig):
    """What the rule is called in the output — the same words the interface uses."""
    return "content signature" if by_sig else "name, size and capture time"
