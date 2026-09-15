# -*- coding: utf-8 -*-
# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License, version 3, as published by
# the Free Software Foundation. See LICENSE for the full text.
# Commercial licensing is available — see COMMERCIAL.md.
# SPDX-License-Identifier: AGPL-3.0-only
"""One window that sets the program up and runs it.

Deliberately built on the standard library alone. The window has to open before
anything is installed — that is the whole point of it, so that a first-time user
sees the setup happening instead of a silent console. Flask and Pillow go into
.venv and are used only by the scripts this launcher starts as separate
processes.

Tasks run as child processes, which is what makes them genuinely parallel: you
can analyse images while the server serves the interface. The database is set up
for that (WAL plus a busy timeout); without it roughly half the server's queries
would fail while a scan is running.
"""

import json
import os
import sqlite3
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(ROOT, "settings.json")
VENV = os.path.join(ROOT, ".venv")
WINDOWS = sys.platform == "win32"
PORT = 5577

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:          # проверяем при запуске, а не при импорте, —
    tk = None                # иначе модуль нельзя было бы протестировать


def nf(n):
    return f"{n:,}".replace(",", "\u2009")      # тонкий пробел между тысячами


def venv_python():
    return os.path.join(VENV, "Scripts", "python.exe") if WINDOWS \
        else os.path.join(VENV, "bin", "python")


def spawn(args, cwd=ROOT):
    """Child process with its output on one pipe.

    -u matters: without it Python buffers stdout when it is not a terminal, and
    the log would stay empty until the task finished.
    """
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0
    return subprocess.Popen(
        [args[0], "-u"] + args[1:], cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        bufsize=0, creationflags=flags)


def interrupt(proc):
    """Ask a task to stop the way Ctrl+C would.

    Killing it outright would throw away the batch it has not committed yet;
    every pass here saves what it has done when interrupted.
    """
    try:
        if WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
    except Exception:
        proc.terminate()


PROGRESS = re.compile(r"(\d+)\s*/\s*(\d+)\s*\(\s*([\d.]+)%\)")
LEFT = re.compile(r"about\s+(\d+)\s+min left")


class Task:
    """A numbered button with a progress bar, an explanation and the process.

    The explanation sits under the button rather than in a tooltip: someone who
    does not know what to press will not hover over things to find out.
    """

    def __init__(self, app, key, label, note, args_fn, parent, writes=True):
        self.app, self.key, self.args_fn = app, key, args_fn
        self.proc = None
        self.label = label
        self.writes = writes         # проходу нужна база на запись?

        box = ttk.Frame(parent)
        box.pack(fill="x", padx=14, pady=(0, 10))
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.button = ttk.Button(row, text=label, width=22, command=self.toggle)
        self.button.pack(side="left")
        self.bar = ttk.Progressbar(row, mode="determinate", maximum=100)
        self.bar.pack(side="left", fill="x", expand=True, padx=10)
        self.state = ttk.Label(row, text="", width=20, anchor="e")
        self.state.pack(side="left")
        self.note = ttk.Label(box, text=note, foreground="#8B8598",
                              wraplength=680, justify="left")
        self.note.pack(fill="x", padx=(2, 0), pady=(3, 0))

    def running(self):
        return self.proc and self.proc.poll() is None

    def toggle(self):
        if self.running():
            self.state.config(text="stopping…")
            interrupt(self.proc)
            return
        # Занятой считается только запись. Проверка каталога базу не трогает,
        # поэтому её ни ждать, ни откладывать не нужно — как и сервер.
        if self.writes and self.app.busy_task() is not None:
            messagebox.showinfo(
                "One at a time",
                "Another pass is already writing to the database. Let it finish "
                "or stop it first — the server can keep running meanwhile.")
            return
        args = self.args_fn()
        if args is None:
            return
        self.bar["value"] = 0
        self.state.config(text="starting…")
        self.button.config(text="Stop")
        self.app.log(f"\n$ {' '.join(args[1:])}\n")
        self.proc = spawn(args)
        threading.Thread(target=self.pump, daemon=True).start()

    def pump(self):
        buf = ""
        while True:
            ch = self.proc.stdout.read(1)
            if not ch:
                break
            if ch in "\r\n":
                if buf.strip():
                    self.app.out.put(("line", buf))
                    self.app.out.put(("progress", (self, buf)))
                buf = ""
            else:
                buf += ch
        code = self.proc.wait()
        self.app.out.put(("done", (self, code)))

    def finished(self, code):
        self.button.config(text=self.label)
        if code == 0:
            self.bar["value"] = 100
            self.state.config(text="done")
        else:
            self.state.config(text="stopped" if code else "failed")

    def progress(self, line):
        m = PROGRESS.search(line)
        if not m:
            return
        self.bar["value"] = float(m.group(3))
        left = LEFT.search(line)
        self.state.config(text=f"{m.group(3)}%" +
                          (f" · {left.group(1)} min" if left else ""))


