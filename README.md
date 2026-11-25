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

The window's log shows each HTTP request with its URL, response status and server
response time, for example `"GET /api/stats HTTP/1.1" 200 - 143.7 ms`. Time is
measured from the start of request handling until the server starts sending the
response headers; it excludes downloading the response body and browser rendering.
The same log is available when the server is started from a terminal.

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
  RAW and DNG* first if you want them; JPEG only by default. RAW files matching
  a JPEG's filename without the extension (case-insensitive) and EXIF capture
  time (to the second) are linked across the whole catalogue, including different
  folders. Only the JPEG appears and counts, with a **RAW** badge in Selection,
  Timeline and the photo card. Unpaired RAW files appear on their own.
  The photo information includes each companion's full path and a **Show RAW in
  Explorer** button. File actions are available locally with network access off.
  No ratings, tags or comments are transferred. Pairing uses indexed files only: discovering
  new RAW files requires this checkbox, `--raw`, `--all` or an explicit `--ext`.
- **Analyse images** — colour, brightness, contrast, clipping. Much slower: it
  decodes every file.
- **Sign files** — content signatures for exact duplicate matching.
- **Check a folder** — pick a folder you have *not* scanned and it says how much
  of it you already have. See below.
- **Rescan folders** — see what was added, changed, moved or lost from the
  selected folders before applying the changes to the catalogue. With no
  selection it checks all configured folders.
- **Open interface** — starts the server and opens the browser.

**Passes and the server run at the same time.** You can watch the statistics
fill in while the analysis is still going. Stopping a pass keeps what it has
already done and it carries on from there next time, so a long analysis can be
done in several sittings.

### What the scanner is doing

While a pass runs, the top of the page says so — *Analysing images · 13,413 of
92,362 · 14%*, with a small bar — and when it ends, how it ended: done and when,
stopped and at what point, or failed and why. It does not matter where the pass
was started: from the window, from a terminal or from a scheduled task, the
line is the same, because the pass writes its progress into the database the
page already reads.

The numbers on the page are not reloaded by themselves, because that would
reshuffle the random photos and fold every row you have open. The line offers
**refresh** instead, while a pass runs and once it has finished. A finished
pass stays in the line for twelve hours or until its **×**.

A page opened while a pass runs leaves the **Selection** panel empty: its
thumbnails are decoded from the originals, on the same disk the scanner is
reading, and loading them then slowed both to a crawl. The panel says so, and
fills in by itself once the pass is over, or straight away with **show them
now**. Only at opening: photos already on screen stay when a pass starts later.

A pass stopped with the window's Stop button, or one that crashed, cannot say
so itself. It writes a heartbeat every few seconds instead, and when that has
been silent for twenty the line reads *stopped*. Checking a folder is not shown:
it is the one pass that never writes to the database.

### Looking from another device

Tick **allow access from the local network** before pressing *Open interface*,
and the window shows an address such as `http://192.168.1.20:5577` — open it on
a tablet or a phone on the same Wi-Fi. The box is off every time the launcher
starts, and it cannot be changed while the server is running: stop the server,
change it, open the interface again.

**There is no password.** While it is on, anyone on that network can see every
photo, its folder and where it was taken, and change ratings, tags and
comments — which is why the warning under the box is red. Use it at home, not on
a shared or public network. The server listens on the home-network address only,
not on every adapter, so a VPN or a virtual network does not get the archive too,
and it refuses to start that way at all on a computer whose address is public.
The first time, Windows asks whether Python may use the network: allow it for
private networks only.

**Show in explorer** and **Open file** disappear while network access is on, and
from any device but the computer running photostats. Both act on that computer,
so pressed from the sofa they would open a window on a desk nobody is sitting at.

From the command line the same thing is `python app.py --host 192.168.1.20`.

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

**Rating and tags** — stars and marks of your own, and up to three words a
photo. See below.

