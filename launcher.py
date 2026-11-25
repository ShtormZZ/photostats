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
import ipaddress
import os
import socket
import sqlite3
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import dupkey
import rescan

ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(ROOT, "settings.json")
VENV = os.path.join(ROOT, ".venv")
WINDOWS = sys.platform == "win32"
PORT = 5577

# The web interface uses this palette in web/index.html.
GROUND = "#16151A"
PANEL = "#1E1D24"
FRAME = "#2C2A34"
LINE = "#3A3745"
PAPER = "#EDE8DF"
MUTED = "#8B8598"
ACCENT = "#E39B33"
WARNING = "#F08A82"
TEXT_SURFACE = {
    "background": PANEL, "foreground": PAPER,
    "selectbackground": ACCENT, "selectforeground": GROUND,
    "borderwidth": 0, "highlightthickness": 1,
    "highlightbackground": LINE, "highlightcolor": ACCENT,
}

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:          # checked at startup rather than on import, or
    tk = None                # the module could not be tested at all


def nf(n):
    return f"{n:,}".replace(",", "\u2009")      # thin space between thousands


def venv_python():
    return os.path.join(VENV, "Scripts", "python.exe") if WINDOWS \
        else os.path.join(VENV, "bin", "python")


def lan_address():
    """This computer's address on the home network, or None.

    Connecting a UDP socket sends nothing — it only asks the system which of
    its addresses it would route that destination through, which is the one a
    tablet on the same Wi-Fi can reach. Asking by host name instead tends to
    come back with a Hyper-V or VPN adapter that nothing else can see.

    Only a private address is accepted. A machine wired straight to the
    internet has a public one, and listening on it would publish the archive
    to the world rather than to the sofa.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        addr = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    ip = ipaddress.ip_address(addr)
    return addr if ip.is_private and not ip.is_loopback else None


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


def configure_theme(root):
    """Use Tk's colourable theme for the launcher and its preview windows."""
    root.configure(background=GROUND)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=GROUND, foreground=PAPER, focuscolor=ACCENT,
                    bordercolor=LINE, lightcolor=FRAME, darkcolor=FRAME, troughcolor=PANEL)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Accent.TLabel", foreground=ACCENT)
    style.configure("Warning.TLabel", foreground=WARNING)
    style.configure("Brand.TLabel", font=("Segoe UI", 16, "bold"))
    style.configure("Accent.Brand.TLabel", foreground=ACCENT)

    style.configure("TButton", background=PANEL, padding=(10, 3))
    style.map("TButton", background=[("disabled", PANEL), ("pressed", GROUND), ("active", FRAME)],
              foreground=[("disabled", MUTED)], bordercolor=[("focus", ACCENT), ("active", ACCENT)])
    style.configure("Accent.TButton", foreground=ACCENT)
    style.configure("Primary.TButton", background=ACCENT, foreground=GROUND,
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT, focuscolor=GROUND)
    style.map("Primary.TButton", background=[("disabled", FRAME), ("pressed", "#D58D27"),
                                             ("active", "#F2AD49")],
              bordercolor=[("disabled", LINE), ("focus", PAPER)],
              lightcolor=[("disabled", FRAME)], darkcolor=[("disabled", FRAME)])

    style.configure("TCheckbutton", indicatorbackground=PANEL, indicatorforeground=GROUND,
                    upperbordercolor=LINE, lowerbordercolor=LINE)
    style.map("TCheckbutton", indicatorbackground=[("disabled", FRAME), ("selected", ACCENT)],
              foreground=[("disabled", MUTED), ("active", ACCENT)],
              background=[("active", GROUND)])
    style.configure("TProgressbar", background=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                    thickness=10)
    style.configure("TScrollbar", background=FRAME, arrowcolor=PAPER)
    style.map("TScrollbar", background=[("active", LINE)])
    style.configure("TSeparator", background=LINE)


