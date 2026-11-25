# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
# SPDX-License-Identifier: AGPL-3.0-only
"""Compare planner maintenance on one disposable copy; never write the source.

python tools/db_benchmark.py --source photos.db --output docs/db-benchmark.local.json
python tools/db_benchmark.py --rows 100000 --distribution unrated --output report.json
"""

import argparse
import datetime
import gc
import hashlib
import json
import math
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import db_schema as schema
import raw_pairs

DISTRIBUTIONS = ("unrated", "bad", "great", "sparse", "large-day")


def synthetic(con, rows, distribution, seed):
    schema.ensure_schema(con)
    rng = random.Random(seed)
    origin = datetime.datetime(2020, 1, 1)
    def records():
        for i in range(rows):
            day = (i // 100) % 365
            if distribution == "sparse":
                day *= 13
            if distribution == "large-day" and i < rows // 2:
                day = 366
            taken = origin + datetime.timedelta(days=day, seconds=i % 86400)
            chance = rng.random()
            if distribution == "bad":
                mark = "bad" if chance < 0.99 else None
            elif distribution == "great":
                mark = "great" if chance < 0.7 else None
            else:
                mark = None if chance < 0.99 else rng.choice(["bad", "great", "5", "4", "2"])
            rating = None if distribution == "unrated" or chance < 0.8 else i % 6 or None
            yield (f"fixture/{i // 1000}/Фото-{i}.jpg", f"fixture/{i // 1000}", f"Фото-{i}.jpg",
                   "jpg", 100000 + i * 97, taken.isoformat(sep=" "), taken.year, taken.month,
                   taken.strftime("%Y-%m"), taken.day, taken.hour, taken.weekday(), "exif",
                   "Fixture", f"Camera {i % 3}", f"Lens {i % 4}", 50, 75, 100, 2.8,
                   "1/125", 0.008, 4000, 3000, mark, rating,
                   "green", 0.8, 0.5, 0.1, 0.6, 12, 8, raw_pairs.stem(f"Фото-{i}.jpg"))
    columns = ("path,folder,filename,ext,size,taken,year,month,ym,day,hour,weekday,date_src,"
               "brand,camera,lens,focal,focal35,iso,fnumber,shutter,exposure,width,height,"
               "mark,rating,color,color_share,brightness,contrast,chroma,sharp_center,sharp_edge,pair_stem")
    with con:
        con.executemany(f"INSERT INTO photos({columns}) VALUES ({','.join('?' for _ in columns.split(','))})",
                        records())


def scenarios(con):
    day = con.execute("SELECT date(taken) FROM visible_photos WHERE date(taken) IS NOT NULL "
                      "GROUP BY 1 ORDER BY COUNT(*) DESC, 1 LIMIT 1").fetchone()
    day = day[0] if day else "2020-01-01"
    return {
        "statistics": "/api/stats",
        "statistics_day": f"/api/stats?day={day}",
        "directories": "/api/directories",
        "timeline": "/api/timeline/photos?limit=8&seed=database-benchmark",
        "timeline_day": f"/api/timeline/photos?day={day}&seed=database-benchmark",
        "expanded_day": f"/api/timeline/day?day={day}&limit=200",
        "photos": "/api/photos?limit=120",
        "random": "/api/random?n=12",
    }, day


def plan(con):
    key = schema.rating_key()
    return [row[3] for row in con.execute(
        f"EXPLAIN QUERY PLAN SELECT taken FROM visible_photos WHERE taken >= ? AND taken < ? "
        f"AND date(taken) IS NOT NULL AND ({key} IS NULL OR {key} IN ('great','5','4')) "
        "ORDER BY taken DESC LIMIT 1", ("0001-01-01", "9999-12-32"))]


def response_digest(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=True, sort_keys=True).encode()).hexdigest()


