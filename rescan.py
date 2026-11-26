# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
# SPDX-License-Identifier: AGPL-3.0-only
"""Plan file catalogue changes from a read snapshot, then apply them atomically."""

import os
import stat
from collections import Counter, defaultdict

import dupkey


def path_key(path):
    return os.path.normcase(os.path.abspath(path))


def within(path, root):
    return path == root or path.startswith(os.path.join(root, ""))


def skipped_directory(name, skip_dirs):
    return name.startswith(".") or name.lower() in skip_dirs


def covered(path, roots, skip_dirs):
    key = path_key(path)
    for root in roots:
        if within(key, root):
            relative = os.path.relpath(os.path.dirname(key), root)
            if relative == "." or not any(skipped_directory(part, skip_dirs)
                                          for part in relative.split(os.sep)):
                return True
    return False


def fingerprint(st):
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]


def directory_state(folder, db_path):
    identity = fingerprint(os.stat(folder))
    if path_key(os.path.realpath(folder)) != path_key(os.path.dirname(db_path)):
        return identity
    # Starting a SQLite writer can create WAL and journal files next to the DB.
    name = os.path.basename(db_path)
    artifacts = {name + suffix for suffix in ("", "-wal", "-shm", "-journal", ".backups")}
    return identity[:2] + [sorted(n for n in os.listdir(folder) if n not in artifacts)]


def revision(con):
    return con.execute("SELECT revision FROM db_state WHERE id=1").fetchone()[0]


def absent_under_walked_folder(path, walked):
    # An unvisited directory or a dangling symlink can look like a missing file.
    # Walk upward until an enumerated parent or an unverified existing path.
    current = path
    while True:
        try:
            os.lstat(current)
        except FileNotFoundError:
            parent = os.path.dirname(current)
            if path_key(parent) in walked:
                return True
            if parent != current:
                current = parent
                continue
        return False


def inventory(roots, known, exts, move_exts, min_size, skip_dirs, db_path):
    files, directories, errors = {}, {}, []
    failed_roots = set()
    backups = path_key(db_path + ".backups")
    for root in roots:
        root_key = path_key(root)

        def error(exc):
            failed_roots.add(root_key)
            errors.append(str(exc))

        for folder, subfolders, names in os.walk(root, onerror=error):
            subfolders[:] = [d for d in subfolders if not skipped_directory(d, skip_dirs)
                             and path_key(os.path.join(folder, d)) != backups]
            try:
                directories[folder] = directory_state(folder, db_path)
            except OSError as exc:
                error(exc)
                continue
            for name in names:
                path = os.path.join(folder, name)
                key = path_key(path)
                ext = os.path.splitext(name)[1].lower()
                if key not in known and ext not in move_exts:
                    continue
                try:
                    st = os.stat(path)
                except OSError as exc:
                    error(exc)
                    continue
                if stat.S_ISREG(st.st_mode):
                    files[key] = (path, st, ext in exts and st.st_size >= min_size)
    return files, directories, errors, failed_roots


