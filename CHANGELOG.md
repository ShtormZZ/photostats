# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

- Comments on photos. Any photo takes one line of your own text, up to 256
  characters counted as characters — Cyrillic and emoji fit as well as Latin.
  It is written and read in full in the photo card, under the tags; in the list
  and on the thumbnails a comment shows only as an icon, with the text on hover.
  There is no panel or filter for comments, by design. They are stored in a new
  `comment` column of `photos` that, like `mark`, no scan ever writes, and they
  come out in the CSV export as a column of their own.

- Grey survives daylight, and the centre of the frame is the subject rather than
  the room. `ALGO_VERSION` goes to 14, so the next scan re-analyses the archive;
  on 400 random frames the dominant colour changes for 10% and the centre colour
  for 20%.
  - **A pixel is achromatic below 0.14 saturation, not 0.10.** The old line sat
    under the tint daylight itself leaves on a neutral surface — grey paving in
    October sun measures a median of 0.12 — so half of it counted as coloured
    and a grey cat on grey stone came out yellow. Grey, black and white together
    go from 14% of the archive to 20%. Not higher than 0.14: by 0.18 it is 26%,
    and frames that are merely muted start being counted as black and white.
  - **The centre region is a quarter of the frame area, not half** — the middle
    half in each direction. Half the area is still mostly the room: a baby in a
    white hat in a dark restaurant scored 37% black against 32% white there, and
    the background won the one measure meant for the subject. A subject placed
    off centre now falls outside the square, which is the price of the change.

- The selection exports as a bare list of paths — *export paths as CSV* in the
  selection panel. One full path per line under a `Path` header, sorted by path,
  following whatever is filtered, so `Import-Csv` reads it without arguments and
  a script can copy what you rated 5+ or delete what you marked bad. The
  separator is a comma rather than the semicolon of the other export: this file
  is one column read by a program, not a table read by Excel, and a folder named
  "Photos, 2019" only comes through as one field when the comma is the separator
  that quotes it.
  The program still never moves, renames or deletes a photo of its own accord.
  It hands over the list; what runs next is the decision of whoever runs it.

- Warm colours are named more like the eye names them. Three rules changed, and
  `ALGO_VERSION` goes to 13 with them, so the next scan re-analyses the archive.
  Brown was the commonest colour in an archive of 105 000 photos and is now
  fourth: on 700 random frames it falls from 153 to 93, orange rises from 72 to
  109 and yellow from 54 to 70.
  - **A merged family is named by its own average colour** rather than by the
    group in it with the most pixels. A golden maple was 43% brown against 28%
    orange and 21% yellow — its shaded half outnumbered each of the lit halves
    separately — so a plurality of 43% named the whole 92%. The average is the
    swatch shown beside the name, so a frame can no longer be called brown while
    displaying an ochre chip. Where the average falls outside the family,
    counting decides as before: the name must be a colour the frame really has.
  - **Brown ends at half brightness, not at 0.62.** The old line meant a
    brightest channel of 158, and rgb(158, 102, 43) is ochre, not chocolate. It
    made brown 30% of the archive; at 0.55 it was still 22%, at 0.50 it is 13%.
    What brown gives up divides between the two colours the eye would have used
    anyway: muted light tones read as gold, vivid ones as orange. It does not go
    lower — at 0.45 brown is 5% and orange inherits everything, tans included,
    which only moves the crowding from one name to another.
  - **Orange gives way to yellow at 40°, not 45°.** A sunlit autumn crown
    measures 35 to 50° at nearly full purity and reads as yellow all through;
    below 40° are the hues nobody calls yellow — skin, terracotta, a ripe orange.
  The purity ceiling that separates gold from a ripe orange is untouched, and so
  is the brightness below which a warm tone is not a colour at all.

- The photo card flips through the selection: arrows over the edges of the
  picture, or the ← and → keys, with no going back to the list in between. The
  set flipped through is the one the card was opened from — the sorted list from
  a row, the ten random frames from the preview strip — so a thumbnail never
  drops you somewhere else in the archive. Walking past the end of what the list
  has loaded fetches the next page instead of stopping at the three hundredth
  photo of six thousand, and at the ends of the selection the arrow with nowhere
  to go is hidden rather than dimmed. Both work in the whole-window view, which
  keeps the sharper copy as you go; the arrow keys stay out of it while a tag is
  being typed, where they belong to the text.

