# photostats

Statistics for a personal photo archive. Point it at your folders, and it reads
the EXIF of every photo into a small SQLite file, then shows you what is in
there: when you shot, with what, at what settings, in what colours and where.
Click any bar to see the photos behind it.

Everything runs on your own machine. Nothing is uploaded anywhere.

*(Русская версия документации: [README.ru.md](README.ru.md))*

<!-- Add a screenshot here: the interface is the selling point, and people
     will not read this far without one. Note that a screenshot of your real
     archive shows your file paths and, on the map, where you live. -->

## Requirements

- Windows, macOS or Linux
- Python 3.10 or newer, with Tk (the standard installer includes it)

## Install and run

Double-click **photostats.bat**. It finds Python, sets up everything it needs on
first run, and opens a small window with the three passes and the interface
button. Nothing else to install by hand.

If Python is missing the window will not open — the file tells you so and prints
the download link. Get it from
[python.org](https://www.python.org/downloads/windows/), tick *Add python.exe to
PATH*, and leave *tcl/tk and IDLE* on: the window is built on it.

Optional but recommended: [ExifTool](https://exiftool.org), needed for RAW files
and for maker-specific lens tags nothing else reads.

```
winget install -e --id OliverBetz.ExifTool     # Windows
brew install exiftool                          # macOS
```

If you install it by hand from exiftool.org, keep the `exiftool_files` folder
next to `exiftool.exe` — moving the exe alone is the classic mistake.

### The window

The steps are numbered, each button says underneath what it does, and a line at
the top tells you what to do next based on what is already in the database — so
you never have to work out which button is the right one.

Add your photo folders once and they are remembered. Then:

- **Read EXIF** — fast, and all the interface needs to be useful. Tick *include
  RAW and DNG* first if you want them; JPEG only by default, because RAW usually
  sits next to a JPEG of the same frame and would count twice.
- **Analyse images** — colour, brightness, contrast, clipping. Much slower: it
  decodes every file.
- **Sign files** — content signatures for exact duplicate matching.
- **Check a folder** — pick a folder you have *not* scanned and it says how much
  of it you already have. See below.
- **Open interface** — starts the server and opens the browser.

**Passes and the server run at the same time.** You can watch the statistics
fill in while the analysis is still going. Stopping a pass keeps what it has
already done and it carries on from there next time, so a long analysis can be
done in several sittings.

### From the command line

The window is a convenience, not a replacement. Everything is still available
directly, which is what CI uses:

```
python scan.py "D:\Photos" "E:\Archive 2001-2010"    # EXIF
python scan.py --colors-only                           # images
python scan.py --hash-only                             # signatures
python scan.py --check-dups "E:\Card"                  # check, no writing
python app.py                                          # interface
```

JPEG only by default. Add formats when you want them:

| Switch | Reads |
|---|---|
| *(none)* | `jpg`, `jpeg` |
| `--raw` | plus RAW and DNG: `cr2`, `cr3`, `nef`, `arw`, `orf`, `rw2`, `raf`, `dng`… |
| `--all` | plus `png`, `tif`, `heic`, `webp`, `bmp`, `gif` |
| `--ext heic,tif` | your own list instead of JPEG |

Files smaller than 50 KB are left out: at that size they are thumbnails,
avatars and pictures saved out of chat apps, not photographs. They would drag
down the file-size distribution and invent duplicates, since identical
thumbnails are everywhere. `--min-size 0` keeps everything, `--min-size 200`
raises the bar. Anything of that size already in the database from an earlier
scan is dropped, and the scanner says how many.

`.jpe` and `.jfif` are JPEG too, but that is how browsers save pictures rather
than how cameras name them, so they are off by default — `--ext jpg,jpeg,jfif`
brings them back.

Re-running only reads new and changed files.

## What it shows

Panels are grouped, and each group folds away.

**When** — a film-strip timeline of the whole archive, months of the year,
time of day with day and night on separate tracks.

**EXIF metrics** — camera brands, models, lenses, focal lengths, aperture,
shutter speed, ISO, file size, format and duplicates. Pick a camera or a lens and a second
panel appears: when it arrived, when the last frame was taken, how many days it
was in service, and a monthly strip of how much of each month it carried.

**Image metrics** — dominant colour in eleven groups plus `mixed`, the colour
in the centre of the frame, brightness, contrast, colour or black & white, and
clipping in highlights and shadows. These need the image analysis pass.

**Where** — clusters of coordinates from EXIF, as a list and, if you ask for
it, on a map.

Duplicates are found by name, exact byte size **and capture time**. Name and
size alone are not enough: one camera repeats file names, and two different
JPEGs land on the same byte count more often than you would expect — but two
different frames never share the same second of capture, while copies of one
file always do. Only a real EXIF time counts; where there is none the file date
stands in, and a copy has its own, so those fall back to name and size.

Pick *has duplicates* and the list sorts by name instead of date, so copies sit
next to each other and you can see which path to keep. It is worked out in the
query, so it stays right as files come and go, with no re-scan.

Sizes are compared byte for byte, never rounded. Watch out for the trap this
creates on screen: two different photos can share a name and both round to the
same "4.5 MB" while differing by tens of kilobytes, and sorting by name puts
them side by side. So the list marks where one group ends and the next begins,
shows `×N` next to a name that has copies, and the expanded row gives the exact
byte count next to the megabytes.

### A stricter answer

```
python scan.py --hash-only
```

This walks the files and stores a content signature: the size plus the first
and last 64 KB, hashed. Once every photo has one, the duplicate panel switches
to it and says so. A signature answers the question outright and finds copies
that were renamed, which name matching never can.

Reading the whole of a 170 GB archive would take hours; head and tail bring
that down to a few gigabytes and a few minutes. Two files with the same size
and the same first and last 64 KB differ only in a deliberately built case.
If that is not good enough, `--hash-full` reads every byte.

The pass is resumable, and signatures are only used once every file has one —
mixing two rules in one key would leave a signed copy unable to find an
unsigned one.

Whichever rule is in use, look before you delete: the list gives you the full
paths of every copy.

### Checking a folder before you add it

The duplicate panel answers the question for photos that are already in the
archive. The other version of that question comes up earlier — a memory card, a
disk someone handed you, a folder called `photos_backup_final`: is any of this
new, or do I have it all already?

```
python scan.py --check-dups "E:\Card"
```

Or press **Check a folder** in the window and pick it. Every file in the folder
is measured by the same rule the interface uses, looked up in the archive, and
counted. At the end you get the summary:

```
Checked 812 files in 41 s.
  duplicates: 763
    749 already in the archive, 14 repeated inside the folder itself
  new:        49

Nothing was written to the database.
```

**Nothing is written.** The database is opened read-only, so the pass cannot add
to it even by mistake — that is the point of having it. Decide from the numbers,
then scan the folder for real, or don't.

Files that are in the archive *at that very path* are counted apart, as `already
scanned`, and are neither duplicates nor new: they are not copies of your
archive, they **are** your archive. You see this line when you point the check
at a folder you scanned earlier.

The rule is the one the interface is using at that moment, and the pass says
which. With signatures it reads 128 KB per file and finds renamed copies; on
name, size and capture time it has to read the EXIF of every file, so it is
slower and a renamed copy gets past it. That is the same trade the duplicate
panel makes — `--hash-only` first, and the check gets strict too.

Filters combine. Pick Canon and the lens panel narrows to Canon lenses; add
July and you get July on Canon. Every panel keeps showing its own dimension in
full, so there is always somewhere to go next.

In the **When** group you can pick several values at once: click September, then
October, and both are selected. Shift-click takes everything between the last
pick and this one — September through December in two clicks. Clicking a lone
selection again clears it. Everywhere else a click still replaces the selection,
since two cameras or two colours at once rarely help.

The filter bar stays pinned under the header, so dropping a filter never means
scrolling back to the top.

On the right is the current selection: ten random photos from it, then a text
list where any row expands into a thumbnail and a summary with buttons to open
the file or show it in the file manager. The selection exports to CSV.

Sort that list by capture time, file name, file size, camera or folder, either
way round. Or switch it to **folders** — the folders those photos live in, with
a count and a date range each. Click one to narrow the selection to it. That is
usually the faster way to find where a batch came from than paging through the
files themselves.

## How some of it is worked out

**Colour is a vote, not an average.** The frame is scaled to 48×48, every pixel
is assigned to a group, and the largest group wins. Averaging RGB over a photo
always gives mud — a colour that is nowhere in the frame. Achromatic groups
compete separately: if at least a quarter of the pixels have colour, grey sky
and black background stay out of the contest, otherwise most of an archive
comes out grey.

**Dark pixels are judged more strictly.** Dark brown formally has a reddish
hue, but the eye reads it as shadow. Below 0.28 brightness a pixel only counts
as coloured when its purity is at least 0.80, so a dark red stays red and a
dark brown becomes black.

**Warm tones split three ways by purity and brightness.** They all share the
hue of an orange, so hue alone cannot separate them:

- dark warm is **brown** — wood, bark, chocolate, earth. The arc reaches a
  little past red, because leaf litter and rotting leaves sit there; a purity
  ceiling on that stretch keeps wine and dark brick red;
- light and muted warm is **yellow** — wheat, straw, sand, sepia, which read as
  golden rather than orange;
- pure and bright stays **orange** — a ripe orange, a sunset, autumn leaves.

Skin sits at a redder hue than straw and stays orange.

**Olive counts as green.** Summer foliage in sunlight sits at 60–68° — the hue
of a lemon — so hue alone cannot tell them apart. Purity times brightness can:
around 0.2 for leaves, near 1.0 for a lemon. Below 0.30 in that band the colour
is olive, and the eye reads olive as green. Without this a forest came out
yellow.

**Neighbouring hues merge when the subject runs across the boundary.** A deep
sky graded from 206° to 219° splits into cyan and blue and can lose to grass,
although there is twice as much sky. Two neighbouring groups are therefore
counted as one colour when their pixels form a continuous band — that is, when
there are pixels on both sides of the boundary. Separate patches of red and
purple flowers stay separate, because nothing sits on the boundary between them.

The boundaries around green are deliberately left out of this. That is exactly
where one subject usually ends and another begins: golden leaves against moss,
tree crowns against sky or water. Merging there would erase what the metric is
for — an autumn frame would come out green again.

**Warm groups compete as one family.** Brown, orange and yellow are split from
each other by brightness and purity, not by hue, so that split should not decide
which colour wins. An autumn frame would otherwise lose to a solid green: the
leaves scatter into 14% orange, 12% yellow and 8% brown against 30% moss. The
family is named after its largest member.

**The centre is half the frame area,** cut from the middle. The dominant colour
mostly describes the background and the centre one the subject, which is why
they are more useful together: a black cat on a white sheet is white dominant,
black centre. Both panels are always on, so you can start from either end:
pick the centre colour alone to pull out every frame with, say, something dark
in the middle.

**A frame with no dominant colour is `mixed`** — when the largest group holds
less than a quarter of the frame.

**Timeline bars are shares, not counts.** Otherwise a quiet year would look
like the camera was in a drawer.

Thresholds live at the top of `scan.py` and `app.py` and are commented. Those
in `app.py` apply in the query, so changing them needs no re-analysis. Those in
`scan.py` do — bump `ALGO_VERSION` after editing and the archive re-analyses
itself on the next pass.

## Privacy

This matters more than usual for a photo archive, so it is worth being precise.

- The database holds full file paths, shooting coordinates and everything
  derived from your photos. `.gitignore` keeps it out of git — do not defeat
  that.
- The cache folder holds thumbnails, which are pieces of your photos.
- The server binds to `127.0.0.1`.
- **The map is the only outbound request the program ever makes.** It is behind
  a button, and pressing it fetches Leaflet from unpkg.com and tiles from
  openstreetmap.org. From those tile requests their servers can tell which
  areas you are looking at. The list of places, with the same clusters and the
  same filtering, works offline — you never have to press it.

## The database

One SQLite file, one flat table, no ORM:

```
taken, year, month, ym, hour, weekday, brand, camera, lens, focal, focal35,
iso, fnumber, shutter, exposure, width, height, size, lat, lon,
color, color_hex, color_share, color_center, color_center_hex,
color_center_share, brightness, contrast, chroma, clip_hi, clip_lo, path
```

If you want a metric the interface does not have, it is usually one SQL query
away. Old databases upgrade themselves: new columns are added, colour names
were translated in place, and metadata is re-read without touching the image
analysis.

## When something looks wrong

**Photos piled into one month.** They have no `DateTimeOriginal`, so the file
date was used. The scanner counts them; the photo card marks such dates.

**A stray year like 1970 or 2052.** A camera clock that was reset. Those frames
stay out of the timeline but not out of the database — a line under the
timeline offers to show them.

**No colours.** The image analysis pass has not been run: `scan-colors.bat` or
`python scan.py --colors-only`.

**No colours for RAW.** Pillow cannot read RAW pixels; install ExifTool and the
embedded preview is used instead.

**Twice as many frames as expected.** The archive was scanned with `--raw` and
shot as RAW+JPEG. Filter by format, or rebuild with `--full` without `--raw`.

## Development

```
python tools/smoke_test.py
python tools/check_license.py
```

Builds a small archive of generated photos in a temporary folder, scans it,
analyses it, starts the server and checks every endpoint including the CSV
export and thumbnails. It touches nothing outside the temporary folder. The
same script runs in CI on Windows and Linux.

```
python tools/make_icon.py
```

Redraws the icon — `web/favicon.ico` for the window and the browser tab,
`web/icon.png` and `web/icon.svg` alongside it, and `web/mark.svg`, the same
drawing without its tile, which is the one in the header of the interface. The
shape lives in the script as a handful of numbers, so it is edited there and not
in a graphics program. The files are committed; you only need this after
changing them.

## Credits

- [ExifTool](https://exiftool.org) by Phil Harvey — optional, for RAW and
  maker-specific tags
- [Pillow](https://python-pillow.org), [Flask](https://flask.palletsprojects.com),
  [exifread](https://github.com/ianare/exif-py)
- [Leaflet](https://leafletjs.com) (BSD-2-Clause) and map data from
  [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors
  (ODbL) — loaded only if you ask for the map

All of those are permissively licensed and none of them constrain the licence on
this project. ExifTool is called as a separate program, never bundled.

## License

[GNU Affero General Public License v3](LICENSE) — free software, OSI and FSF
approved. Use it, change it, share it, publish your fork; nothing to pay and
nothing to ask.

The AGPL asks one thing in return: if you build on this code, or run it as a
service other people reach over a network, your whole work goes out under the
AGPL too. If that does not fit — a product you ship, a hosted service, a company
that forbids AGPL code — a commercial licence is available. See
[COMMERCIAL.md](COMMERCIAL.md).

The interface carries a `source` link in the header. If you host a modified
version, point it at your own repository: section 13 of the AGPL requires that
your users can get the source.

Copyright © 2026 Sergey Inyutin.

### Getting the licence file

The repository needs the canonical, unmodified AGPL text in `LICENSE`:

```
curl -o LICENSE https://www.gnu.org/licenses/agpl-3.0.txt
```

On Windows: `Invoke-WebRequest https://www.gnu.org/licenses/agpl-3.0.txt -OutFile LICENSE`.
GitHub's own "Add file → Create new file → LICENSE" picker inserts the same text.

`python tools/check_license.py` verifies the file, and CI runs it on every push,
so a missing or altered licence cannot ship unnoticed.