**EXIF metrics** — camera brands, models, lenses, focal lengths, aperture,
shutter speed, ISO, file size, format and duplicates. Pick a camera or a lens and a second
panel appears: when it arrived, when the last frame was taken, how many days it
was in service, and a monthly strip of how much of each month it carried.

**Image metrics** — dominant colour in eleven groups plus `mixed`, the colour
in the centre of the frame, brightness, contrast, colour or black & white,
clipping in highlights and shadows, sharpness in the centre and at the edges,
and focus — the two compared. These need the image analysis pass.

**Where** — clusters of coordinates from EXIF, as a list and, if you ask for
it, on a map.

### Rating

Stars are read out of the file's own EXIF — tag `Rating`, which is what a camera
writes when you press its star button and what Windows Explorer shows. Nothing
else is read: Lightroom keeps its stars in `xmp:Rating` or in its catalogue, so
a photo rated only in Lightroom arrives here unrated.

You can also rate photos here, over the top of the camera. Hover a thumbnail in
the preview strip, or open a row in the list, and the control is there: a red
**✗** for a bad frame, **one to five stars**, and **5+** for the ones worth
keeping above the rest. Only one of the three can be on at a time, which is why
they sit in a single row — clicking what is already set clears it.

**Your ratings go into the database and nowhere else.** The files are never
written to. Clearing a rating you set hands the photo back to whatever EXIF
said, so the camera's own stars are never lost.

The panel lists the scale best first. Only the ratings given are there: the
unrated are most of any archive, and a bar for them set the scale for the rest,
leaving five stars a pixel wide. To work through what is not rated yet, sort the
selection by rating from the *sort* box — the unrated come first. The same
control is in the photo card, to the right of the picture.

### Tags

A photo takes **up to three tags**: one word each, letters and digits, no
spaces and no punctuation. Any alphabet will do — `море` is as good a tag as
`sea` — and everything is stored in lower case, so `Sea`, `SEA` and `sea` are
one tag rather than three.

Open a row in the list, or the photo card, and there is a field under the
rating. Type, and the tags already in the archive appear underneath with the
number of photos on each: pick one and the same word is used again rather than
a near miss of it. The suggestions come from the whole archive, not from what
is filtered at the time, so a tag used elsewhere is still offered. Enter takes
what you typed, arrows walk the list, and the **×** on a chip removes that tag.

The limit of three is the point of the feature. A photo with a dozen tags is a
photo nobody will find again, and three fit on one line everywhere they are
shown — in the list, in an expanded row and in the card.

The panel lists tags by how many photos carry each. Photos with no tag are not
a row there — while an archive is being tagged they are nearly all of it, and a
bar for them would leave every real tag too short to tell apart. Picking several
tags shows the photos carrying **any** of them, the way every other panel here
works. Tags come out in the CSV export as one column.

**Tags go into the database and nowhere else.** No file is ever written to.
They live in the catalogue, so re-reading metadata preserves them. Use
**Rescan folders** after moving files to keep the existing IDs, tags, ratings
and comments. Missing files keep their records and annotations. The web interface
counts and exports only photos whose indexed paths are present.

### Comments

Any photo can carry **one comment of up to 256 characters** — a line of your
own about it, in any language, emoji included. It is written in the photo card,
in the field under the tags: click, type, and Enter or clicking anywhere else
saves it; Escape puts back what was there. Emptying the field removes it.

In the selection a comment is only a small speech-bubble icon, after the file
name in the list and in the corner of a thumbnail — hover it for the text. The
whole text is read in the card. There is no panel for comments and no filter:
they are notes on a photo, not a dimension to count photos by. They come out in
the CSV export as one column.

The limit is in characters as you read them, so 256 letters of Cyrillic or 256
emoji fit as well as 256 of Latin. A comment is one line — a line break pasted
in becomes a space. Like the rating and the tags it lives in the database only,
and Rescan folders keeps it when a move is matched.

### Rescan folders

