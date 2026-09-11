# -*- coding: utf-8 -*-
# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License, version 3, as published by
# the Free Software Foundation. See LICENSE for the full text.
# Commercial licensing is available — see COMMERCIAL.md.
# SPDX-License-Identifier: AGPL-3.0-only
"""
scan.py — обходит папки с фотографиями, читает EXIF и складывает всё в SQLite.

Запуск:
    python scan.py "D:\\Photos" "E:\\Archive 2001-2010"
    python scan.py D:\\Фото --db photos.db --full

Читает EXIF через ExifTool (если он найден — поддерживает RAW и все нестандартные
теги), иначе откатывается на библиотеку exifread. Повторный запуск обрабатывает
только новые и изменившиеся файлы.
"""

import argparse
import colorsys
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime

try:
    from version import VERSION
except ImportError:          # файл скопировали без version.py
    VERSION = "unknown"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --------------------------------------------------------------------------
# Какие файлы считаем фотографиями
# --------------------------------------------------------------------------

# Ровно два расширения. .jpe и .jfif — тоже JPEG, но так сохраняют картинки
# из браузера, а не камеры; кому нужно, добавит их ключом --ext.
JPEG_EXT = {".jpg", ".jpeg"}
RASTER_EXT = JPEG_EXT | {
    ".jpe", ".jfif",        # тоже JPEG, но по умолчанию не берём — см. выше
    ".png", ".tif", ".tiff", ".heic", ".heif", ".webp", ".avif", ".bmp", ".gif",
}
RAW_EXT = {
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".orf",
    ".rw2", ".raf", ".dng", ".pef", ".ptx", ".raw", ".rwl", ".3fr", ".fff",
    ".iiq", ".erf", ".mos", ".mrw", ".x3f", ".srw", ".kdc", ".dcr", ".mef",
}
PHOTO_EXT = RASTER_EXT | RAW_EXT          # всё, что программа вообще умеет читать
DEFAULT_EXT = set(JPEG_EXT)               # что берём, если не сказано иное

# Файлы мельче этого — эскизы, аватарки, сохранённые из переписки картинки.
# В статистике они только мешают: тянут вниз распределение размеров и дают
# ложные дубликаты, потому что одинаковых эскизов много.
MIN_SIZE = 50 * 1024

SKIP_DIRS = {
    ".git", "$recycle.bin", "system volume information", "__pycache__",
    ".thumbnails", ".picasaoriginals", "lightroom previews.lrdata",
}

# --------------------------------------------------------------------------
# Нормализация брендов и моделей
# --------------------------------------------------------------------------

BRAND_MAP = {
    "nikon corporation": "Nikon", "nikon": "Nikon",
    "canon": "Canon",
    "sony": "Sony", "sony corporation": "Sony",
    "olympus imaging corp.": "Olympus", "olympus corporation": "Olympus",
    "olympus optical co.,ltd": "Olympus", "olympus": "Olympus",
    "om digital solutions": "OM System",
    "panasonic": "Panasonic",
    "matsushita electric industrial co.,ltd.": "Panasonic",
    "fujifilm": "Fujifilm", "fujifilm corporation": "Fujifilm", "fuji photo film co., ltd.": "Fujifilm",
    "pentax": "Pentax", "pentax corporation": "Pentax", "asahi optical co.,ltd": "Pentax",
    "ricoh": "Ricoh", "ricoh imaging company, ltd.": "Ricoh",
    "apple": "Apple", "apple computer, inc.": "Apple",
    "samsung": "Samsung", "samsung techwin": "Samsung", "samsung electronics": "Samsung",
    "xiaomi": "Xiaomi", "huawei": "Huawei", "google": "Google",
    "oneplus": "OnePlus", "oppo": "OPPO", "vivo": "vivo",
    "motorola": "Motorola", "nokia": "Nokia", "htc": "HTC",
    "lg electronics": "LG", "lge": "LG",
    "leica camera ag": "Leica", "leica": "Leica",
    "sigma": "Sigma", "sigma corporation": "Sigma",
    "casio": "Casio", "casio computer co.,ltd.": "Casio",
    "eastman kodak company": "Kodak", "kodak": "Kodak",
    "minolta co., ltd.": "Minolta", "konica minolta": "Konica Minolta",
    "konica minolta camera, inc.": "Konica Minolta",
    "hewlett-packard": "HP", "hp": "HP",
    "dji": "DJI", "gopro": "GoPro", "hasselblad": "Hasselblad",
    "phase one": "Phase One", "epson": "Epson", "sanyo electric co.,ltd.": "Sanyo",
    "seiko epson corp.": "Epson", "zenit": "Zenit",
}

_BRAND_TAIL = re.compile(
    r"\s*(corporation|corp\.?|co\.?,?\s*ltd\.?|company|inc\.?|imaging|"
    r"electronics|optical|camera|ag|gmbh)\.?$", re.I)


def norm_brand(make):
    if not make:
        return None
    s = " ".join(str(make).split()).strip(" .,")
    key = s.lower()
    if key in BRAND_MAP:
        return BRAND_MAP[key]
    prev = None
    while prev != s:
        prev = s
        s = _BRAND_TAIL.sub("", s).strip(" .,")
        if s.lower() in BRAND_MAP:
            return BRAND_MAP[s.lower()]
    if not s:
        return None
    return s if (s.isupper() and len(s) <= 4) else s.title()


def norm_model(brand, model):
    """Полное имя камеры без дублирования бренда: 'Canon' + 'Canon EOS 20D'."""
    if not model:
        return None
    m = " ".join(str(model).split()).strip(" .,")
    if not m:
        return None
    if brand:
        if m.lower().startswith(brand.lower()):
            m = brand + m[len(brand):]          # единое написание бренда
        elif m.split()[0].lower() not in brand.lower():
            m = f"{brand} {m}"
    return m


def norm_lens(*candidates):
    for c in candidates:
        if c is None:
            continue
        s = " ".join(str(c).split()).strip(" .,")
        if not s or s.lower() in ("unknown", "n/a", "----", "0", "----."):
            continue
        if re.fullmatch(r"[\d.\s]+", s):          # мусор вида "0 0 0 0"
            continue
        return s
    return None


