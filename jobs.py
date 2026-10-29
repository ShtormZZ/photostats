# -*- coding: utf-8 -*-
# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License, version 3, as published by
# the Free Software Foundation. See LICENSE for the full text.
# Commercial licensing is available — see COMMERCIAL.md.
# SPDX-License-Identifier: AGPL-3.0-only
"""What the scanner is doing, told through the database; scan.py writes it,
app.py reads it for the status line at the top of the page.

Through the database because it is the one thing both programs already share.
The scanner may be started by the launcher, from a terminal, or on a schedule,
and the server has no way of knowing which — but whichever it is, it writes to
the same file the server reads.

A pass that ends cleanly says so. A pass that does not — the launcher's Stop
button ends the process outright (see `scan.py:stop_on_break()`), a crash, a
closed terminal — cannot say anything, so a pass also writes a heartbeat every
few seconds from a thread of its own, and a "running" row whose heartbeat has
gone quiet is read as stopped. The heartbeat is a thread rather than a write in
the progress loop because the loop can go quiet for minutes on end while still
working: walking a large archive on a network drive, or a hundred RAW files in
one batch.
"""

import sqlite3
import threading
import time

HEARTBEAT = 3        # seconds between the writes of a running pass
# Silence after which "running" is taken to mean stopped. Several heartbeats,
# because one can be held up for a while waiting on the pass's own write lock.
STALE = 20
KEEP = 50            # rows kept; this is a status line, not a log

KINDS = {"exif": "Reading EXIF", "images": "Analysing images",
         "sign": "Signing files"}

SCHEMA = """CREATE TABLE IF NOT EXISTS jobs (
    id       INTEGER PRIMARY KEY,
    kind     TEXT NOT NULL,      -- a key of KINDS
    state    TEXT NOT NULL,      -- running | done | stopped | failed
    phase    TEXT,               -- what a running pass is busy with, in words
    done     INTEGER NOT NULL DEFAULT 0,
    total    INTEGER,            -- NULL while it is not known yet
    started  REAL NOT NULL,      -- Unix time, so the server can measure silence
    beat     REAL NOT NULL,
    finished REAL,
    note     TEXT                -- the closing word: an error, "nothing to do"
)"""


def ensure_schema(con):
    """Both programs call this, as with tags: either may meet a database that
    predates the table, and the server has to be able to read an empty one."""
    con.execute(SCHEMA)
    con.commit()


def latest(con):
    """The row the status line shows: the last pass started, with a running
    one that has gone silent reported as stopped. None if nothing has ever run."""
    row = con.execute("SELECT id, kind, state, phase, done, total, started, beat,"
                      " finished, note FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    job = dict(zip(("id", "kind", "state", "phase", "done", "total", "started",
                    "beat", "finished", "note"), row))
    now = time.time()
    if job["state"] == "running" and now - job["beat"] > STALE:
        job["state"] = "stopped"
        job["finished"] = job["beat"]
    job["label"] = KINDS.get(job["kind"], job["kind"])
    job["now"] = now
    return job


class Job:
    """One pass, as the status line sees it. Used as a context manager:

        with Job(db_path, "images") as job:
            job.total = len(todo)
            ...
            job.done += 1

    The pass only sets attributes; the heartbeat thread writes them, so the
    loop doing the work never waits on a write of its own status. Leaving the
    block writes the end state — done, stopped on Ctrl+C, failed on anything
    else — unless the pass already settled it with `stop()`.
    """

    def __init__(self, db_path, kind, phase=None):
        self.kind = kind
        self.phase = phase
        self.done = 0
        self.total = None
        self.state = "running"
        self.note = None
        self._path = db_path
        self._halt = threading.Event()
        self._lock = threading.Lock()

    def __enter__(self):
        # A connection of its own: the pass's connection belongs to its thread,
        # and its transactions are its own business.
        self._con = sqlite3.connect(self._path, timeout=15,
                                    check_same_thread=False)
        self._con.execute("PRAGMA busy_timeout = 15000")
        # On a first run this connection creates the file, before the scanner's
        # own does — and WAL, which lets the server read while the pass writes,
        # is switched on by whoever gets there first. Unavailable on network
        # drives, where the ordinary journal stays and the lock wait copes.
        try:
            self._con.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error:
            pass
        ensure_schema(self._con)
        now = time.time()
        with self._lock:
            self.id = self._con.execute(
                "INSERT INTO jobs (kind, state, phase, started, beat)"
                " VALUES (?, 'running', ?, ?, ?)",
                (self.kind, self.phase, now, now)).lastrowid
            self._con.execute("DELETE FROM jobs WHERE id <= ?", (self.id - KEEP,))
            self._con.commit()
        self._thread = threading.Thread(target=self._beat, daemon=True)
        self._thread.start()
        return self

    def stop(self, note=None):
        """A pass that catches Ctrl+C itself and carries on to its summary says
        here that it was stopped, rather than letting it read as finished."""
        self.state = "stopped"
        self.note = note

    def _write(self, final=False):
        now = time.time()
        with self._lock:
            try:
                self._con.execute(
                    "UPDATE jobs SET state = ?, phase = ?, done = ?, total = ?,"
                    " beat = ?, finished = ?, note = ? WHERE id = ?",
                    (self.state if final else "running", self.phase, self.done,
                     self.total, now, now if final else None, self.note, self.id))
                self._con.commit()
            except sqlite3.Error:
                # The status line is not worth stopping a pass over. A missed
                # beat is the next one's job; a missed final write reads as
                # stopped once the silence runs out, which is nearly true.
                pass

    def _beat(self):
        while not self._halt.wait(HEARTBEAT):
            self._write()

    def __exit__(self, kind, exc, tb):
        self._halt.set()
        self._thread.join(timeout=HEARTBEAT + 1)
        if kind is not None:
            if issubclass(kind, KeyboardInterrupt):
                self.state = "stopped"
            else:
                self.state, self.note = "failed", f"{kind.__name__}: {exc}"
        elif self.state == "running":
            self.state = "done"
        self._write(final=True)
        self._con.close()
        return False