Select the folders to check in the launcher and click **Rescan folders**.
Review the old and new paths and press **Apply changes**. A match uses the same
rule as **Check a folder**. A new copy gets its own record if the original remains
at its old path. Ambiguous matches and unreadable folders are listed in the
report. Conflicts are left for another rescan. The optional **Apply changes
automatically after rescan** checkbox applies the actions listed in the report.
The web server periodically checks indexed paths while the interface is open.
A file disappears from the results when its path is gone and returns when the
file is back. Use **Rescan folders** to match a file moved to a new path.

For the command line, save the preview outside the folders being checked:

```sh
python scan.py --rescan-plan preview.json "D:\Photos"
python scan.py --apply-rescan preview.json
```

### Duplicates

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

**The selection also exports as a bare list of paths** — *export paths as CSV*,
one full path per line under a `Path` header, sorted by path and following
whatever is filtered. It is a list to hand to the program that actually moves
files: copy everything rated 5+ somewhere, delete what you marked bad, feed a
folder to a backup tool. On Windows that is one line of PowerShell:

```powershell
Import-Csv photo-paths.csv | ForEach-Object { Copy-Item $_.Path D:\Keep }
```

**This program never moves, renames or deletes a photo.** It reads files and
writes its own database, and that is the whole of it — the duplicate rule is a
good guess, not a certainty, and a program that acts on a guess like that costs
you a photograph the first time it is wrong. Deciding is yours; the list is so
that deciding does not mean copying paths by hand.

Clicking a thumbnail opens the photo card: the picture, everything known about
it, the rating and the tags. **Double-click the picture and it takes the whole browser
window** — no card, no metadata, just the photograph, with a sharper copy fetched
to match. Double-click again, or press Escape, and the card comes back; Escape
again closes it.

Arrows over the left and right edges of the picture — or the **←** and **→**
keys — flip through the selection without going back to the list. They page
through the set the card was opened from: the sorted list if you opened it from
a row, the ten random frames if from the preview strip. The list is loaded a
page at a time, and walking past the end of what is loaded fetches the next page
rather than stopping. At the ends of the selection the arrow that has nowhere to
go is not shown. They work in the whole-window view too, which is where flipping
through photographs is most of what you are doing.

Sort that list by capture time, file name, file size, camera or folder, either
way round. Or switch it to **folders** — the folders those photos live in, with
a count and a date range each. Click one to narrow the selection to it. That is
usually the faster way to find where a batch came from than paging through the
files themselves.

### Directory tree

Open **Directories** in the top navigation for a tree of the whole indexed
archive, independent of the filters in **Statistics**. Expand a directory with
its arrow or search by any part of its path; hovering a name shows the full path.
The columns show scanned file count, files with both colour and sharpness
successfully measured, and total file size. Each directory includes all its
descendants, with each file counted once. Failed or incomplete image analysis
does not count as measured; a sharpness value of zero does.

**Analyse** opens **Statistics** for that directory and its subdirectories,
keeping the other filters. Remove the Folder filter chip to remove the directory
restriction. **Refresh** reloads the tree from the database; it does not update
automatically. Expanded branches and the search are kept when refreshing or
switching pages in the same browser tab.

The tree describes files stored in the database, not the live filesystem.
Empty directories and directories with no indexed files are not recorded;
intermediate parent directories are included to show the hierarchy.

### Photo timeline

**Timeline** has its own filters, independent of Statistics. **Tags** lets you
search and select several existing tags; a photo needs at least one selected tag.
No tag selection includes the whole archive. Chips remove individual selections.
**Rating** offers **Good to bad** (`5+`, `5`, `4`, `3`, `2`, `1`, `bad`),
**Good only** (`5+`, `5`, `4`, the default) and **5+ only**. Your rating overrides
EXIF stars. Ratings and the stable random seed determine the 15-photo highlight
selection. For both highlights and full days, the browser puts **5+ first**,
other rated and unrated photos in the middle, and **bad last**, even after
unrated photos. Each group is ordered by capture time, earliest first, with photo
ID breaking ties. Date order changes the order of days only. Days without
matching dated photos are skipped.