# --------------------------------------------------------------------------
# База данных
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,
    folder      TEXT,
    filename    TEXT,
    ext         TEXT,
    size        INTEGER,
    mtime       REAL,
    taken       TEXT,      -- 'YYYY-MM-DD HH:MM:SS'
    date_src    TEXT,      -- exif | file
    year        INTEGER,
    month       INTEGER,
    ym          TEXT,      -- 'YYYY-MM'
    day         INTEGER,
    hour        INTEGER,
    weekday     INTEGER,   -- 0 = понедельник
    brand       TEXT,
    camera      TEXT,
    lens        TEXT,
    focal       REAL,
    focal35     REAL,
    iso         INTEGER,
    fnumber     REAL,
    shutter     TEXT,      -- для показа: '1/125'
    exposure    REAL,      -- то же в секундах, для распределения
    width       INTEGER,
    height      INTEGER,
    lat         REAL,      -- координаты из EXIF, знаковые градусы
    lon         REAL,
    sig         TEXT,      -- подпись содержимого; NULL = проход не делали
    color       TEXT,      -- группа основного цвета; NULL = ещё не разбирали
    color_hex   TEXT,      -- средний оттенок этой группы, для образца
    color_share REAL,      -- какую долю кадра она занимает
    color_center       TEXT,   -- то же по центральной половине площади кадра
    color_center_hex   TEXT,
    color_center_share REAL,
    brightness  REAL,      -- средняя яркость, 0…1
    contrast    REAL,      -- разброс яркости
    chroma      REAL,      -- доля цветных точек; около нуля — чёрно-белый кадр
    clip_hi     REAL,      -- доля выбитых в белое точек
    clip_lo     REAL       -- доля провалившихся в чёрное
);
CREATE INDEX IF NOT EXISTS ix_year   ON photos(year);
CREATE INDEX IF NOT EXISTS ix_ym     ON photos(ym);
CREATE INDEX IF NOT EXISTS ix_month  ON photos(month);
CREATE INDEX IF NOT EXISTS ix_hour   ON photos(hour);
CREATE INDEX IF NOT EXISTS ix_brand  ON photos(brand);
CREATE INDEX IF NOT EXISTS ix_camera ON photos(camera);
CREATE INDEX IF NOT EXISTS ix_lens   ON photos(lens);
CREATE INDEX IF NOT EXISTS ix_focal  ON photos(focal);
CREATE INDEX IF NOT EXISTS ix_iso    ON photos(iso);
CREATE INDEX IF NOT EXISTS ix_color  ON photos(color);
CREATE INDEX IF NOT EXISTS ix_geo    ON photos(lat, lon);
CREATE INDEX IF NOT EXISTS ix_sig    ON photos(sig);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

COLUMNS = [
    "path", "folder", "filename", "ext", "size", "mtime", "taken", "date_src",
    "year", "month", "ym", "day", "hour", "weekday", "brand", "camera", "lens",
    "focal", "focal35", "iso", "fnumber", "shutter", "exposure", "width", "height",
    "lat", "lon",
]


# Сколько ждать освобождения базы. Сканер и сервер работают одновременно, и без
# ожидания почти половина запросов падает с "database is locked": на замере из
# трёх секунд одновременной работы это 891 ошибка чтения и 513 записи.
BUSY_MS = 15000


def tune(con):
    """Режимы, без которых параллельная работа сканера и сервера невозможна."""
    con.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
    # WAL пропускает читателя и писателя одновременно и даёт в полтора раза
    # больше чтений. На сетевых дисках он недоступен — тогда молча остаётся
    # обычный журнал, и спасает только ожидание блокировки выше.
    try:
        con.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error:
        pass
    return con


def open_db(path):
    """Таблицы, потом миграция, и только потом индексы: индекс по новой колонке
    нельзя создать раньше, чем миграция её добавит в уже существующую базу."""
    con = tune(sqlite3.connect(path, timeout=BUSY_MS / 1000))
    # комментарии убираем до разбора: в них встречается точка с запятой
    clean = "\n".join(re.sub(r"--.*$", "", ln) for ln in SCHEMA.splitlines())
    stmts = [x.strip() for x in clean.split(";") if x.strip()]
    for st in stmts:
        if not st.upper().startswith("CREATE INDEX"):
            con.execute(st)
    migrate(con)
    for st in stmts:
        if st.upper().startswith("CREATE INDEX"):
            con.execute(st)
    con.commit()
    return con