# Иконку держим ссылкой на объекте окна. PhotoImage живёт, пока на него кто-то
# ссылается, а Tk ссылкой не считается: собранная сборщиком картинка оставляет
# окно с пустым значком и без единой ошибки. На Windows берём ico — только он
# доходит до панели задач, — в остальном png.
def set_window_icon(root):
    ico = os.path.join(ROOT, "web", "favicon.ico")
    png = os.path.join(ROOT, "web", "icon.png")
    try:
        if WINDOWS and os.path.isfile(ico):
            root.iconbitmap(default=ico)
        elif os.path.isfile(png):
            root._icon = tk.PhotoImage(file=png)
            root.iconphoto(True, root._icon)
    except tk.TclError:          # окно останется со стандартным значком
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.out = queue.Queue()
        self.server = None
        self.ready = False
        root.title("photostats")
        set_window_icon(root)
        root.minsize(760, 760)       # четыре прохода + лог; ниже лог схлопывается

        pad = {"padx": 14, "pady": (0, 8)}
        head = ttk.Frame(root)
        head.pack(fill="x", padx=14, pady=(12, 10))
        ttk.Label(head, text="photostats", font=("Segoe UI", 11, "bold")).pack(side="left")
        self.setup_state = ttk.Label(head, text="checking the environment…")
        self.setup_state.pack(side="right")

        # Главная подсказка. Описания у кнопок объясняют, что каждая делает, но
        # растерянность вызывает другой вопрос — что нажимать сейчас. На него
        # отвечает состояние базы, а не текст в инструкции.
        self.next_step = ttk.Label(root, text="", foreground="#E39B33",
                                   wraplength=680, justify="left")
        self.next_step.pack(fill="x", padx=14, pady=(0, 12))

        ttk.Label(root, text="1 · Photo folders").pack(anchor="w", padx=14)
        box = ttk.Frame(root)
        box.pack(fill="both", padx=14, pady=(4, 6))
        self.folders = tk.Listbox(box, height=4, activestyle="none")
        self.folders.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(box, command=self.folders.yview)
        bar.pack(side="right", fill="y")
        self.folders.config(yscrollcommand=bar.set)

        line = ttk.Frame(root)
        line.pack(fill="x", **pad)
        ttk.Button(line, text="Add folder", command=self.add_folder).pack(side="left")
        ttk.Button(line, text="Remove", command=self.remove_folder).pack(side="left", padx=6)
        self.raw = tk.BooleanVar()
        ttk.Checkbutton(line, text="include RAW and DNG", variable=self.raw,
                        command=self.save).pack(side="right")
        ttk.Label(root, foreground="#8B8598", wraplength=680, justify="left",
                  text="Sub-folders are included, so the top of your archive is "
                       "usually enough. RAW files normally sit beside a JPEG of "
                       "the same shot, so leaving that box off avoids counting "
                       "those frames twice.").pack(fill="x", padx=14, pady=(0, 14))

        self.tasks = {}
        for key, label, note, fn in (
            ("exif", "2 · Read EXIF",
             "Reads dates, camera, lens and settings from the files. Fast, and "
             "enough on its own — everything except colour works after this.",
             self.args_exif),
            ("images", "3 · Analyse images",
             "Opens every photo to work out its colour, brightness and contrast. "
             "Slow: hours on a large archive. You can stop it and carry on later.",
             lambda: [venv_python(), "scan.py", "--colors-only"]),
            ("sign", "Sign files",
             "Optional. Reads a little of every file so identical copies can be "
             "told apart for certain. Only needed for exact duplicate matching.",
             lambda: [venv_python(), "scan.py", "--hash-only"]),
            ("check", "Check a folder",
             "Asks of a folder you have not scanned yet: how much of it is "
             "already in the archive? Counts copies and new files and says so at "
             "the end. Reads the database, never writes to it.",
             self.args_check),
        ):
            self.tasks[key] = Task(self, key, label, note, fn, root,
                                   writes=(key != "check"))

        ttk.Separator(root).pack(fill="x", padx=14, pady=(4, 10))
        srv = ttk.Frame(root)
        srv.pack(fill="x", **pad)
        self.open_btn = ttk.Button(srv, text="4 · Open interface", command=self.open_ui)
        self.open_btn.pack(side="left")
        self.srv_state = ttk.Label(srv, text="server stopped")
        self.srv_state.pack(side="left", padx=10)
        self.stop_btn = ttk.Button(srv, text="Stop server", command=self.stop_server,
                                   state="disabled")
        self.stop_btn.pack(side="right")
        ttk.Label(root, foreground="#8B8598", wraplength=680, justify="left",
                  text="Opens the statistics in your browser. Nothing leaves this "
                       "computer. It can stay open while a pass is running — the "
                       "numbers fill in as the scan goes.").pack(
            fill="x", padx=14, pady=(3, 10))

        self.text = tk.Text(root, height=10, wrap="none", state="disabled",
                            font=("Consolas", 9))
        self.text.pack(fill="both", expand=True, padx=14, pady=(6, 14))

        self.load()
        self.set_ready(False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(60, self.drain)
        root.after(200, self.tick)
        threading.Thread(target=self.prepare, daemon=True).start()

    # ---------- настройки ----------

    def load(self):
        try:
            data = json.load(open(SETTINGS, encoding="utf-8"))
        except (OSError, ValueError):
            return
        for f in data.get("folders", []):
            self.folders.insert("end", f)
        self.raw.set(bool(data.get("raw")))

    def save(self):
        try:
            json.dump({"folders": list(self.folders.get(0, "end")),
                       "raw": self.raw.get()},
                      open(SETTINGS, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        except OSError:
            pass

    def add_folder(self):
        d = filedialog.askdirectory(title="Folder with photos")
        if d:
            self.folders.insert("end", os.path.normpath(d))
            self.save()
            self.refresh_hint()

    def remove_folder(self):
        for i in reversed(self.folders.curselection()):
            self.folders.delete(i)
        self.save()

    # ---------- что делать дальше ----------

    def db_state(self):
        """Что уже сделано, по самой базе. Читаем напрямую: sqlite3 входит в
        стандартную библиотеку, окружение для этого не нужно."""
        path = os.path.join(ROOT, "photos.db")
        if not os.path.exists(path):
            return {"total": 0}
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
            con.execute("PRAGMA busy_timeout = 3000")
            try:
                row = con.execute(
                    "SELECT COUNT(*), SUM(color IS NULL), SUM(sig IS NULL) FROM photos"
                ).fetchone()
                out = {"total": row[0], "no_images": row[1] or 0,
                       "no_sig": row[2] or 0}
            except sqlite3.OperationalError:
                # база от прежней версии, без колонок разбора: их допишет
                # первый же запуск сканера или сервера
                total, = con.execute("SELECT COUNT(*) FROM photos").fetchone()
                out = {"total": total, "no_images": total, "no_sig": total}
            con.close()
            return out
        except sqlite3.Error:
            return None          # занята записью — оставим прежнюю подсказку

    def refresh_hint(self):
        st = self.db_state()
        if st is None:
            return
        has_folders = self.folders.size() > 0
        total = st.get("total", 0)

        if not self.ready:
            text = "Setting things up. The buttons wake up when it is done."
        elif not has_folders and not total:
            text = ("Start here: add the folder your photos are in, then press "
                    "Read EXIF.")
        elif not total:
            text = "Nothing scanned yet — press Read EXIF."
        elif st["no_images"] == total:
            text = (f"{nf(total)} photos are in. Press Open interface to look at "
                    f"them. Colour and light stay empty until you run Analyse "
                    f"images, which takes a while.")
        elif st["no_images"]:
            text = (f"{nf(total)} photos, {nf(st['no_images'])} still without "
                    f"image analysis. Everything works meanwhile; run Analyse "
                    f"images again to carry on.")
        else:
            text = f"{nf(total)} photos, everything analysed. Open interface."
        self.next_step.config(text=text)

        for key, left in (("images", st.get("no_images")), ("sign", st.get("no_sig"))):
            task = self.tasks[key]
            if task.running() or not total:
                continue
            task.state.config(text="done" if not left else f"{nf(left)} left")
            task.bar["value"] = 0 if not total else 100 * (total - left) / total

    # ---------- окружение ----------

    def prepare(self):
        """Создаёт .venv и ставит библиотеки, если их ещё нет."""
        py = venv_python()
        if os.path.exists(py) and self.deps_ok(py):
            self.out.put(("ready", "environment ready"))
            return
        if not os.path.exists(py):
            self.out.put(("line", "Creating .venv…"))
            if self.run_quiet([sys.executable, "-m", "venv", VENV]) != 0:
                self.out.put(("ready", "could not create .venv"))
                return
        self.out.put(("line", "Installing libraries, this takes a minute…"))
        code = self.run_quiet([py, "-m", "pip", "install", "-r",
                               os.path.join(ROOT, "requirements.txt")])
        if code == 0 and self.deps_ok(py):
            self.out.put(("ready", "environment ready"))
        else:
            self.out.put(("ready", "install failed — see the log"))

    def deps_ok(self, py):
        try:
            return subprocess.run([py, "-c", "import flask, PIL"],
                                  capture_output=True).returncode == 0
        except OSError:
            return False

    def run_quiet(self, args):
        proc = spawn(args) if args[0].endswith(("python", "python.exe")) else \
            subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace")
        for line in proc.stdout:
            self.out.put(("line", line.rstrip()))
        return proc.wait()

    def set_ready(self, ready, note=None):
        self.ready = ready
        state = "normal" if ready else "disabled"
        for t in self.tasks.values():
            t.button.config(state=state)
        self.open_btn.config(state=state)
        if note:
            self.setup_state.config(text=note)

    # ---------- задачи ----------

    def busy_task(self):
        """Проход, который сейчас держит базу на запись. Читающие не в счёт."""
        for t in self.tasks.values():
            if t.writes and t.running():
                return t
        return None

    def args_exif(self):
        folders = list(self.folders.get(0, "end"))
        if not folders:
            messagebox.showinfo("No folders", "Add at least one folder with photos.")
            return None
        args = [venv_python(), "scan.py"] + folders
        if self.raw.get():
            args.append("--raw")
        return args

    def args_check(self):
        """Спрашивает папку отдельно, а не берёт список сверху.

        Проверяют как раз то, чего в списке ещё нет: карту из фотоаппарата,
        чужой диск, старую копию архива. Выбранная папка в настройках не
        сохраняется — иначе она попала бы в следующий проход EXIF, а это ровно
        то, от чего проверка и должна уберечь.
        """
        if not os.path.exists(os.path.join(ROOT, "photos.db")):
            messagebox.showinfo(
                "Nothing to compare with",
                "The archive is empty. Read EXIF from your own folders first — "
                "then a folder can be checked against them.")
            return None
        d = filedialog.askdirectory(title="Folder to check against the archive")
        if not d:
            return None
        args = [venv_python(), "scan.py", "--check-dups", os.path.normpath(d)]
        if self.raw.get():
            args.append("--raw")
        return args

    # ---------- сервер ----------

    def open_ui(self):
        if not (self.server and self.server.poll() is None):
            self.log("\n$ app.py\n")
            self.server = spawn([venv_python(), "app.py", "--no-browser",
                                 "--port", str(PORT)])
            threading.Thread(target=self.pump_server, daemon=True).start()
            self.srv_state.config(text="server starting…")
            self.stop_btn.config(state="normal")
            threading.Thread(target=self.wait_and_open, daemon=True).start()
        else:
            webbrowser.open(f"http://127.0.0.1:{PORT}/")

    def wait_and_open(self):
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2)
                self.out.put(("server", f"server running · 127.0.0.1:{PORT}"))
                webbrowser.open(f"http://127.0.0.1:{PORT}/")
                return
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)
        self.out.put(("server", "server did not answer — see the log"))

    def pump_server(self):
        for line in self.server.stdout:
            self.out.put(("line", line.rstrip()))
        self.out.put(("server", "server stopped"))

    def stop_server(self):
        if self.server and self.server.poll() is None:
            interrupt(self.server)
        self.stop_btn.config(state="disabled")

    # ---------- вывод ----------

    def log(self, text):
        self.text.config(state="normal")
        self.text.insert("end", text if text.endswith("\n") else text + "\n")
        self.text.see("end")
        if float(self.text.index("end-1c").split(".")[0]) > 400:
            self.text.delete("1.0", "200.0")
        self.text.config(state="disabled")

    def tick(self):
        """База меняется под нами, пока идёт проход, — подсказка это отражает."""
        try:
            self.refresh_hint()
        except Exception:
            pass
        self.root.after(4000, self.tick)

    def drain(self):
        try:
            while True:
                kind, payload = self.out.get_nowait()
                if kind == "line":
                    self.log(payload)
                elif kind == "progress":
                    task, line = payload
                    task.progress(line)
                elif kind == "done":
                    task, code = payload
                    task.finished(code)
                    self.refresh_hint()
                elif kind == "ready":
                    self.set_ready(payload == "environment ready", payload)
                    self.refresh_hint()
                elif kind == "server":
                    self.srv_state.config(text=payload)
                    if "stopped" in payload or "did not" in payload:
                        self.stop_btn.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(60, self.drain)

    # ---------- закрытие ----------

    def on_close(self):
        busy = self.busy_task()
        alive = self.server and self.server.poll() is None
        if busy or alive:
            what = busy.label.lower() if busy else "the server"
            if not messagebox.askyesno(
                    "Still running",
                    f"{what.capitalize()} is still running. Stop it and quit?\n\n"
                    "Whatever a pass has already done is saved, and it will "
                    "carry on from there next time."):
                return
            for t in self.tasks.values():
                if t.running():
                    interrupt(t.proc)
            if alive:
                interrupt(self.server)
            for t in list(self.tasks.values()) + []:
                if t.proc:
                    try:
                        t.proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        t.proc.kill()
            if alive:
                try:
                    self.server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.server.kill()
        self.save()
        self.root.destroy()


def main():
    if tk is None:
        print("Tkinter is missing, so the window cannot open.\n"
              "Re-run the Python installer and tick \"tcl/tk and IDLE\".\n"
              "On Linux: install the python3-tk package.")
        input("Press Enter to close. ")
        return 1
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if WINDOWS else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main() or 0)