**View → Portfolio** gives photos the full page width in one continuous mosaic.
Each day is marked by a plain date tile the size of an ordinary photo, without
buttons. The date tiles follow the selected day order. Each date tile and its
photos form one connected area. Neighboring days share space around large
photos, so part of one day can continue below the start of the next. A thin,
muted outline in the gaps traces each day's area. Its color is stable for the
same date.
The browser sorts three separate groups within each day in the order **5+**,
**five stars**, then all remaining photos (including bad and unrated), each by
capture time and photo ID. All tiles align to a grid of standard squares.
The first two groups use **3×4** cells for portraits, **4×3** for other
frames, and **4×2** for horizontal photos at 16:9 or wider. **4×4** takes
priority when the centre is **very sharp** and the edges are **soft** or
**very soft**, based on the Statistics sharpness bands. Aspect ratio
determines the size when a sharpness measurement is missing. All thumbnails
fill their tiles with a centred crop. Portfolio uses the Timeline tile size
to choose how many columns fit, then adjusts the squares to fill the available
width. Narrow windows show at least four columns. Only 5+ and five-star images show comments over
the bottom of the image. No RAW, rating or annotation
badges appear on Portfolio photos; the usual photo card and hover information remain available.
Within a day, ordinary squares follow the photo order along rows and continue
beside or below the tiles already placed for that day. The overlay keeps the
mosaic at the same height when a comment is added.
Portfolio fills remaining gaps, including the end of the final row, with blank
squares the size of an ordinary photo. Each square blends stored colors from
nearby photos or uses 50% gray (`#808080`) when no nearby color is available.
With **15 per day** enabled, Portfolio shows exactly 15 highlights per matching
day, or every match in a shorter day. The date tile sits outside that limit. There is no
**Show all** button or day action in Portfolio. Turn the limit off to load all
matching photos automatically. Resizing changes placement, not the selection.
Switching views keeps the filters and scroll anchor. Timeline retains its day
actions and Show all control; an expanded Timeline day still respects the limit
when switching to Portfolio. The view resets on reload.

**Filter** chooses one additional dimension: **Year**, **Camera models**,
**Lenses**, **Focal lengths** or **Aperture**. The second list selects one option.
It loads 30 choices at a time; type part of a name or range to search, or use
**More**. Search ignores case, including Cyrillic, and treats punctuation literally.
Choices and counts follow the current tag/rating/unrated filters. The selected
option stays visible; choosing another dimension clears it. **Clear option**
removes just the value, and **No extra filter** removes the dimension too.
Focal length and aperture ranges match Statistics; Timeline always uses
35mm-equivalent focal lengths, independent of Statistics' switch. Photos without
an equivalent value appear under **not specified**, even if actual focal length
is recorded. Other missing metadata is also offered as
**not specified** when present. The extra condition also applies to days, counts,
Show all and cards, before the 15-per-day limit.

**Include unrated** is on by default and independently adds photos without a
rating, including in 5+ mode. **15 per day** is a separate display toggle, off by
default. When enabled, it limits each day to 15 photographs **after** applying
all tag, rating and unrated filters. If more than 15 match, **Show all** expands
every remaining match under those same filters; **Show highlights** collapses
them. With the limit off, every matching photo is shown automatically with paged
loading and virtual thumbnails, regardless of the Include unrated setting.
Counters refer to matching photos. Large days load as you scroll and only nearby
thumbnails are drawn, including on narrow screens. Before displaying a full day,
the client collects its lightweight records in batches of up to 500 and sorts
them locally; a loading counter is shown meanwhile. Metadata loading pauses when
a day leaves the visible area or is collapsed. This avoids moving already visible
photos when a later server batch contains earlier capture times. Photo cards
follow the displayed order. No chronological SQL sort is added.