class Task:
    """A numbered button with a progress bar, an explanation and the process.

    The explanation sits under the button rather than in a tooltip: someone who
    does not know what to press will not hover over things to find out.
    """

    def __init__(self, app, key, label, note, args_fn, parent, writes=True):
        self.app, self.key, self.args_fn = app, key, args_fn
        self.proc = None
        self.stop_requested = False
        self.label = label
        self.writes = writes         # does this pass need the database for writing?

        box = ttk.Frame(parent)
        box.pack(fill="x", padx=14, pady=(0, 7))
        row = ttk.Frame(box)
        row.pack(fill="x")
        button_style = "Accent.TButton" if key in ("exif", "rescan") else "TButton"
        self.button = ttk.Button(row, text=label, width=22, command=self.toggle, style=button_style)
        self.button.pack(side="left")
        self.bar = ttk.Progressbar(row, mode="determinate", maximum=100)
        self.bar.pack(side="left", fill="x", expand=True, padx=10)
        self.state = ttk.Label(row, text="", width=20, anchor="e", style="Muted.TLabel")
        self.state.pack(side="left")
        self.note = ttk.Label(box, text=note, style="Muted.TLabel",
                              wraplength=680, justify="left")
        self.note.pack(fill="x", padx=(2, 0), pady=(2, 0))

    def running(self):
        return self.proc and self.proc.poll() is None

    def toggle(self):
        if self.running():
            self.stop_requested = True
            self.state.config(text="stopping…")
            interrupt(self.proc)
            return
        # Only writing counts as busy. The folder check does not touch the
        # database, so it neither waits nor is put off — no more than the server is.
        if self.writes and self.app.busy_task() is not None:
            messagebox.showinfo(
                "One at a time",
                "Another pass is already writing to the database. Let it finish "
                "or stop it first — the server can keep running meanwhile.")
            return
        args = self.args_fn()
        if args is None:
            return
        self.start(args)

    def start(self, args):
        self.stop_requested = False
        self.bar["value"] = 0
        self.state.config(text="starting…", style="Accent.TLabel")
        self.button.config(text="Stop")
        self.app.log(f"\n$ {' '.join(args[1:])}\n")
        try:
            self.proc = spawn(args)
        except OSError as exc:
            self.app.log(f"Could not start {self.label}: {exc}\n")
            self.app.out.put(("done", (self, 1)))
            return
        threading.Thread(target=self.pump, args=(self.proc,), daemon=True).start()

    def pump(self, proc):
        buf = ""
        while True:
            ch = proc.stdout.read(1)
            if not ch:
                break
            if ch in "\r\n":
                if buf.strip():
                    self.app.out.put(("line", buf))
                    self.app.out.put(("progress", (self, buf)))
                buf = ""
            else:
                buf += ch
        code = proc.wait()
        self.app.out.put(("done", (self, code)))

    def finished(self, code):
        self.button.config(text=self.label)
        if code == 0:
            self.bar["value"] = 100
            self.state.config(text="done", style="Muted.TLabel")
        elif self.stop_requested:
            self.state.config(text="stopped", style="Muted.TLabel")
        else:
            self.state.config(text="failed", style="Warning.TLabel")

    def progress(self, line):
        m = PROGRESS.search(line)
        if not m:
            return
        self.bar["value"] = float(m.group(3))
        left = LEFT.search(line)
        self.state.config(text=f"{m.group(3)}%" +
                          (f" · {left.group(1)} min" if left else ""))