def measure(path, urls, warmups, repeats):
    results = {}
    for name, url in urls.items():
        counter = {"selects": 0, "connections": 0}
        def trace(sql):
            if sql.lstrip().upper().startswith(("SELECT", "WITH")):
                counter["selects"] += 1
        def connect():
            counter["connections"] += 1
            # Match the real request connection, including its WAL/timeout setup.
            con = schema.connect(path)
            con.row_factory = sqlite3.Row
            con.set_trace_callback(trace)
            return con
        with patch.object(app, "db", connect), app.app.test_client() as client:
            elapsed = []
            for i in range(warmups + repeats):
                counter.update(selects=0, connections=0)
                start = time.perf_counter()
                response = client.get(url)
                duration = (time.perf_counter() - start) * 1000
                if response.status_code != 200:
                    raise RuntimeError(f"{name} returned HTTP {response.status_code}")
                if i >= warmups:
                    elapsed.append(duration)
            data = response.get_json()
            results[name] = {"median_ms": round(statistics.median(elapsed), 3),
                             "p95_ms": round(sorted(elapsed)[math.ceil(len(elapsed) * 0.95) - 1], 3),
                             "selects": counter["selects"], "connections": counter["connections"],
                             "response_bytes": len(response.data),
                             "digest": response_digest(data) if name != "random" else None}
        print(f"  {name}: {results[name]['median_ms']} ms", flush=True)
    return results


def run(path, warmups, repeats, source, distribution, seed):
    # Source catalogues may predate visible_photos. Migrate only the disposable
    # copy before measuring requests; planner maintenance stays a separate step.
    with closing(schema.open_database(path, maintenance=False)) as con:
        urls, _ = scenarios(con)
        rows = con.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        before_plan = plan(con)
    print("Before maintenance", flush=True)
    before = measure(path, urls, warmups, repeats)
    with closing(schema.open_database(path, maintenance=False)) as con:
        maintenance = schema.maintain_statistics(con, force=True)
        after_plan = plan(con)
    print("After maintenance", flush=True)
    after = measure(path, urls, warmups, repeats)
    comparisons = {}
    for name in urls:
        ratio = after[name]["median_ms"] / max(before[name]["median_ms"], 0.001)
        comparisons[name] = {"median_ratio": round(ratio, 4),
                             "over_10_percent": ratio > 1.1,
                             "same_response": (before[name]["digest"] == after[name]["digest"]
                                               if name != "random" else None)}
    return {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "platform": platform.system(), "source": "backup copy" if source else "synthetic",
            "distribution": distribution if not source else None, "rows": rows, "seed": seed,
            "warmups": warmups, "repeats": repeats,
            "before_plan": before_plan, "after_plan": after_plan,
            "maintenance": maintenance, "before": before, "after": after,
            "comparison": comparisons}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--distribution", choices=(*DISTRIBUTIONS, "all"), default="unrated")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rows < 1 or args.warmups < 0 or args.repeats < 1:
        parser.error("rows/repeats must be positive and warmups nonnegative")
    reports = []
    distributions = DISTRIBUTIONS if args.distribution == "all" and not args.source else (args.distribution,)
    for distribution in distributions:
        print(f"Distribution: {distribution}" if not args.source else "Read-only source copy", flush=True)
        with tempfile.TemporaryDirectory(prefix="photostats-db-bench-") as folder:
            path = Path(folder) / "benchmark.db"
            with closing(sqlite3.connect(path)) as target:
                if args.source:
                    with closing(sqlite3.connect(args.source.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                        source.backup(target)
                else:
                    synthetic(target, args.rows, distribution, args.seed)
            report = run(path, args.warmups, args.repeats, args.source, distribution, args.seed)
            reports.append(report)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            output = {"runs": reports} if len(distributions) > 1 else report
            args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
            gc.collect()
    return int(any(item["same_response"] is False for report in reports for item in report["comparison"].values()))


if __name__ == "__main__":
    sys.exit(main())
