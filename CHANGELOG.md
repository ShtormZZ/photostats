# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/).

## [1.13.0] — 2026-08-22

- The launcher explains itself. Steps are numbered, every button and the RAW
  checkbox carry a line saying what they do and roughly what they cost, and a
  line at the top says what to do next — worked out from the database, not from
  a fixed script. The passes also show how much is left rather than "not run
  yet".

## [1.12.1] — 2026-08-22

- "Show all" and "show fewer" in a panel no longer reload anything. Expanding a
  list changes nothing about the selection, but it used to refetch everything
  and reset the selection along with its scrolling and expanded rows.

## [1.12.0] — 2026-08-22

- The centre-colour panel is always shown once images have been analysed,
  instead of appearing only after a dominant colour was picked. It is useful on
  its own — picking a centre colour pulls out frames by their subject.

## [1.11.1] — 2026-08-22

- Fixed: the header and the filter bar still scrolled away. A sticky element
  only holds inside its parent's box, and `body` had `height: 100%`, so that box
  ended at the first screenful. The smoke test now guards the rule.

## [1.11.0] — 2026-08-22

- Forest litter no longer comes out red. Dark, muted reddish tones at 345–8°
  now count as brown; wine and dark brick keep their red thanks to a purity
  ceiling on that stretch of the arc. A frame of woodland floor was reported as
  red because those pixels edged out yellow by one point inside the warm family
  and gave it their name. Archives re-analyse themselves on the next image pass.

## [1.10.0] — 2026-08-22

- Only `.jpg` and `.jpeg` are read by default. `.jpe` and `.jfif` are still
  JPEG but are how browsers save pictures, so they moved behind `--ext`.
- Files under 50 KB are skipped as thumbnails, and any already in the database
  are dropped. `--min-size` changes or disables the threshold.

## [1.9.0] — 2026-08-22

- Several values can be picked at once in the When group: months, hours, years
  and months of the timeline. Shift-click selects a range.
- The filter bar is pinned under the header instead of scrolling away.
- The selection list sorts by capture time, file name, file size, camera or
  folder, in either direction.
- The selection can be shown as the folders the photos live in, with counts and
  date ranges; clicking one narrows the selection to that folder.

## [1.8.0] — 2026-08-22

- One `photostats.bat` replaces the five batch files. It finds Python, says
  where to get it if it is missing, sets up the environment on first run and
  opens a small window with the three passes and the interface button.
- Passes and the server now genuinely run at the same time. The database gets a
  busy timeout and WAL where the filesystem allows it; without them roughly half
  the server's queries failed with "database is locked" while a scan was
  running — 891 read errors in a three-second test, now zero.
- Stopping a pass from the window interrupts it the way Ctrl+C does, so the
  batch it has not committed is not lost. Closing the window asks first if
  anything is still running.
- Folders and the RAW setting are remembered in `settings.json`, which is kept
  out of git — it holds your paths.

## [1.7.0] — 2026-08-22

- Licence is now the GNU AGPL v3, with commercial licensing available — see
  COMMERCIAL.md. This replaces the brief move to PolyForm Noncommercial: the
  AGPL is real open source, recognised by OSI and FSF and by GitHub, while still
  giving companies a reason to buy a licence rather than build on it silently.
- The interface header carries a source link, as section 13 of the AGPL expects
  from anything reachable over a network.
- `tools/check_license.py` verifies that LICENSE holds the canonical AGPL text;
  CI runs it on every push.

## [1.6.1] — 2026-08-22

- Licence changed from MIT to the PolyForm Noncommercial License 1.0.0.
  Noncommercial use stays free; commercial use needs a licence — see
  COMMERCIAL.md. Note that this makes the project source-available rather than
  open source.

## [1.6.0] — 2026-08-22

- Neighbouring hue groups now merge when the subject runs across the boundary
  between them: a graded sky no longer splits into cyan and blue and lose to a
  smaller patch of something else. Merging only happens where pixels form a
  continuous band across the boundary, so separate patches of red and purple
  stay separate. The boundaries around green are excluded on purpose — that is
  where one subject usually meets another. Archives re-analyse themselves on the
  next image pass.

## [1.5.0] — 2026-08-22