def plan_changes(con, db_path, args, exts, skip_dirs, read_rows, sign):
    """Take one rule and DB revision for the whole preview, before walking files."""
    db_path = os.path.realpath(db_path)
    database_id = fingerprint(os.stat(db_path))[:2]
    roots = {path_key(path): os.path.abspath(path) for path in args.roots}
    con.execute("BEGIN")
    try:
        version = revision(con)
        by_sig = dupkey.sigs_complete(con)
        mode = con.execute("SELECT v FROM meta WHERE k='sig_mode'").fetchone()
        full = mode == ("full",)
        present = dupkey.available_where(con)
        rows = con.execute(f"SELECT id, path, ext, size, mtime, NOT ({present}), "
                           f"{dupkey.dup_key(by_sig=by_sig)} FROM photos").fetchall()
    finally:
        con.rollback()

    plan = {"format": 1, "database": db_path, "database_id": database_id, "revision": version,
            "rule": dupkey.rule_name(by_sig), "actions": [], "conflicts": [], "errors": []}
    known = {}
    for row in rows:
        if covered(row[1], roots, skip_dirs):
            key = path_key(row[1])
            if key in known:
                raise ValueError(f"Two catalogue records refer to the same path: {row[1]}")
            known[key] = row
    # Previously indexed formats remain eligible for moves/or restores even if
    # they are currently disabled for importing new files.
    move_exts = exts | {"." + r[2] for r in known.values() if r[2]}
    files, directories, errors, failed = inventory(
        roots.values(), known, exts, move_exts, max(0, args.min_kb) * 1024, skip_dirs, db_path)
    plan.update(directories=directories, errors=errors)
    complete = roots.keys() - failed
    walked = {path_key(folder) for folder in directories}

    actions = plan["actions"]
    absent, new, pending = [], {}, {}
    for key, entry in files.items():
        path, st, addable = entry
        if key in known:
            photo_id, old_path, _, size, mtime, missing, _ = known[key]
            if (size, mtime) != (st.st_size, st.st_mtime):
                pending[path] = ("update", photo_id, old_path, st)
            elif missing or path != old_path:
                pending[path] = ("restore" if missing else "rename", photo_id, old_path, st)
        elif covered(path, complete, skip_dirs):
            pending[path] = ("new", None, None, st)
            new[path] = addable

    for key, row in known.items():
        if key in files or not covered(row[1], complete, skip_dirs):
            continue
        try:
            if absent_under_walked_folder(row[1], walked):
                absent.append(row)
        except OSError as exc:
            errors.append(str(exc))

    prepared, unreadable = defaultdict(list), False
    readable = []
    for path, (_, _, _, st) in pending.items():
        try:
            with open(path, "rb") as reader:
                if fingerprint(os.fstat(reader.fileno())) != fingerprint(st):
                    raise OSError(f"File changed during preview: {path}")
                reader.read(1)
            readable.append((path, st))
        except OSError as exc:
            errors.append(str(exc))
            unreadable = True

    key_sql = (f"SELECT {dupkey.name_key('candidate')} FROM (SELECT ? AS filename, "
               "? AS size, ? AS taken, ? AS date_src) AS candidate")
    signatures = dict(sign(readable, full)) if by_sig else {}
    for row in read_rows(readable):
        path = row["path"]
        kind, photo_id, old_path, st = pending[path]
        sig = signatures.get(path)
        try:
            if (by_sig and sig is None) or fingerprint(os.stat(path)) != fingerprint(st):
                raise OSError(f"Could not verify file during preview: {path}")
        except OSError as exc:
            errors.append(str(exc))
            unreadable = True
            continue
        key = sig if by_sig else con.execute(key_sql, (row["filename"], row["size"],
                                                       row["taken"], row["date_src"])).fetchone()[0]
        if by_sig and kind in ("restore", "rename") and sig != known[path_key(old_path)][-1]:
            kind = "update"
        item = {"kind": kind, "id": photo_id, "old": old_path, "path": path,
                "fingerprint": fingerprint(st), "row": row, "sig": sig}
        if kind == "new":
            prepared[key].append(item)
        else:
            actions.append(item)

    # An unreadable candidate could have turned a 1:1 match into a conflict.
    if unreadable:
        errors.append("New paths and missing files were deferred because a file could not be read.")
        return plan

    sources = defaultdict(list)
    for row in absent:
        sources[row[-1]].append(row)
    for key, old in sources.items():
        candidates = prepared.pop(key, []) if key is not None else []
        if len(old) == len(candidates) == 1:
            item = candidates[0]
            item.update(kind="move", id=old[0][0], old=old[0][1])
            actions.append(item)
        elif candidates:
            plan["conflicts"].append({"old": [r[1] for r in old],
                                      "new": [item["path"] for item in candidates]})
        else:
            for photo_id, path, _, _, _, missing, _ in old:
                if not missing:
                    actions.append({"kind": "missing", "id": photo_id, "old": path})
    for candidates in prepared.values():
        for item in candidates:
            if new[item["path"]]:
                item["kind"] = "add"
                actions.append(item)
    actions.sort(key=lambda item: (item["kind"], path_key(item.get("path") or item["old"])))
    return plan


def summary(plan):
    counts = Counter(item["kind"] for item in plan["actions"])
    return ", ".join((f"{counts['add']} added", f"{counts['update']} updated",
                      f"{counts['move'] + counts['rename']} moved", f"{counts['restore']} restored",
                      f"{counts['missing']} missing", f"{len(plan['conflicts'])} conflicts",
                      f"{len(plan['errors'])} warnings"))


def apply_plan(con, plan, columns, file_results):
    """Refresh stale checks under the writer lock; any failure rolls back all rows."""
    with con:
        con.execute("BEGIN IMMEDIATE")
        if revision(con) != plan["revision"]:
            raise ValueError("The catalogue changed after preview. Run Rescan folders again.")
        for folder, expected in plan["directories"].items():
            if directory_state(folder, plan["database"]) != expected:
                raise ValueError(f"Folder changed after preview: {folder}")
        for item in plan["actions"]:
            kind = item["kind"]
            if kind in ("move", "missing"):
                try:
                    os.lstat(item["old"])
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError(f"File returned after preview: {item['old']}")
            if kind != "missing" and fingerprint(os.stat(item["path"])) != item["fingerprint"]:
                raise ValueError(f"File changed after preview: {item['path']}")

        insert = (f"INSERT INTO photos ({','.join(columns)},sig) "
                  f"VALUES ({','.join(':' + name for name in columns)},:sig)")
        assign = ",".join(f"{name}=:{name}" for name in columns)
        reset = ",".join(f"{name}=NULL" for name in file_results if name != "sig")
        for item in plan["actions"]:
            kind = item["kind"]
            if kind == "missing":
                con.execute("UPDATE photos SET missing=1 WHERE id=?", (item["id"],))
                continue
            values = dict(item["row"], id=item["id"], sig=item["sig"])
            if kind == "add":
                con.execute(insert, values)
            else:
                invalidate = kind == "update" or (kind == "move" and item["sig"] is None)
                derived = f", sig=:sig, {reset}" if invalidate else ""
                con.execute(f"UPDATE photos SET {assign}, missing=0{derived} WHERE id=:id", values)