Tags, rating mode, Include unrated, 15 per day and the extra filter are kept only while the page is open;
reloading restores all tags, Good only, Include unrated on, 15 per day off and no extra filter. Changing these options
restarts the view and cancels obsolete requests. **Date order** still switches
between newest and oldest first and remembers its existing browser setting.
**Refresh** reloads database results and changes the random unrated highlight sample. Switching
pages or using Back keeps the current Timeline filters and scroll position.
Dates come from EXIF or the file date; files without usable dates are omitted.

Hover over a thumbnail for its filename, date, camera model, lens, one line with
focal length/aperture/ISO/shutter speed, tags and comment. Separate tag and
comment-bubble badges at the bottom right, opposite the rating, indicate each
annotation independently; both appear when both are present. Empty Lens, Tags and
Comment rows are omitted; other missing shooting metadata is shown as a dash.
Tooltip data arrives with Timeline records, with no request
on hover; saving tags or comments updates the tooltip and badges, including cached
days and the collapsed highlight selection.

**Add tags** under a day's date opens a tag picker with search and new-tag entry.
Press **OK** to add the selected tags to every photo matching the current Timeline
filters in that day, including photos hidden by **15 per day**. The dialog shows
the number of matches and warns when filters exclude any photos from the day.
Existing tags are kept and duplicates ignored. If adding the tags would exceed
the three-tag limit on any photo, the entire operation is blocked with an
explanation; choose fewer tags. Cancel leaves the photos unchanged.

**Remove tags** opens a checklist of all tags used by matching photos in that day,
with counts and a text search. Select one or more tags and press **OK** to remove
only those tags from the entire filtered selection, including photos hidden by
**15 per day**. Other tags are kept. The dialog warns when filters exclude part
of the day. Removing a tag used by the current filter still processes the whole
original selection, then refreshes the day's results. Cancel makes no changes.

Click a thumbnail for the usual card with ratings, tags and comments. Editing a
rating or tag refreshes the day's matching photos, including expanded pages.
**Analyse day** always opens Statistics with only that date selected and **every
photo from the day**, regardless of Timeline filters. Remove the Day chip there
to return to the whole archive.

## How some of it is worked out

**Colour is a vote, not an average.** The frame is scaled to 48×48, every pixel
is assigned to a group, and the largest group wins. Averaging RGB over a photo
always gives mud — a colour that is nowhere in the frame. Achromatic groups
compete separately: if at least a quarter of the pixels have colour, grey sky
and black background stay out of the contest, otherwise most of an archive
comes out grey.

**Grey has to allow for the light it was shot in.** A pixel counts as
achromatic below 0.14 saturation, which is above the tint daylight itself puts
on a neutral surface: grey paving in October sun measures 0.12, so at a lower
line half of it was called coloured and a grey cat on grey stone came out
yellow. The eye discounts the light it looks through and this cannot, so the
threshold has to. It does not go higher than that, or frames that are merely
muted start being counted as black and white.

**Dark pixels are judged more strictly.** Dark brown formally has a reddish
hue, but the eye reads it as shadow. Below 0.28 brightness a pixel only counts
as coloured when its purity is at least 0.80, so a dark red stays red and a
dark brown becomes black.

**Warm tones split three ways by purity and brightness.** They all share the
hue of an orange, so hue alone cannot separate them:

- dark warm is **brown** — wood, bark, chocolate, earth. Dark means half
  brightness, a brightest channel below 128: above that the tone is an ochre,
  and calling ochre brown made brown the commonest colour in an archive, a fifth
  of every frame in it. The arc reaches a little past
  red, because leaf litter and rotting leaves sit there; a purity ceiling on
  that stretch keeps wine and dark brick red;
- light and muted warm is **yellow** — wheat, straw, sand, sepia, which read as
  golden rather than orange;
- pure and bright stays **orange** — a ripe orange, a sunset, terracotta.

Skin sits at a redder hue than straw and stays orange. The boundary between
orange and yellow is at 40°, not the 45° of the wheel: a sunlit autumn crown
measures 35 to 50° and the eye calls all of it yellow, while nothing below 40°
is ever called yellow.

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
leaves scatter into 14% orange, 12% yellow and 8% brown against 30% moss.