- The photo card now shows the centre colour. It was only ever visible in the
  expanded list row.
- Brown, orange and yellow now compete as one family when the dominant colour is
  picked, and the winner is named after its largest member. They are separated
  by brightness and purity rather than hue, so that split should not decide which
  colour wins: autumn leaves used to scatter into three groups and lose to solid
  green. Archives re-analyse themselves on the next image pass.

## [1.4.0] — 2026-08-22

- Brown is now only a dark warm tone: wood, bark, chocolate, earth. Wheat,
  straw, sand and sepia used to be called brown and are now yellow, which is
  how they read — light and muted warm is golden, not brown. Bright, pure warm
  tones stay orange. Archives re-analyse themselves on the next image pass.

## [1.3.0] — 2026-08-22

- Olive is now green, not yellow. Sunlit foliage sits at the hue of a lemon and
  was making green frames come out yellow; purity times brightness separates
  the two. Archives re-analyse themselves on the next image pass.

## [1.2.1] — 2026-08-22

- Fixed: the selection list took about forty seconds on a 40,000 photo archive
  and looked empty. Counting the size of a duplicate group scanned the whole
  table for every row on screen; an index on the key expression brings that to
  80 ms.
- The selection pane now says when a request fails instead of showing nothing.

## [1.2.0] — 2026-08-22

- Duplicates now match on capture time as well as name and size. Two different
  frames can share a name and an exact byte count — the second of capture tells
  them apart, and no re-scan is needed since the time was already stored.
- New optional pass `scan.py --hash-only` stores a content signature (size plus
  the first and last 64 KB). Once every file has one the duplicate panel uses
  it, which also finds copies that were renamed. `--hash-full` reads every byte.
- The panel says which rule it is using.

## [1.1.1] — 2026-08-22

- Duplicates are easier to read: the list marks group boundaries, a name with
  copies gets `×N`, and the expanded row shows the exact byte count. Sizes were
  always compared byte for byte, but rounded megabytes on screen made two
  different files look like a wrong match.

## [1.1.0] — 2026-08-22

- Duplicate detection: a new metric with *no duplicates* / *has duplicates*.
- Shooting timeline: the strip and the year axis now share one width. They used
  to drift apart on large archives, and the later years fell off the strip.

## [1.0.0] — 2026-08-22

First public release.

### Scanning

- Walks folders, reads EXIF, stores everything in a single SQLite file.
- JPEG by default; `--raw` adds RAW and DNG, `--all` every supported format,
  `--ext` a custom list. The scanner reports how many files of other formats
  were left out.
- Incremental: only new and changed files are re-read. Rows are updated in
  place, so re-reading metadata never destroys the image analysis.
- ExifTool is used when available (RAW support, maker-specific lens tags),
  otherwise exifread and Pillow.
- Optional image analysis pass (`--colors`, `--colors-only`): dominant colour,
  colour of the central half of the frame, brightness, contrast, chroma and
  clipping. Resumable — Ctrl+C keeps whatever is done.
- Analysis rules carry a version: when they change, the archive is
  re-analysed automatically.

### Interface

- Local web interface, no build step, no external dependencies at startup.
- Panels grouped into When, EXIF metrics, Image metrics and Where; each group
  collapses.
- Cross-filtering: every panel filters the selection, and each panel keeps
  showing its own dimension in full so there is always somewhere to go next.
- Shooting timeline as a film strip; months, hours, camera brands, models,
  lenses, focal lengths, aperture, shutter, ISO, file size and format.
- Camera and lens over time: appears once a camera or lens is picked, with
  first and last frame, days in service and a monthly strip.
- Dominant colour in eleven groups plus `mixed`, with a sub-panel for the
  colour in the centre of the frame — a black cat on a white sheet is white
  dominant, black centre.
- Places: coordinate clusters computed server-side, with an optional map.
- Selection pane: ten random photos, then a text list where any row expands to
  a thumbnail and a summary.
- Duplicate detection by file name and size, case-insensitive; selecting them
  sorts the list by name so copies sit together.
- Current selection exports to CSV.

### Privacy

- Nothing leaves the machine. The map is the only outbound request, it is
  opt-in behind a button, and the reason is spelled out before you press it.