# The icon is kept as a reference on the window object. A PhotoImage lives only
# while something refers to it, and Tk does not count as a referrer: an image the
# collector has taken leaves the window with a blank icon and not one error. On
# Windows an ico is used — only that one reaches the taskbar — and a png elsewhere.
def set_window_icon(root):
    ico = os.path.join(ROOT, "web", "favicon.ico")
    png = os.path.join(ROOT, "web", "icon.png")
    try:
        if WINDOWS and os.path.isfile(ico):
            root.iconbitmap(default=ico)
        elif os.path.isfile(png):
            root._icon = tk.PhotoImage(file=png)
            root.iconphoto(True, root._icon)
    except tk.TclError:          # the window keeps the default icon
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.out = queue.Queue()
        self.server = None
        self.ready = False
        configure_theme(root)
        root.title("photostats")
        set_window_icon(root)
        root.minsize(760, 760)

        pad = {"padx": 14, "pady": (0, 6)}
        head = ttk.Frame(root)
        head.pack(fill="x", padx=14, pady=(12, 8))
        ttk.Label(head, text="photo", style="Brand.TLabel").pack(side="left")
        ttk.Label(head, text="stats", style="Accent.Brand.TLabel").pack(side="left")
        self.setup_state = ttk.Label(head, text="checking the environment…", style="Muted.TLabel")
        self.setup_state.pack(side="right")

        # The main hint. The notes under the buttons say what each one does, but
        # what leaves people at a loss is a different question — which one to press
        # now. That is answered by the state of the database, not by instructions.
        self.next_step = ttk.Label(root, text="", style="Accent.TLabel",
                                   wraplength=680, justify="left")
        self.next_step.pack(fill="x", padx=14, pady=(0, 8))

        ttk.Label(root, text="(1 step) Configure photo folders", style="Accent.TLabel").pack(anchor="w", padx=14)
        box = ttk.Frame(root)
        box.pack(fill="both", padx=14, pady=(3, 5))
        self.folders = tk.Listbox(box, height=4, activestyle="none",
                                  selectmode="extended", exportselection=False, **TEXT_SURFACE)
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
        ttk.Label(root, style="Muted.TLabel", wraplength=680, justify="left",
                  text="Sub-folders are included, so the top of your archive is "
                       "usually enough. RAW files normally sit beside a JPEG of "
                       "the same shot, so leaving that box off avoids counting "
                       "those frames twice.").pack(fill="x", padx=14, pady=(0, 10))

        self.tasks = {}
        self.rescan_dir = None
        self.rescan_applying = False
        self.rescan_auto = tk.BooleanVar()
        for key, label, note, fn in (
            ("exif", "Read EXIF",
             "(2 step) Reads dates, camera, lens and settings from the files. Fast, and "
             "enough on its own — everything except colour works after this.",
             self.args_exif),
            ("images", "Analyse images",
             "(Optional) Opens every photo to work out its colour, brightness and contrast. "
             "Slow: hours on a large archive. You can stop it and carry on later.",
             lambda: [venv_python(), "scan.py", "--colors-only"]),
            ("sign", "Sign files",
             "(Optional) Reads a little of every file so identical copies can be "
             "told apart for certain. Only needed for exact duplicate matching.",
             lambda: [venv_python(), "scan.py", "--hash-only"]),
            ("check", "Check a folder",
             "(Optional) Asks of a folder you have not scanned yet: how much of it is "
             "already in the archive? Counts copies and new files and says so at "
             "the end. Reads the database, never writes to it.",
             self.args_check),
            ("rescan", "Rescan folders",
             "Checks selected photo folders, or all configured folders if none are selected. "
             "Finds moves and marks missing files while keeping their tags, ratings and comments.",
             self.args_rescan),
        ):
            self.tasks[key] = Task(self, key, label, note, fn, root,
                                   writes=(key != "check"))
        ttk.Checkbutton(root, text="Apply changes automatically after rescan",
                        variable=self.rescan_auto).pack(anchor="w", padx=14, pady=(0, 6))

        ttk.Separator(root).pack(fill="x", padx=14, pady=(4, 6))
        srv = ttk.Frame(root)
        srv.pack(fill="x", **pad)
        self.open_btn = ttk.Button(srv, text="Open interface", command=self.open_ui, style="Primary.TButton")
        self.open_btn.pack(side="left")
        self.srv_state = ttk.Label(srv, text="server stopped", style="Muted.TLabel")
        self.srv_state.pack(side="left", padx=10)
        self.stop_btn = ttk.Button(srv, text="Stop server", command=self.stop_server,
                                   state="disabled")
        self.stop_btn.pack(side="right")
        # Off at every start and never saved: opening the archive to the network
        # should be something done on purpose that evening, not a setting that
        # quietly outlives the reason it was switched on.
        self.lan = tk.BooleanVar(value=False)
        self.lan_box = ttk.Checkbutton(srv, text="allow access from the local network",
                                       variable=self.lan, command=self.lan_changed)
        self.lan_box.pack(side="right", padx=10)
        ttk.Label(root, style="Muted.TLabel", wraplength=680, justify="left",
                  text="(3 step) Opens the statistics in your browser.  "
                       "It can stay open while a pass is running — the "
                       "numbers fill in as the scan goes.").pack(
            fill="x", padx=14, pady=(3, 4))
        self.lan_warn = ttk.Label(
            root, style="Warning.TLabel", wraplength=680, justify="left",
            text="Network access is on. There is no password: anyone on this "
                 "Wi-Fi or network can see every photo, its folder and where "
                 "it was taken, and change ratings, tags and comments. Opening "
                 "files is switched off. Windows may ask whether Python may "
                 "use the network — allow it for private networks only. Do not "
                 "use this on a public or shared network.")
        self.lan_spacer = ttk.Frame(root, height=4)
        self.lan_spacer.pack(fill="x")

        self.text = tk.Text(root, height=10, wrap="none", state="disabled",
                            font=("Consolas", 9), **TEXT_SURFACE)
        self.text.pack(fill="both", expand=True, padx=14, pady=(4, 12))

        self.load()
        self.set_ready(False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(60, self.drain)
        root.after(200, self.tick)
        threading.Thread(target=self.prepare, daemon=True).start()

    # ---------- settings ----------

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

    # ---------- what to do next ----------

    def db_state(self):
        """What is already done, read off the database itself. Read directly:
        sqlite3 is in the standard library, no environment needed for this."""
        path = os.path.join(ROOT, "photos.db")
        if not os.path.exists(path):
            return {"total": 0}
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
            con.execute("PRAGMA busy_timeout = 3000")
            try:
                available = dupkey.available_where(con)
                row = con.execute(
                    f"SELECT COUNT(*), SUM(color IS NULL), SUM(sig IS NULL) FROM photos WHERE {available}"
                ).fetchone()
                out = {"total": row[0], "no_images": row[1] or 0,
                       "no_sig": row[2] or 0}
            except sqlite3.OperationalError:
                # a database from an earlier version, without the analysis
                # columns: the first run of the scanner or the server adds them
                total, = con.execute("SELECT COUNT(*) FROM photos").fetchone()
                out = {"total": total, "no_images": total, "no_sig": total}
            con.close()
            return out
        except sqlite3.Error:
            return None          # busy being written to — keep the previous hint

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

    # ---------- environment ----------

    def prepare(self):
        """Creates .venv and installs the libraries if they are not there yet."""
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
            # ImageMath.lambda_eval is what sharpness needs (Pillow 10.3): an
            # environment from before it gets the install run again, which
            # upgrades Pillow, rather than an analysis that skips sharpness
            return subprocess.run([py, "-c", "import flask; "
                                   "from PIL.ImageMath import lambda_eval"],
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

    # ---------- tasks ----------

    def busy_task(self):
        """The pass currently holding the database for writing. Readers do not count."""
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
        """Asks for a folder of its own instead of taking the list above.

        What gets checked is precisely what is not in that list yet: a card out of
        the camera, someone else's disk, an old copy of the archive. The chosen
        folder is not saved into the settings — it would otherwise end up in the
        next EXIF pass, which is exactly what the check is there to prevent.
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

    def args_rescan(self):
        if self.rescan_dir is not None:
            return None
        selected = self.folders.curselection()
        folders = ([self.folders.get(i) for i in selected] if selected
                   else list(self.folders.get(0, "end")))
        if not folders or not os.path.isfile(os.path.join(ROOT, "photos.db")):
            messagebox.showinfo("Rescan folders", "Configure your folders and run Read EXIF first.")
            return None
        self.rescan_dir = tempfile.TemporaryDirectory(prefix=".photostats-rescan-")
        self.rescan_applying = False
        args = [venv_python(), "scan.py", "--rescan-plan", self.rescan_plan_path()] + folders
        if self.raw.get():
            args.append("--raw")
        return args

    def rescan_plan_path(self):
        return os.path.join(self.rescan_dir.name, "preview.json")

    def discard_rescan(self):
        if self.rescan_dir is not None:
            self.rescan_dir.cleanup()
            self.rescan_dir = None

    def finish_rescan(self, code):
        if code or self.rescan_applying or self.rescan_dir is None:
            self.discard_rescan()
            return
        try:
            with open(self.rescan_plan_path(), encoding="utf-8") as source:
                plan = json.load(source)
        except (OSError, ValueError) as exc:
            self.log(f"Could not read rescan preview: {exc}\n")
            self.discard_rescan()
            return
        if self.rescan_auto.get() and plan["actions"] and self.busy_task() is None:
            # Still show the report when some paths require attention.
            if not (plan["conflicts"] or plan["errors"]):
                self.apply_rescan()
                return
            self.show_rescan(plan, automatic=True)
        else:
            self.show_rescan(plan)

    def apply_rescan(self):
        self.rescan_applying = True
        self.tasks["rescan"].start([venv_python(), "scan.py", "--apply-rescan",
                                    self.rescan_plan_path()])

    def show_rescan(self, plan, automatic=False):
        win = tk.Toplevel(self.root, background=GROUND)
        win.title("Rescan folders preview")
        win.transient(self.root)
        win.geometry("760x480")
        win.minsize(460, 300)
        win.columnconfigure(0, weight=1)
        win.rowconfigure(3, weight=1)
        buttons = ttk.Frame(win)
        buttons.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        summary = ttk.Label(win, text=rescan.summary(plan), wraplength=736, style="Accent.TLabel")
        summary.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 3))

        def wrap_summary(event):
            if event.widget is win:
                summary.configure(wraplength=max(1, event.width - 24))

        win.bind("<Configure>", wrap_summary)
        ttk.Label(win, text=f"Match rule: {plan['rule']}", style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", padx=12, pady=(0, 8))
        body = ttk.Frame(win)
        body.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 12))
        report = tk.Text(body, width=1, height=1, wrap="word", padx=8, pady=6, **TEXT_SURFACE)
        bar = ttk.Scrollbar(body, command=report.yview)
        report.configure(yscrollcommand=bar.set)
        report.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        report.tag_configure("accent", foreground=ACCENT)
        parts = []

        def report_line(label, path=None):
            detail = f"   {path}\n" if path is not None else "\n"
            parts.extend((label, ("accent",), detail, ()))

        for item in plan["actions"]:
            kind, old, path = item["kind"], item["old"], item.get("path")
            if kind in ("move", "rename"):
                report_line("Move", old)
                parts.extend((f"    → {path}\n", ()))
            else:
                report_line(kind.capitalize(), path or old)
        for conflict in plan["conflicts"]:
            report_line("\nConflicting paths (left for another rescan)")
            parts.extend(("\n".join(f"  Missing: {p}" for p in conflict["old"]) + "\n", ()))
            parts.extend(("\n".join(f"  Candidate: {p}" for p in conflict["new"]) + "\n", ()))
        if plan["errors"]:
            report_line("\nWarnings")
            parts.extend(("\n".join(plan["errors"]) + "\n", ()))
        report.insert("1.0", *(parts or ["No changes found."]))
        report.configure(state="disabled")

        def close():
            win.destroy()
            if not automatic:
                self.discard_rescan()

        def apply(_event=None):
            if self.busy_task() is not None:
                messagebox.showinfo("Rescan folders", "Wait for the current pass to finish.", parent=win)
            else:
                win.destroy()
                self.apply_rescan()
            return "break"

        has_changes = bool(plan["actions"])
        ttk.Button(buttons, text="Close" if automatic or not has_changes else "Cancel",
                   command=close).pack(side="right", ipady=3)
        if automatic:
            ttk.Label(buttons, text="Applying the listed changes automatically.",
                      style="Accent.TLabel").pack(side="left")
            self.apply_rescan()
        else:
            ttk.Button(buttons, text="Apply changes", command=apply,
                       style="Primary.TButton", state="normal" if has_changes else "disabled").pack(
                side="right", padx=(0, 8), ipady=3)
            if has_changes:
                win.bind("<Control-Return>", apply)
            else:
                ttk.Label(buttons, text="No changes to apply.", style="Muted.TLabel").pack(side="left")
        win.protocol("WM_DELETE_WINDOW", close)
        win.grab_set()
        win.focus_set()

    # ---------- server ----------

    def lan_changed(self):
        if self.lan.get():
            self.lan_warn.pack(fill="x", padx=14, pady=(0, 6), before=self.lan_spacer)
        else:
            self.lan_warn.pack_forget()

    def open_ui(self):
        if not (self.server and self.server.poll() is None):
            # The address is the one the server listens on, and only that one:
            # the LAN address rather than every interface, so a VPN or a
            # virtual adapter does not get the archive along with the Wi-Fi.
            host = "127.0.0.1"
            if self.lan.get():
                host = lan_address()
                if not host:
                    messagebox.showwarning(
                        "No local network",
                        "This computer does not seem to be on a home or office "
                        "network, so there is nothing to open the archive to. "
                        "The interface will start for this computer only.")
                    self.lan.set(False)
                    self.lan_changed()
                    host = "127.0.0.1"
            self.url = f"http://{host}:{PORT}/"
            self.log("\n$ app.py\n")
            self.server = spawn([venv_python(), "app.py", "--no-browser",
                                 "--host", host, "--port", str(PORT)])
            threading.Thread(target=self.pump_server, daemon=True).start()
            self.srv_state.config(text="server starting…")
            self.stop_btn.config(state="normal")
            # where the server listens is fixed when it starts; changing the box
            # under a running server would only make it lie
            self.lan_box.config(state="disabled")
            threading.Thread(target=self.wait_and_open, daemon=True).start()
        else:
            webbrowser.open(self.url)

    def wait_and_open(self):
        for _ in range(60):
            try:
                urllib.request.urlopen(self.url, timeout=2)
                self.out.put(("server", "server running · " + self.url.rstrip("/") +
                              ("  — open this on the tablet" if self.lan.get() else "")))
                webbrowser.open(self.url)
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

    # ---------- output ----------

    def log(self, text):
        self.text.config(state="normal")
        self.text.insert("end", text if text.endswith("\n") else text + "\n")
        self.text.see("end")
        if float(self.text.index("end-1c").split(".")[0]) > 400:
            self.text.delete("1.0", "200.0")
        self.text.config(state="disabled")

    def tick(self):
        """The database changes under us while a pass runs; the hint follows it."""
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
                    if task.key == "rescan":
                        self.finish_rescan(code)
                    self.refresh_hint()
                elif kind == "ready":
                    self.set_ready(payload == "environment ready", payload)
                    self.refresh_hint()
                elif kind == "server":
                    self.srv_state.config(text=payload)
                    if "stopped" in payload or "did not" in payload:
                        self.stop_btn.config(state="disabled")
                        self.lan_box.config(state="normal")
        except queue.Empty:
            pass
        self.root.after(60, self.drain)

    # ---------- shutting down ----------

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
        self.discard_rescan()
        self.root.destroy()


def main():
    if tk is None:
        print("Tkinter is missing, so the window cannot open.\n"
              "Re-run the Python installer and tick \"tcl/tk and IDLE\".\n"
              "On Linux: install the python3-tk package.")
        input("Press Enter to close. ")
        return 1
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main() or 0)