**A merged family is named by its own average colour**, not by whichever group
in it has the most pixels. Counting decided it before, and a plurality named the
whole: a golden maple came out brown at 43% brown against 28% orange and 21%
yellow, because its shaded half outnumbered each of the lit halves separately.
The average is also the swatch shown beside the name, so the two can no longer
disagree. Where the average falls outside the family, counting decides as
before — the name has to be a colour that is really in the frame, or picking it
in the panel would select nothing.

**The centre is a quarter of the frame area,** the middle half in each
direction. The dominant colour mostly describes the background and the centre
one the subject, which is why they are more useful together: a black cat on a
white sheet is white dominant, black centre. It used to be half the area, and
half the area is still mostly the room: a baby in a white hat photographed in a
dark restaurant came out 37% black against 32% white in the centre, and the
background won a measure meant for the subject. The narrower square pays for
that on composition by thirds — a subject placed off centre falls outside it. Both panels are always on, so you can start from either end:
pick the centre colour alone to pull out every frame with, say, something dark
in the middle.

**A frame with no dominant colour is `mixed`** — when the largest group holds
less than a quarter of the frame.

**Sharpness is measured at screen size, by the sharpest part of the frame.**
The image pass decodes each photo a second time, grey, with its long side at
1500 pixels — what a photo looks like on a screen, not at 100%. A fixed size is
what makes cameras comparable: at full size a 50 MP frame looks softer than a
12 MP one of the same scene. The frame is cut into 64-pixel squares and each is
measured by the variance of the Laplacian, the usual focus measure that needs no
reference picture: how much the picture changes from one pixel to the next.
Squares with nothing in them — sky, a wall — are left out, and each region
scores its sharpest tenth. Not the average: a portrait with the background
thrown out of focus is a sharp photograph.

The centre is the same square as for the centre colour; the edges are the rest
of the frame, without the centre. The bands, from *very soft* to *very sharp*,
started out as fifths of a real archive and are meant to be tuned by eye; they
live in `app.py` and change without a re-scan. **Focus** compares the two
regions: *soft throughout* (both very soft), *centre sharper* (the edges less
than half as sharp — a subject against a background out of focus), *edges
sharper* (usually a missed focus, sometimes plain sky in the middle) and *evenly
sharp*. The list sorts by sharpness — by the sharper of the two regions, or
by the centre or the edges alone — and while it does, its last column shows
the number it is sorted by instead of the focal length.

What it cannot tell: fog, night and plain sky read softer than they are, since
there is little in them to be sharp, and the grain of a film scan reads as
detail. Motion blur is caught along with missed focus — the measure cannot tell
the two apart, and for deciding what to keep it does not need to.

The second decode makes the image pass about twice as long. An archive whose
colours are done only has sharpness measured; colour is not counted again.

**Timeline bars are shares, not counts.** Otherwise a quiet year would look
like the camera was in a drawer.

Thresholds live at the top of `scan.py` and `app.py` and are commented. Those
in `app.py` apply in the query, so changing them needs no re-analysis. Those in
`scan.py` do — bump `ALGO_VERSION` after editing and the archive re-analyses
itself on the next pass. Sharpness has a counter of its own, `SHARP_VERSION`,
which re-measures sharpness alone.

## Privacy

This matters more than usual for a photo archive, so it is worth being precise.

- The database holds full file paths, shooting coordinates and everything
  derived from your photos. `.gitignore` keeps it out of git — do not defeat
  that.