def migrate(con):
    """Достраивает базы, собранные прежними версиями программы."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(photos)")}
    for name, decl in (("lat", "REAL"), ("lon", "REAL"), ("sig", "TEXT"),
                       ("color", "TEXT"), ("color_hex", "TEXT"),
                       ("color_share", "REAL"), ("color_center", "TEXT"),
                       ("color_center_hex", "TEXT"),
                       ("color_center_share", "REAL"), ("brightness", "REAL"),
                       ("contrast", "REAL"), ("chroma", "REAL"),
                       ("clip_hi", "REAL"), ("clip_lo", "REAL")):
        if name not in cols:
            con.execute(f"ALTER TABLE photos ADD COLUMN {name} {decl}")
            con.commit()
    if "exposure" not in cols:
        con.execute("ALTER TABLE photos ADD COLUMN exposure REAL")
        rows = con.execute("SELECT id, shutter FROM photos "
                           "WHERE shutter IS NOT NULL").fetchall()
        upd = [(shutter_seconds(t), i) for i, t in rows]
        con.executemany("UPDATE photos SET exposure = ? WHERE id = ?",
                        [u for u in upd if u[0]])
        con.commit()
        print(f"Database upgraded: shutter speed converted to seconds "
              f"({len(upd)} rows).")


# --------------------------------------------------------------------------
# Разбор значений
# --------------------------------------------------------------------------

_DT_RE = re.compile(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def parse_dt(value):
    if not value:
        return None
    m = _DT_RE.search(str(value))
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    if not (1970 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31 and h < 24):
        return None
    try:
        return datetime(y, mo, d, h, mi, min(s, 59))
    except ValueError:
        return None


def to_float(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) or None
    num, den = getattr(v, "numerator", None), getattr(v, "denominator", None)
    if num is not None and den:
        return float(num) / float(den) or None
    if isinstance(v, (tuple, list)) and len(v) == 2 and v[1]:
        return float(v[0]) / float(v[1]) or None
    s = str(v).strip()
    m = re.match(r"^\s*([\d.]+)\s*/\s*([\d.]+)\s*$", s)
    if m:
        try:
            den = float(m.group(2))
            return float(m.group(1)) / den if den else None
        except ValueError:
            return None
    m = re.search(r"[\d.]+", s)
    try:
        return float(m.group()) if m else None
    except ValueError:
        return None


def to_int(v):
    f = to_float(v.split()[0] if isinstance(v, str) and v.split() else v)
    return int(f) if f else None


def fmt_shutter(sec):
    """0.008 -> '1/125'. Для показа; для статистики хранится само число."""
    if not sec:
        return None
    if sec >= 1:
        return f"{sec:g}s"
    return f"1/{int(round(1 / sec))}"


def shutter_seconds(txt):
    """Обратный разбор: '1/125' -> 0.008. Нужен для миграции старых баз."""
    if not txt:
        return None
    t = str(txt).strip().rstrip("sс")
    try:
        if "/" in t:
            a, b = t.split("/")
            return float(a) / float(b)
        return float(t)
    except (ValueError, ZeroDivisionError):
        return None


def gps_value(raw, ref):
    """Координата из EXIF: либо готовое число, либо градусы-минуты-секунды.
    Полушарие задаётся отдельным тегом, S и W означают отрицательное значение."""
    if raw is None:
        return None
    val = None
    if isinstance(raw, (int, float)):
        val = float(raw)
    elif isinstance(raw, (list, tuple)) and len(raw) == 3:
        parts = [to_float(x) or 0 for x in raw]
        val = parts[0] + parts[1] / 60 + parts[2] / 3600
    else:
        txt = str(raw).strip().strip("[]")
        if "," in txt:
            parts = [to_float(x) or 0 for x in txt.split(",")]
            if len(parts) == 3:
                val = parts[0] + parts[1] / 60 + parts[2] / 3600
        if val is None:
            val = to_float(txt)
    if val is None:
        return None
    if ref and str(ref).strip().upper().startswith(("S", "W")) and val > 0:
        val = -val
    return val


def make_geo(tags):
    lat = gps_value(tags.get("GPSLatitude"), tags.get("GPSLatitudeRef"))
    lon = gps_value(tags.get("GPSLongitude"), tags.get("GPSLongitudeRef"))
    if lat is None or lon is None:
        return None, None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:      # нулевой остров — почти всегда мусор
        return None, None
    return round(lat, 6), round(lon, 6)


def make_row(path, st, tags):
    """tags — словарь EXIF-полей (уже унифицированный)."""
    dt = None
    src = "exif"
    for k in ("DateTimeOriginal", "CreateDate", "DateTimeDigitized", "ModifyDate"):
        dt = parse_dt(tags.get(k))
        if dt:
            break
    if not dt:
        dt = datetime.fromtimestamp(st.st_mtime)
        src = "file"

    geo = make_geo(tags)
    brand = norm_brand(tags.get("Make"))
    camera = norm_model(brand, tags.get("Model"))
    lens = norm_lens(tags.get("LensModel"), tags.get("LensID"), tags.get("Lens"),
                     tags.get("LensType"), tags.get("LensSpec"))
    focal = to_float(tags.get("FocalLength"))
    focal35 = to_float(tags.get("FocalLengthIn35mmFormat"))

    return {
        "path": path,
        "folder": os.path.dirname(path),
        "filename": os.path.basename(path),
        "ext": os.path.splitext(path)[1].lower().lstrip("."),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "taken": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "date_src": src,
        "year": dt.year,
        "month": dt.month,
        "ym": dt.strftime("%Y-%m"),
        "day": dt.day,
        "hour": dt.hour,
        "weekday": dt.weekday(),
        "brand": brand,
        "camera": camera,
        "lens": lens,
        "focal": focal if focal and 0 < focal < 3000 else None,
        "focal35": focal35 if focal35 and 0 < focal35 < 3000 else None,
        "iso": to_int(tags.get("ISO")),
        "fnumber": to_float(tags.get("FNumber")),
        "shutter": fmt_shutter(to_float(tags.get("ExposureTime"))),
        "exposure": to_float(tags.get("ExposureTime")),
        "width": to_int(tags.get("ImageWidth")),
        "height": to_int(tags.get("ImageHeight")),
        "lat": geo[0],
        "lon": geo[1],
    }


# --------------------------------------------------------------------------
# Чтение EXIF — ExifTool
# --------------------------------------------------------------------------

EXIFTOOL_TAGS = [
    "-DateTimeOriginal", "-CreateDate", "-ModifyDate", "-Make", "-Model",
    "-LensModel", "-LensID", "-Lens", "-LensType", "-LensSpec",
    "-FocalLength", "-FocalLengthIn35mmFormat", "-ISO", "-FNumber",
    "-ExposureTime", "-ImageWidth", "-ImageHeight",
    "-GPSLatitude", "-GPSLongitude",
]


def find_exiftool():
    """Ищем в PATH, затем рядом со скриптом. Возвращаем (путь, версия)."""
    candidates = []
    for name in ("exiftool", "exiftool.exe"):
        p = shutil.which(name)
        if p:
            candidates.append(p)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates += [os.path.join(here, "exiftool.exe"),
                   os.path.join(here, "exiftool", "exiftool.exe"),
                   os.path.join(here, "exiftool")]
    for cand in candidates:
        if not os.path.isfile(cand):
            continue
        ver = exiftool_version(cand)
        if ver:
            return cand, ver
        # exe есть, но не запускается — почти всегда потерян exiftool_files
        near = os.path.join(os.path.dirname(cand), "exiftool_files")
        print(f"  ! {cand} does not run.")
        if not os.path.isdir(near):
            print(f"  ! No exiftool_files folder next to it — move that folder "
                  f"together with the exe, from the same archive.")
    return None, None


def exiftool_version(exe):
    try:
        out = subprocess.run([exe, "-ver"], capture_output=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ver = out.stdout.decode("utf-8", "replace").strip()
        return ver if re.fullmatch(r"[\d.]+", ver) else None
    except Exception:
        return None


def read_chunk_exiftool(exe, paths):
    fd, arg = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(paths))
        cmd = [exe, "-json", "-n", "-q", "-q", "-m", "-fast2",
               "-charset", "filename=UTF8", *EXIFTOOL_TAGS, "-@", arg]
        out = subprocess.run(cmd, capture_output=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = out.stdout.decode("utf-8", "replace").strip()
        if not text:
            return {}
        data = json.loads(text)
        return {os.path.normpath(d.get("SourceFile", "")): d for d in data}
    except Exception as e:
        print(f"  ! ExifTool: {e}", flush=True)
        return {}
    finally:
        try:
            os.unlink(arg)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Чтение EXIF — резервный вариант без ExifTool
# --------------------------------------------------------------------------

BASE_IFD = {0x010F: "Make", 0x0110: "Model", 0x0132: "ModifyDate"}
GPS_IFD = {1: "GPSLatitudeRef", 2: "GPSLatitude",
           3: "GPSLongitudeRef", 4: "GPSLongitude"}
EXIF_IFD = {
    0x9003: "DateTimeOriginal", 0x9004: "CreateDate", 0x920A: "FocalLength",
    0xA405: "FocalLengthIn35mmFormat", 0x8827: "ISO", 0x829D: "FNumber",
    0x829A: "ExposureTime", 0xA002: "ImageWidth", 0xA003: "ImageHeight",
    0xA434: "LensModel", 0xA432: "LensSpec", 0xA433: "Lens",
}


def read_one_fallback(path):
    tags = {}
    try:
        import exifread
        with open(path, "rb") as f:
            raw = exifread.process_file(f, details=False)
        get = lambda k: str(raw[k]) if k in raw else None
        tags = {
            "DateTimeOriginal": get("EXIF DateTimeOriginal"),
            "CreateDate": get("EXIF DateTimeDigitized"),
            "ModifyDate": get("Image DateTime"),
            "Make": get("Image Make"),
            "Model": get("Image Model"),
            "LensModel": get("EXIF LensModel") or get("MakerNote LensModel"),
            "LensID": get("MakerNote LensType") or get("MakerNote Lens"),
            "FocalLength": get("EXIF FocalLength"),
            "FocalLengthIn35mmFormat": get("EXIF FocalLengthIn35mmFilm"),
            "ISO": get("EXIF ISOSpeedRatings"),
            "FNumber": get("EXIF FNumber"),
            "ExposureTime": get("EXIF ExposureTime"),
            "ImageWidth": get("EXIF ExifImageWidth"),
            "ImageHeight": get("EXIF ExifImageLength"),
            "GPSLatitude": get("GPS GPSLatitude"),
            "GPSLatitudeRef": get("GPS GPSLatitudeRef"),
            "GPSLongitude": get("GPS GPSLongitude"),
            "GPSLongitudeRef": get("GPS GPSLongitudeRef"),
        }
    except Exception:
        pass
    if not tags.get("DateTimeOriginal"):
        try:
            from PIL import Image
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except Exception:
                pass
            with Image.open(path) as im:
                tags.setdefault("ImageWidth", im.width)
                tags.setdefault("ImageHeight", im.height)
                ex = im.getexif()
                for tid, name in BASE_IFD.items():
                    if ex.get(tid) is not None:
                        tags[name] = ex.get(tid)
                sub = ex.get_ifd(0x8769) or {}
                for tid, name in EXIF_IFD.items():
                    if sub.get(tid) is not None:
                        tags[name] = sub.get(tid)
                gps = ex.get_ifd(0x8825) or {}
                for tid, name in GPS_IFD.items():
                    if gps.get(tid) is not None:
                        tags[name] = gps.get(tid)
        except Exception:
            pass
    return {k: v for k, v in tags.items() if v is not None}


# --------------------------------------------------------------------------
# Подпись содержимого
# --------------------------------------------------------------------------

SIG_HEAD = 64 * 1024        # сколько байт берём с начала и с конца файла


def file_sig(job):
    """Подпись файла: размер плюс начало и конец содержимого.

    Читать файл целиком незачем. У снимка в 4,5 МБ совпадение размера, первых
    и последних 64 килобайт означает совпадение содержимого — разойтись они
    могут разве что в специально собранном файле. Зато вместо ста семидесяти
    гигабайт с диска читается пять, и проход занимает минуты, а не часы.
    Полное чтение включается ключом --hash-full."""
    pid, path, full = job
    try:
        size = os.path.getsize(path)
        h = hashlib.sha1(str(size).encode())
        with open(path, "rb") as f:
            if full:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                return pid, "f1:" + h.hexdigest()
            h.update(f.read(SIG_HEAD))
            if size > SIG_HEAD * 2:
                f.seek(-SIG_HEAD, os.SEEK_END)
                h.update(f.read(SIG_HEAD))
        return pid, "p1:" + h.hexdigest()
    except OSError:
        return pid, None


def sign_files(con, workers, full=False):
    """Считает подписи там, где их ещё нет. Прерывается и продолжается."""
    mode = "full" if full else "partial"
    row = con.execute("SELECT v FROM meta WHERE k = 'sig_mode'").fetchone()
    if row and row[0] != mode:
        print(f"Signature mode changed ({row[0]} -> {mode}) — recomputing all.")
        con.execute("UPDATE photos SET sig = NULL")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('sig_mode', ?)", (mode,))
    con.commit()

    rows = con.execute("SELECT id, path FROM photos WHERE sig IS NULL").fetchall()
    if not rows:
        print("Signatures already computed for every photo.")
        return
    jobs = [(pid, path, full) for pid, path in rows]
    print(f"Signing files: {len(jobs)}, {'whole file' if full else 'head and tail'}.")
    print("Ctrl+C stops it; whatever is done is kept.", flush=True)

    started, done, buf = time.time(), 0, []
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        for pid, sig in pool.map(file_sig, jobs):
            buf.append((sig, pid))
            done += 1
            if len(buf) >= 400:
                con.executemany("UPDATE photos SET sig = ? WHERE id = ?", buf)
                con.commit()
                buf = []
            if done % 200 == 0 or done == len(jobs):
                el = time.time() - started
                speed = done / max(el, 0.001)
                left = (len(jobs) - done) / max(speed, 0.001)
                print(f"\r  {done}/{len(jobs)} ({100 * done / len(jobs):5.1f}%)  "
                      f"{speed:.0f} files/s  about {left / 60:.0f} min left    ",
                      end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        pool.shutdown(wait=False)
        if buf:
            con.executemany("UPDATE photos SET sig = ? WHERE id = ?", buf)
            con.commit()
    print()
    bad, = con.execute("SELECT COUNT(*) FROM photos WHERE sig IS NULL").fetchone()
    if bad:
        print(f"{bad} files could not be read; duplicates fall back to "
              f"name, size and capture time until they are.")


# --------------------------------------------------------------------------
# Основной цвет кадра
# --------------------------------------------------------------------------

# Группы оттенков по кругу, в градусах: (верхняя граница, имя).
# Фиолетовый добавлен к перечисленным девяти: между синим и красным иначе
# остаётся пробел, и сирень с закатным небом распределялись бы наугад.
# Чтобы вернуться к девяти группам, замените "фиолетовый" на "синий".
HUE_GROUPS = [(15, "red"), (45, "orange"), (70, "yellow"),
              (165, "green"), (215, "cyan"), (260, "blue"),
              (335, "violet")]
CHROMATIC = {"red", "orange", "brown", "yellow", "green",
             "cyan", "blue", "violet"}

# Коричневый, оранжевый и жёлтый — одна тёплая семья: между собой их делит не
# оттенок, а яркость и чистота. При выборе главного цвета они считаются вместе,
# иначе осенний кадр проигрывает цельному зелёному, хотя тёплого в нём больше:
# листья разойдутся на 14% оранжевого, 12% жёлтого и 8% коричневого против 30%
# мха. Победившая семья называется по самой многочисленной группе.
WARM = {"brown", "orange", "yellow"}

# То же самое случается на любой границе оттенков, если сюжет через неё
# переходит: глубокое небо с градиентом 206–219° делится на голубой и синий и
# проигрывает траве, хотя неба в кадре вдвое больше. Поэтому две соседние
# группы считаются одним цветом, когда их точки образуют непрерывную полосу —
# то есть по обе стороны от границы оттенков что-то есть. Далёкие друг от друга
# зелень и небо так не слипнутся: у границы между ними пусто.
# Границы вокруг зелёного намеренно пропущены. Именно по ним чаще всего
# проходит граница между разными сюжетами: золотая листва рядом с мхом, кроны
# рядом с небом или водой. Слить их — значит стереть то, ради чего метрика и
# нужна: осенний кадр снова стал бы зелёным.
NEIGHBOURS = [({"warm"}, {"red"}, 15), ({"cyan"}, {"blue"}, 215),
              ({"blue"}, {"violet"}, 260), ({"violet"}, {"red"}, 335)]
TOUCH_BAND = 18      # насколько близко к границе точка считается пограничной
TOUCH_MIN = 0.03     # какую долю пары должны составлять точки с каждой стороны

GRAY_SAT = 0.10     # ниже этой насыщенности точка считается ахроматической
BLACK_VAL = 0.16    # ниже этой яркости — чёрная, каким бы ни был оттенок
DARK_VAL = 0.28     # до этой яркости цвет засчитывается только при высокой чистоте
DARK_SAT = 0.80     # какой должна быть чистота цвета, чтобы тёмная точка не стала чёрной

# Коричневый — это тёплый оттенок, который либо тёмный, либо приглушённый.
# Своего места на круге у него нет: шоколад и яркий апельсин отличаются не
# оттенком, а яркостью, поэтому он выделяется внутри красно-жёлтой дуги.
# Коричневый — только тёмный тёплый тон: дерево, кора, шоколад, земля.
# Светлые приглушённые тёплые тона коричневыми не считаются — пшеничное поле
# и песок глаз читает как золотые, а не как коричневые.
BROWN_HUE = (8, 50)        # дуга оттенков, где коричневый возможен, в градусах
# Дуга захватывает и красную сторону круга: прелая листва и лесная подстилка
# лежат на 345–8°, и без этого они попадали в красный — а оттуда вытягивали за
# собой название всей тёплой семьи. Чистый тёмно-красный там тоже встречается
# (вино, кирпич), поэтому на этом участке действует потолок чистоты.
BROWN_RED_LO = 345
BROWN_RED_SAT = 0.45
BROWN_VAL = 0.62           # тёплый оттенок темнее этого — коричневый
BROWN_SAT_MIN = 0.18       # но не почти-серый: тёплый серый остаётся серым

# Золотой — светлый приглушённый тёплый тон. Оттенок у него оранжевый, 30–50°,
# но сочным апельсином он не выглядит: солома, песок и сепия читаются как
# жёлтые. Отличает их чистота: у апельсина она под единицу, у поля вдвое ниже.
GOLD_HUE = (30, 50)
GOLD_SAT = 0.55            # выше этой чистоты тон остаётся оранжевым
GOLD_VAL = 0.62            # ниже этой яркости он уже коричневый

OLIVE_HUE = (55, 70)       # где жёлтый вообще может оказаться оливковым
OLIVE_MAX = 0.30           # ниже этого произведения жёлтый читается зелёным
WHITE_VAL = 0.82
CHROMA_MIN = 0.25   # доля цветных точек, начиная с которой кадр считаем цветным
CENTER_AREA = 0.50  # какую долю площади занимает центральная область кадра
CLIP_HI = 0.98      # ярче этого — света выбиты в белое
CLIP_LO = 0.016     # темнее этого — тени провалились в чёрное
UNKNOWN = "unknown"

# Версия правил разбора. При её изменении программа сама пересчитывает архив:
# иначе снимки, разобранные прежними правилами, молча остались бы в старых
# группах и статистика перестала бы быть однородной.
ALGO_VERSION = 11

# Версия набора полей EXIF. При её изменении сканер перечитывает метаданные
# всех файлов: иначе новое поле осталось бы пустым у всех, кто уже в базе.
# Разбор изображений при этом не теряется — он лежит в отдельных колонках.
EXIF_VERSION = 2


def pixel_group(r, g, b):
    """Одна точка — одна группа.

    Тёплые тона делятся по яркости и чистоте: тёмные — коричневые, светлые
    приглушённые — золотые, то есть жёлтые, а сочные остаются оранжевыми.
    Нижний порог чистоты не даёт увести в коричневый тёплый серый, который от
    него в полушаге.

    К тёмным точкам правило строже. Тёмно-коричневый (70, 35, 20) формально
    имеет красноватый оттенок, но глазом читается как тень, а не как цвет —
    чистоты в нём чуть больше половины. А вот (57, 4, 3), где на красный
    приходится почти вся яркость, остаётся красным, хоть и тёмным. Разделяет
    эти случаи именно чистота цвета, поэтому в тёмной зоне поднят её порог."""
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if v < BLACK_VAL:
        return "black"
    if s < GRAY_SAT:
        return "white" if v > WHITE_VAL else "gray"
    if v < DARK_VAL and s < DARK_SAT:
        return "black"
    deg = h * 360
    reddish = (deg >= BROWN_RED_LO or deg < BROWN_HUE[0]) and s < BROWN_RED_SAT
    if ((BROWN_HUE[0] <= deg < BROWN_HUE[1] or reddish)
            and s >= BROWN_SAT_MIN and v < BROWN_VAL):
        return "brown"
    if GOLD_HUE[0] <= deg < GOLD_HUE[1] and s < GOLD_SAT and v >= GOLD_VAL:
        return "yellow"
    if OLIVE_HUE[0] <= deg < OLIVE_HUE[1] and s * v < OLIVE_MAX:
        return "green"
    for edge, name in HUE_GROUPS:
        if deg < edge:
            return name
    return "red"                      # хвост круга, 335…360


def band_touches(hues, edge, total):
    """Полоса переходит через границу оттенков, если точки есть с обеих сторон."""
    lo = sum(n for d, n in hues.items() if 0 < (edge - d) % 360 <= TOUCH_BAND)
    hi = sum(n for d, n in hues.items() if 0 <= (d - edge) % 360 < TOUCH_BAND)
    need = max(1, int(total * TOUCH_MIN))
    return lo >= need and hi >= need


def winner(tally, sums, hues, total):
    """Побеждает самая многочисленная группа; соседние сливаются, если сюжет
    переходит через границу оттенков.

    Ахроматические группы участвуют отдельно от цветных: если цветных точек
    набралось хотя бы четверть, серое небо или чёрный фон в конкурсе не
    участвуют — иначе почти весь архив оказался бы серым."""
    if not total:
        return None, None, None, 0.0
    chroma = sum(n for k, n in tally.items() if k in CHROMATIC) / total
    allowed = CHROMATIC if chroma >= CHROMA_MIN else set(tally) - CHROMATIC
    cand = {k: n for k, n in tally.items() if k in allowed} or dict(tally)

    # тёплые считаем одной группой: их делит яркость, а не оттенок
    groups = {}
    warm = {k: n for k, n in cand.items() if k in WARM}
    if warm:
        groups["warm"] = set(warm)
    for k in cand:
        if k not in WARM:
            groups[k] = {k}

    size = lambda names: sum(cand[n] for n in names)
    best = max(groups.values(), key=size)

    for a, b, edge in NEIGHBOURS:
        if not (a <= set(groups) and b <= set(groups)):
            continue
        pair = groups[next(iter(a))] | groups[next(iter(b))]
        if size(pair) > size(best) and band_touches(hues, edge, size(pair)):
            best = pair

    win = max(best, key=cand.get)              # называем по главной группе
    n = size(best)
    acc = [0, 0, 0]
    for k in best:
        for i in range(3):
            acc[i] += sums[k][i]
    hexv = "#%02X%02X%02X" % (acc[0] // n, acc[1] // n, acc[2] // n)
    return win, hexv, round(n / total, 3), chroma


def image_stats(im):
    """Один проход по точкам уменьшенной копии — цвет и характеристики света.

    Цвет определяется голосованием по точкам. Усреднять RGB по кадру нельзя:
    у пёстрого снимка среднее всегда выходит бурым — цвета, которого в кадре
    нет вовсе.

    Отдельно считается центральная область — половина площади кадра, вырезанная
    по середине. Основной цвет описывает в первую очередь фон, центральный —
    то, что в кадре снято. Порознь они мало что дают, а вместе позволяют искать
    сюжет: чёрный кот на белой простыне это белый основной и чёрный центральный.

    Заодно считаем яркость, контраст, долю цветных точек и потери в светах и
    тенях: точки уже разобраны, всё это обходится в несколько сложений."""
    # tobytes() даёт те же точки и не зависит от версии Pillow,
    # в отличие от getdata(), объявленного устаревшим в 13-й
    raw = im.tobytes()
    w, hgt = im.size
    total = len(raw) // 3
    if not total:
        return None

    # сторона центрального квадрата: половина площади — это сторона в √2 раз
    # меньше, то есть примерно 0,707 от кадра. Краёв область не касается.
    side = max(1, round(min(w, hgt) * CENTER_AREA ** 0.5))
    cx0 = (w - side) // 2
    cy0 = (hgt - side) // 2
    cx1, cy1 = cx0 + side, cy0 + side

    tally, sums = Counter(), defaultdict(lambda: [0, 0, 0])
    ctally, csums = Counter(), defaultdict(lambda: [0, 0, 0])
    hues, chues = Counter(), Counter()      # для проверки непрерывности полосы
    ctotal = 0
    lum_sum = lum_sq = 0.0
    hi = lo = 0

    for i in range(total):
        p = i * 3
        r, g, b = raw[p], raw[p + 1], raw[p + 2]

        key = pixel_group(r, g, b)
        tally[key] += 1
        acc = sums[key]
        acc[0] += r
        acc[1] += g
        acc[2] += b
        if key in CHROMATIC:
            deg = int(colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[0] * 360)
            hues[deg] += 1

        y, x = divmod(i, w)
        if cy0 <= y < cy1 and cx0 <= x < cx1:
            ctally[key] += 1
            cacc = csums[key]
            cacc[0] += r
            cacc[1] += g
            cacc[2] += b
            ctotal += 1
            if key in CHROMATIC:
                chues[deg] += 1

        # яркость по восприятию: глаз чувствительнее всего к зелёному
        lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
        lum_sum += lum
        lum_sq += lum * lum
        if lum > CLIP_HI:
            hi += 1
        elif lum < CLIP_LO:
            lo += 1

    win, hexv, share, chroma = winner(tally, sums, hues, total)
    cwin, chexv, cshare, _ = winner(ctally, csums, chues, ctotal)

    mean = lum_sum / total
    var = max(lum_sq / total - mean * mean, 0.0)      # разброс яркости = контраст

    return {
        "color": win,
        "color_hex": hexv,
        "color_share": share,
        "color_center": cwin,
        "color_center_hex": chexv,
        "color_center_share": cshare,
        "brightness": round(mean, 4),
        "contrast": round(var ** 0.5, 4),
        "chroma": round(chroma, 4),
        "clip_hi": round(hi / total, 4),
        "clip_lo": round(lo / total, 4),
    }


def load_small(path, exe=None, grid=48):
    """Уменьшенная копия кадра для разбора.

    JPEG сжимается прямо при декодировании, поэтому полный размер в память не
    поднимается. Уменьшаем строго NEAREST, то есть берём существующие точки, а
    не усредняем соседние: ночной кадр из чёрного фона и жёлтых огней при
    сглаживании превратился бы в сплошной бурый."""
    try:
        from PIL import Image
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        with Image.open(path) as im:
            try:
                im.draft("RGB", (grid * 4, grid * 4))
            except Exception:
                pass
            return im.convert("RGB").resize((grid, grid), Image.NEAREST)
    except Exception:
        pass
    if exe:                                # RAW: берём встроенное превью
        import io
        for tag in ("-ThumbnailImage", "-PreviewImage", "-JpgFromRaw"):
            try:
                out = subprocess.run(
                    [exe, "-b", tag, path], capture_output=True, timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if out.stdout and len(out.stdout) > 1000:
                    from PIL import Image
                    with Image.open(io.BytesIO(out.stdout)) as im:
                        return im.convert("RGB").resize((grid, grid), Image.NEAREST)
            except Exception:
                continue
    return None


STAT_KEYS = ["color", "color_hex", "color_share",
             "color_center", "color_center_hex", "color_center_share",
             "brightness", "contrast",
             "chroma", "clip_hi", "clip_lo"]
# длина всегда равна STAT_KEYS: добавили метрику — заглушка подстроилась сама
FAILED = (UNKNOWN,) + (None,) * (len(STAT_KEYS) - 1)


def stats_of(job):
    """Задание для рабочего процесса: (id, путь, путь к exiftool)."""
    pid, path, exe = job
    im = load_small(path, exe)
    if im is None:
        return (pid, *FAILED)
    try:
        got = image_stats(im)
    except Exception:
        got = None
    if not got:
        return (pid, *FAILED)
    return (pid, *(got[k] for k in STAT_KEYS))


def analyze_colors(con, exe, workers, reset=False):
    """Разбирает только те снимки, у которых разбора ещё не было: проход можно
    прервать и продолжить другим запуском. Условие включает brightness, чтобы
    базы, собранные до появления метрик света, дополнились сами."""
    row = con.execute("SELECT v FROM meta WHERE k = 'color_algo'").fetchone()
    stored = int(row[0]) if row else 0
    if reset or stored != ALGO_VERSION:
        done_before, = con.execute("SELECT COUNT(*) FROM photos "
                                   "WHERE color IS NOT NULL").fetchone()
        if done_before:
            print("Image analysis rules have changed — "
                  f"re-analysing all {done_before} photos.")
        con.execute("UPDATE photos SET color=NULL, color_hex=NULL, color_share=NULL,"
                    " brightness=NULL, contrast=NULL, chroma=NULL,"
                    " clip_hi=NULL, clip_lo=NULL")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('color_algo', ?)",
                    (str(ALGO_VERSION),))
        con.commit()

    rows = con.execute("SELECT id, path FROM photos "
                       "WHERE color IS NULL OR brightness IS NULL").fetchall()
    if not rows:
        print("Images already analysed — colour and light are done for every photo.")
        return
    jobs = [(pid, path, exe) for pid, path in rows]
    print(f"Analysing images: {len(jobs)} files, {workers} processes.")
    print("This is much slower than EXIF — the pixels are read.")
    print("Ctrl+C stops it; whatever is done is kept.", flush=True)

    sql = ("UPDATE photos SET " + ", ".join(f"{k}=?" for k in STAT_KEYS) +
           " WHERE id=?")
    started, done, buf = time.time(), 0, []
    pool = ProcessPoolExecutor(max_workers=workers)
    try:
        for row in pool.map(stats_of, jobs, chunksize=16):
            pid, values = row[0], row[1:]
            buf.append((*values, pid))
            done += 1
            if len(buf) >= 200:
                con.executemany(sql, buf)
                con.commit()
                buf = []
            if done % 100 == 0 or done == len(jobs):
                el = time.time() - started
                speed = done / max(el, 0.001)
                left = (len(jobs) - done) / max(speed, 0.001)
                print(f"\r  {done}/{len(jobs)} ({100 * done / len(jobs):5.1f}%)  "
                      f"{speed:.0f} files/s  about {left / 60:.0f} min left    ",
                      end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if buf:
            con.executemany(sql, buf)
            con.commit()
    print()

    bad, = con.execute("SELECT COUNT(*) FROM photos WHERE color = ?",
                       (UNKNOWN,)).fetchone()
    if bad:
        print(f"Could not read the image of {bad} files"
              f"{' (RAW needs ExifTool)' if not exe else ''}.")
    left, = con.execute("SELECT COUNT(*) FROM photos "
                        "WHERE color IS NULL OR brightness IS NULL").fetchone()
    if left:
        print(f"Still to analyse: {left}. Run again to continue.")


# --------------------------------------------------------------------------
# Обход файловой системы
# --------------------------------------------------------------------------

def walk_photos(roots, exts, passed_by=None):
    """Отдаёт файлы с расширениями из exts. Остальные знакомые форматы
    считает в passed_by, чтобы потом подсказать про --raw и --all."""
    for root in roots:
        root = os.path.abspath(root)
        if os.path.isfile(root):
            if os.path.splitext(root)[1].lower() in exts:
                yield os.path.normpath(root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS
                           and not d.startswith(".")]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in exts:
                    yield os.path.normpath(os.path.join(dirpath, fn))
                elif ext in PHOTO_EXT and passed_by is not None:
                    passed_by[ext] += 1


def active_exts(args):
    """--ext задаёт список целиком, --raw добавляет RAW сверху, --all берёт всё."""
    if args.all_formats:
        return set(PHOTO_EXT)
    if args.ext:
        exts = {"." + e.strip().lower().lstrip(".")
                for e in args.ext.split(",") if e.strip()}
    else:
        exts = set(DEFAULT_EXT)
    if args.raw:
        exts |= RAW_EXT
    return exts


def describe_exts(exts):
    if exts >= PHOTO_EXT:
        return "every supported format"
    if exts == DEFAULT_EXT:
        return "JPEG only"
    if exts == DEFAULT_EXT | RAW_EXT:
        return "JPEG, RAW and DNG"
    return ", ".join(sorted(e.lstrip(".") for e in exts))


def report_passed(passed_by):
    """Сообщает, что рядом лежали файлы форматов, которые мы не взяли."""
    if not passed_by:
        return
    raw = {e: n for e, n in passed_by.items() if e in RAW_EXT}
    other = {e: n for e, n in passed_by.items() if e not in RAW_EXT}
    top = lambda d: ", ".join(f"{e.lstrip('.')} {n}" for e, n in
                              sorted(d.items(), key=lambda x: -x[1])[:5])
    if raw:
        print(f"Skipped RAW and DNG: {sum(raw.values())} ({top(raw)}). "
              f"The --raw switch includes them.")
    if other:
        print(f"Skipped other formats: {sum(other.values())} ({top(other)}). "
              f"The --all switch includes everything.")


def main():
    ap = argparse.ArgumentParser(description="Scan a photo archive into a database")
    ap.add_argument("roots", nargs="*", help="folders with photos")
    ap.add_argument("--version", action="version", version=f"photostats {VERSION}")
    ap.add_argument("--db", default="photos.db", help="database file (default photos.db)")
    ap.add_argument("--full", action="store_true", help="re-read every file")
    ap.add_argument("--raw", action="store_true",
                    help="include RAW and DNG (cr2, cr3, nef, arw, orf, rw2, raf, dng…)")
    ap.add_argument("--all", action="store_true", dest="all_formats",
                    help="every format: JPEG, PNG, TIFF, HEIC, WebP and RAW")
    ap.add_argument("--ext", help="your own extension list instead of JPEG, e.g. heic,tif")
    ap.add_argument("--workers", type=int, default=4, help="parallel threads")
    ap.add_argument("--min-size", type=int, default=MIN_SIZE // 1024, dest="min_kb",
                    help=f"skip files smaller than this many KB "
                         f"(default {MIN_SIZE // 1024}; 0 keeps everything)")
    ap.add_argument("--colors", action="store_true",
                    help="after EXIF, work out the dominant colour of every photo")
    ap.add_argument("--colors-only", action="store_true", dest="colors_only",
                    help="images only, over an existing database; no folders needed")
    ap.add_argument("--hash", action="store_true",
                    help="also compute a content signature for every file")
    ap.add_argument("--hash-only", action="store_true", dest="hash_only",
                    help="signatures only, over an existing database")
    ap.add_argument("--hash-full", action="store_true", dest="hash_full",
                    help="read whole files for the signature instead of head and tail")
    ap.add_argument("--recolor", action="store_true",
                    help="re-analyse images even where it was already done")
    args = ap.parse_args()
    if args.recolor and not args.roots:
        args.colors_only = True
    if (args.hash_only or args.hash_full) and not args.roots:
        args.hash_only = True
    if not args.roots and not args.colors_only:
        ap.error("give at least one folder (or use --colors-only)")

    if args.hash_only:
        if not os.path.exists(args.db):
            print(f"No database at {args.db}. Scan your folders first.")
            return
        con = open_db(args.db)
        sign_files(con, min((os.cpu_count() or 4) * 2, 12), full=args.hash_full)
        con.close()
        print("\nDone. Now run:  python app.py")
        return

    if args.colors_only:
        if not os.path.exists(args.db):
            print(f"No database at {args.db}. Scan your folders first.")
            return
        con = open_db(args.db)
        analyze_colors(con, find_exiftool()[0], min(os.cpu_count() or 4, 8),
                       reset=args.recolor)
        con.close()
        print("\nDone. Now run:  python app.py")
        return

    con = open_db(args.db)

    row = con.execute("SELECT v FROM meta WHERE k = 'exif_version'").fetchone()
    stored_exif = int(row[0]) if row else 0
    if stored_exif != EXIF_VERSION and not args.full:
        have, = con.execute("SELECT COUNT(*) FROM photos").fetchone()
        if have:
            print(f"The EXIF field set has changed — re-reading metadata of "
                  f"{have} files. Image analysis is kept.")
            args.full = True

    known = {}
    if not args.full:
        for p, s, m in con.execute("SELECT path, size, mtime FROM photos"):
            known[p] = (s, m)


    args.min_size = max(0, args.min_kb) * 1024
    exts = active_exts(args)
    passed_by = Counter()
    print(f"Looking at: {describe_exts(exts)}", flush=True)
    print("Looking for files…", flush=True)
    todo, skipped, gone = [], 0, set(known)
    tiny, tiny_known = 0, []
    for path in walk_photos(args.roots, exts, passed_by):
        gone.discard(path)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_size < args.min_size:
            tiny += 1
            if path in known:
                tiny_known.append(path)   # попал в базу прежним запуском
            continue
        prev = known.get(path)
        if prev and abs(prev[0] - st.st_size) < 1 and abs(prev[1] - st.st_mtime) < 2:
            skipped += 1
            continue
        todo.append((path, st))

    if tiny_known:
        con.executemany("DELETE FROM photos WHERE path = ?",
                        [(p,) for p in tiny_known])
        con.commit()

    # выбывшими считаем только те, которых действительно нет на диске,
    # иначе смена набора форматов вычистила бы из базы ранее собранные RAW
    gone = {p for p in gone if not os.path.exists(p)}
    if gone:
        con.executemany("DELETE FROM photos WHERE path = ?", [(p,) for p in gone])
        con.commit()
        print(f"Dropped from the database (files are gone): {len(gone)}")

    if tiny:
        kb = args.min_size // 1024
        print(f"Skipped {tiny} files under {kb} KB — thumbnails and the like." +
              (f" {len(tiny_known)} of them were dropped from the database."
               if tiny_known else ""))
    print(f"New and changed: {len(todo)}   already in the database: {skipped}")
    report_passed(passed_by)
    if not todo:
        con.close()
        print("Nothing to update.")
        return

    exe, ver = find_exiftool()
    if exe:
        print(f"Reading EXIF with ExifTool {ver} — {exe}", flush=True)
    else:
        print("Reading EXIF with exifread/Pillow.", flush=True)
        if exts & RAW_EXT:
            print("  That is not enough for RAW: brand, lens and focal length will\n"
                  "  usually stay empty. Install ExifTool — winget install "
                  "-e --id OliverBetz.ExifTool", flush=True)

    # Обновляем только колонки EXIF. Прежнее INSERT OR REPLACE переписывало
    # строку целиком и обнуляло разбор изображения — перечитать метаданные
    # значило бы потерять цвет и свет по всему архиву.
    upd = ", ".join(f"{c}=excluded.{c}" for c in COLUMNS if c != "path")
    sql = (f"INSERT INTO photos ({','.join(COLUMNS)}) "
           f"VALUES ({','.join(':' + c for c in COLUMNS)}) "
           f"ON CONFLICT(path) DO UPDATE SET {upd}")

    started = time.time()
    done = 0
    CHUNK = 150

    def flush(rows):
        nonlocal done
        con.executemany(sql, rows)
        con.commit()
        done += len(rows)
        speed = done / max(time.time() - started, 0.001)
        pct = 100 * done / len(todo)
        print(f"\r  processed {done}/{len(todo)}  ({pct:5.1f}%)  "
              f"{speed:.0f} files/s", end="", flush=True)

    if exe:
        chunks = [todo[i:i + CHUNK] for i in range(0, len(todo), CHUNK)]

        def work(chunk):
            tags_by_path = read_chunk_exiftool(exe, [p for p, _ in chunk])
            return [make_row(p, st, tags_by_path.get(p, {})) for p, st in chunk]

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for rows in pool.map(work, chunks):
                flush(rows)
    else:
        with ThreadPoolExecutor(max_workers=args.workers * 2) as pool:
            buf = []
            for (path, st), tags in zip(todo, pool.map(
                    lambda t: read_one_fallback(t[0]), todo)):
                buf.append(make_row(path, st, tags))
                if len(buf) >= CHUNK:
                    flush(buf)
                    buf = []
            if buf:
                flush(buf)

    print()
    if args.hash:
        print()
        sign_files(con, min((os.cpu_count() or 4) * 2, 12), full=args.hash_full)

    if args.colors:
        print()
        analyze_colors(con, exe, min(os.cpu_count() or 4, 8), reset=args.recolor)

    con.execute("INSERT OR REPLACE INTO meta VALUES ('scanned_at', ?)",
                (datetime.now().isoformat(timespec="seconds"),))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('roots', ?)",
                (json.dumps([os.path.abspath(r) for r in args.roots], ensure_ascii=False),))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('exts', ?)",
                (json.dumps(sorted(exts)),))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('exif_version', ?)",
                (str(EXIF_VERSION),))
    con.commit()

    total, = con.execute("SELECT COUNT(*) FROM photos").fetchone()
    no_exif, = con.execute("SELECT COUNT(*) FROM photos WHERE date_src='file'").fetchone()
    yrs = con.execute("SELECT MIN(year), MAX(year) FROM photos").fetchone()
    by_ext = con.execute("SELECT ext, COUNT(*) c FROM photos GROUP BY ext "
                         "ORDER BY c DESC").fetchall()
    pending_color, = con.execute("SELECT COUNT(*) FROM photos "
                                 "WHERE color IS NULL OR brightness IS NULL"
                                 ).fetchone()
    year_limit = datetime.now().year + 1
    odd, = con.execute("SELECT COUNT(*) FROM photos WHERE year < 1990 OR year > ?",
                       (year_limit,)).fetchone()
    con.close()

    print(f"\nDone in {time.time() - started:.0f} s.")
    print(f"In the database: {total} photos, years {yrs[0]}–{yrs[1]}.")
    if len(by_ext) > 1:
        print("By format: " + ", ".join(f"{e} {c}" for e, c in by_ext[:8]))
        print("A frame stored as both RAW and JPEG is counted twice —"
              "\n  the interface can filter by format.")
    if no_exif:
        print(f"{no_exif} files have no EXIF date — the file date was used instead.")
    if odd:
        print(f"{odd} photo{'' if odd == 1 else 's'} dated outside 1990–{year_limit} —\n"
              f"  usually a camera clock that was reset. They stay out of the\n"
              f"  timeline but remain in the database, reachable from a link\n"
              f"  under the timeline card.")
    print(f"Database: {os.path.abspath(args.db)}")
    if pending_color:
        print(f"{pending_color} photos have no image analysis yet "
              f"(colour, brightness, contrast).\n"
              f"  That is a separate pass:  scan.py --colors-only")
    print("\nNow run:  python app.py")


if __name__ == "__main__":
    main()
