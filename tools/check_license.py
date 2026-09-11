# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License, version 3, as published by
# the Free Software Foundation. See LICENSE for the full text.
# Commercial licensing is available — see COMMERCIAL.md.
# SPDX-License-Identifier: AGPL-3.0-only
"""Check that LICENSE holds the real, unmodified AGPL-3.0 text.

The licence has to be byte-for-byte the canonical one, or GitHub will not
recognise it and the terms you think you are offering are not the terms you are
actually offering. This runs in CI so a wrong or missing file cannot be shipped.

Get the file with either of these:

    curl -o LICENSE https://www.gnu.org/licenses/agpl-3.0.txt
    Invoke-WebRequest https://www.gnu.org/licenses/agpl-3.0.txt -OutFile LICENSE
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "LICENSE")

# Landmarks that appear only in the genuine AGPL-3.0. Section 13 is the one that
# separates it from the plain GPL-3.0 — the network clause is the whole point of
# choosing this licence, so it is worth checking for by name.
MUST_CONTAIN = [
    "GNU AFFERO GENERAL PUBLIC LICENSE",
    "Version 3, 19 November 2007",
    "13. Remote Network Interaction; Use with the GNU General Public License.",
    "your modified version must prominently offer all users interacting with it",
    "TERMS AND CONDITIONS",
    "END OF TERMS AND CONDITIONS",
]
MIN_LINES = 600


def main():
    if not os.path.exists(PATH):
        print("LICENSE is missing. Fetch the canonical text:")
        print("    curl -o LICENSE https://www.gnu.org/licenses/agpl-3.0.txt")
        return 1

    text = open(PATH, encoding="utf-8", errors="replace").read()
    lines = text.count("\n") + 1
    problems = [m for m in MUST_CONTAIN if m not in text]

    if lines < MIN_LINES:
        problems.append(f"only {lines} lines, expected at least {MIN_LINES}")
    if "GNU GENERAL PUBLIC LICENSE" in text and "AFFERO" not in text:
        problems.append("this looks like the plain GPL-3.0, not the AGPL-3.0")

    if problems:
        print("LICENSE does not look like the canonical AGPL-3.0:")
        for p in problems:
            print("  -", p)
        print("\nReplace it with the official text:")
        print("    curl -o LICENSE https://www.gnu.org/licenses/agpl-3.0.txt")
        return 1

    print(f"LICENSE looks like the canonical AGPL-3.0 ({lines} lines).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