- The cache folder holds thumbnails, which are pieces of your photos.
- The server binds to `127.0.0.1`. Only the launcher's *allow access from the
  local network* box, or an explicit `--host`, changes that — see *Looking from
  another device*. File-opening buttons work only from `127.0.0.1` to a server
  bound to it.
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
color_center_share, brightness, contrast, chroma, clip_hi, clip_lo,
sharp_center, sharp_edge, sharp_v, mark, comment, path
```

If you want a metric the interface does not have, it is usually one SQL query
away. Old databases upgrade themselves: new columns are added, colour names
were translated in place, and metadata is re-read without touching the image
analysis.

Indexes for metric filters and capture-time sorting are added automatically when
the server or scanner starts; no photo re-scan is needed for these updates.

Two columns there are yours rather than the scanner's: `mark`, the rating you
set, and `comment`. No pass writes either, so re-reading metadata keeps them.

Tags are the one thing not in that table — a photo takes up to three of them,
which is not the shape of a column. They live in `photo_tags`, a row per tag per
photo, with a trigger that clears a photo's tags when its row goes. The status
line at the top of the page reads `jobs`, where each pass records its progress;
only the last fifty passes are kept.

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

**A RAW+JPEG frame still appears twice.** Pairing requires the same filename
without the extension and matching EXIF capture times to the second on both
files. File modification times do not qualify. Check these fields and rescan
with RAW enabled. Previously indexed RAW files stay in the catalogue when RAW
is switched off; `--full` re-reads metadata rather than rebuilding the database.

## Development

```
python tools/smoke_test.py
python tools/check_license.py
python tools/test_timeline.py
python tools/timeline_browser_test.py --allow-skip
```

Builds a small archive of generated photos in a temporary folder, scans it,
analyses it, starts the server and checks every endpoint including the CSV
export and thumbnails. It touches nothing outside the temporary folder. The
same script runs in CI on Windows and Linux.

Timeline API tests cover every rating/unrated combination and match-any tag
selection, including Unicode, day cursors, filtered cards and duplicate-free
paging. The browser test uses an already installed Chromium browser, a temporary
profile and generated fixtures; `--allow-skip` reports a skip if no browser is
installed. Use `--width 360` for the narrow-screen checks. It verifies the actual
controls, reload/Back, rating/tag edits and a deliberately delayed obsolete
response. No browser packages are downloaded.

```
python tools/make_icon.py
```

Redraws the icon — `web/favicon.ico` for the window and the browser tab,
`web/icon.png` and `web/icon.svg` alongside it, and `web/mark.svg`, the same
drawing without its tile, which is the one in the header of the interface. The
shape lives in the script as a handful of numbers, so it is edited there and not
in a graphics program. The files are committed; you only need this after
changing them.

## Database maintenance

Server, scanner and job tracking share numbered migrations in `db_schema.py`.
Existing catalogues upgrade at startup without rescanning, preserving photo IDs,
ratings, comments and tags. Schema changes, backfills and success records commit
together; interrupted migrations roll back and retry. Changed index expressions
are detected and rebuilt. Before changing a nonempty catalogue, the program
creates and checks a SQLite backup in `<database filename>.backups/` next to it;
a backup failure stops migration. These retained copies contain private catalogue
data, **not original photos**. See [recovery instructions](docs/database-maintenance.md).

Planner statistics are collected once for catalogues that lack them, after index
changes, and after completed scans or analysis change at least
`max(1000, 10% of the last analysed photo count)` rows. Committed work from an
interrupted pass is checked at the next startup. Maintenance runs at startup and
writer boundaries, never in GET requests. Table-scoped `ANALYZE` handles older
SQLite; SQLite 3.46+ also gets bounded `PRAGMA optimize`. The timestamp, versions,
method, duration, result and any error are available at
[`/api/db/diagnostics`](http://127.0.0.1:5577/api/db/diagnostics).
A persistent revision changes in the same transaction as photos, annotations and
signature mode, ready for future cache invalidation.

```
python tools/test_database.py
python tools/db_benchmark.py --rows 100000 --distribution unrated --output report.json
python tools/db_benchmark.py --source photos.db --output report.json
```

The benchmark compares one temporary synthetic catalogue or backup copy before
and after maintenance with a fixed seed, 5 warmups and 30 measured requests by
default. JSON contains versions, median/p95, SQL/connection counts, response sizes,
query plans and checksums, without photo paths or coordinates. Other distributions
are `bad`, `great`, `sparse` and `large-day`. Source catalogues are read-only.

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