- Photos take tags: up to three a photo, one word each, letters and digits of
  any alphabet, stored in lower case so `Sea`, `SEA` and `sea` stay one tag. The
  field sits under the rating in an expanded row and in the photo card, and
  offers the tags already in the archive as you type, with the number of photos
  on each — from the whole archive rather than the current selection, or a tag
  used elsewhere would not be offered and the same word would be typed in twice.
  A panel of its own counts them; picking several shows the photos carrying any
  of them. Photos with no tag are not a row there — they are the bulk of an
  archive being tagged, and a bar for them would leave every real tag too short
  to compare. The list has a tags column, and so does the CSV export.
  Tags live in a `photo_tags` table the scanner never writes to, so no pass can
  wipe them, and a trigger clears them when a photo is dropped from the database.
  Case is folded in Python, which folds Cyrillic — SQLite's own `lower()` does
  not — and spellings are composed to NFC, so the two shapes of "й" cannot
  become two tags.

- The photo card opens to the whole browser window on a double-click of the
  picture: card, margins and metadata all step aside. Escape backs out of it one
  step at a time rather than closing everything at once. A sharper copy is
  fetched to match — the thumbnail ceiling went from 1400 to 2800 pixels, which
  is what a full window on a dense display actually needs, and the bigger copy
  only replaces the one on screen once it has arrived, so nothing blinks.

- Photos have a rating. Stars are read from the file's own EXIF (`Rating`, the
  tag a camera writes and Explorer shows — not `xmp:Rating`, so Lightroom's own
  stars stay invisible), and you can rate photos in the interface on top of that:
  one to five stars, a red ✗ for a bad frame, or 5+ for the best. One of the three
  at a time, and clicking what is set clears it, which hands the photo back to
  whatever EXIF said. A group of its own lists the scale — the ratings given,
  not the unrated majority, which set the scale for every other bar — the
  selection can be sorted by it, and the control sits both in an expanded row and
  over a thumbnail in the preview strip.
  Ratings you give live in the database and nowhere else; no file is ever written
  to. They are kept in a `mark` column the scanner does not own, so re-reading
  metadata — which this release does, `EXIF_VERSION` having changed — cannot wipe
  them.

- Comments and docstrings across the program are in English. They were Russian in
  `app.py`, `scan.py`, `launcher.py`, `dupkey.py`, `web/index.html`, the batch file
  and `.gitignore`, which put half the reasoning in the code out of reach of anyone
  who does not read it. No code changed: the colour names in `COLOR_RENAME` and the
  Cyrillic seconds suffix in `rstrip` are data and stayed as they were.

- A folder can be checked against the archive before it is added: **Check a
  folder** in the window, or `scan.py --check-dups FOLDER`. It reports how many
  of the files are copies of what you already have and how many are new. The
  database is opened read-only, so the check cannot write to it; files that are
  in the archive at that very path are counted apart from copies, since they are
  the archive rather than duplicates of it. The rule is the one the duplicate
  panel uses, and both now read it from one place — `dupkey.py`.
- Fixed: `scan.py --hash-only` refused to start, and with it the **Sign files**
  button. The argument check demanded a folder from a pass that works off the
  database and takes none.
- Fixed: stopping the folder check from the window printed nothing. Windows
  sends CTRL_BREAK to a stopped task, which Python ends the process on rather
  than raising KeyboardInterrupt, so the summary never got out.
- Fixed: the map in **Places** painted over the photo card, so opening a photo
  while the map was shown left the map on top of the picture. Leaflet stacks its
  own tiles, controls and attribution far higher than anything in the interface,
  and none of it stayed inside the map's box.

- The program has an icon: viewfinder corners around three bars, in the same
  palette as the interface. It sits on the launcher window, in the taskbar, on
  the browser tab and to the left of the name in the header. At 16 and 24 pixels
  a separate drawing is used — just the bars, no frame, because at that size the
  two run together. `tools/make_icon.py` redraws all of it.

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
