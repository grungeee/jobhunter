# Universal UI structure

`ui/interface.json` is one declarative file that describes what the app shows,
how the items group and nest, and what each element does. Two renderers read
it: a terminal menu (`tools/ui.py tui`) and a browser navigation
(`tools/ui.py gui`). Neither renderer owns the structure, so a new element
appears on both surfaces after a single edit.

```
ui/interface.json        the structure (canonical, reviewed by hand)
ui/SCHEMA.md             this contract
ui/examples/*.json       sample render context (facts, rows, selection)
ui/out/                  generated projections, safe to delete and rebuild
tools/ui.py              validator, the two renderers, and the menu loop
tests/test_ui.py         structure, projection and rendering tests
tests/test_ui_nav.py     menu navigation, filters, action text, frame widths
```

## Why a structure file

A dashboard usually grows two independent copies of "what exists": a menu in
the TUI and a nav in the page. They drift. Here the vocabulary is shared
(statuses, sort keys), the actions are shared (one transport per channel), and
the tree is shared. Density, glyphs and keyboard letters stay per surface.

## File shape

| Key | Meaning |
| --- | --- |
| `schema` | `hermes-ui-structure/1`. Bump when a rule changes shape. |
| `channels` | Delivery paths: `chat-frame`, `served`, `static`, `browser`. |
| `app` | Title, language, source of truth, renderer commands. |
| `surfaces` | `tui` and `gui`: capabilities, `max_depth`, density, global keys. |
| `roles` | Semantic roles with a terminal marker and a CSS class each. |
| `facts` | Typed values a renderer may read. Undeclared facts are rejected. |
| `vocabulary` | Closed lists: `statuses`, `stages`, `sort_keys`, densities. |
| `data` | Collections and fields; every listing field is untrusted text. |
| `actions` | What can happen, arguments, and one transport per channel. |
| `states` | `loading`, `empty`, `degraded`, `error`: message plus show/hide. |
| `tree` | The nested structure. Roots are groups or views. |

## Node grammar

Every node needs a unique `id` (dotted, stable, never positional) and a `kind`.

| Kind | Use | Renders as |
| --- | --- | --- |
| `group` | Container that nests items | Section header, collapsible in the GUI |
| `view` | A place you can navigate to | Nav entry plus a panel |
| `item` | A row template bound to a collection | Terminal row block, GUI table |
| `field` | One labelled value | Label and value, wrapped |
| `meter` | A headline count | Inline pair, stat card in the header |
| `status` | A value from a vocabulary, with role | Badge with a text label |
| `filter` | Toggle one vocabulary key or choose one value from a vocabulary | Chip, select, or picker |
| `sort` | A selector over `sort_keys` | One line list, `<select>` |
| `query` | Free text search | `/` prompt, search input |
| `action` | A command from the action registry | Menu entry, button |
| `message` | Explanatory or state text | Wrapped paragraph, callout |

Optional node keys:

```json
{
  "id": "listings.filters.scope",
  "kind": "group",
  "label": "Scope",
  "meaning": "Why this exists, one sentence.",
  "order": 10,
  "role": "warning",
  "when": [{"fact": "counts.applied", "op": "gt", "value": 0}],
  "requires": ["url_links"],
  "keys": {"tui": "s", "gui": "Alt+S"},
  "layout": {"tui": "inline", "gui": "chip-row"},
  "bind": {"vocabulary": "statuses", "key": "applied"},
  "surface": {"tui": {"label": "Open AMS in browser"}, "gui": {"label": "Open listing"}},
  "children": []
}
```

Nesting is arbitrary. A group inside a group inside a view is normal; the
surface decides how deep it may draw (`max_depth`) and truncates the rest while
keeping the parent, so nothing silently disappears.

Bindings:

- `"bind": "counts.total"` reads a declared fact.
- `"bind": "listing.score"` reads a field of the collection inside `item`.
- `"bind": {"vocabulary": "statuses", "key": "applied"}` binds one closed-list key.
- `"bind": {"vocabulary": "stages"}` on a `filter` with `"mode": "select"` exposes every stage as one exact current-stage choice.
- `"bind": {"vocabulary": "sort_keys"}` lists a whole vocabulary.

Conditions use a tiny declarative language, never `eval`:

```json
{"fact": "feed.warnings.count", "op": "gt", "value": 0}
```

Operators: `is`, `is_not`, `gt`, `lt`, `gte`, `lte`, `in`, `not_in`, `truthy`,
`falsy`, `present`. A missing fact fails closed: the node is hidden, not shown
by accident.

Capabilities are enforced per surface: a node with `"requires": ["url_links"]`
does not appear in the terminal, and the TUI footer reports how many nodes were
hidden and which capability caused it.

## Actions and their transports

An action declares its arguments once and a transport per channel. The
dashboard's single writer contract lives here as data:

```json
"mark_applied": {
  "label": "Mark applied",
  "role": "primary",
  "scope": "selection",
  "args": [{"name": "listing_id", "from": "selection.listing_id"},
           {"name": "status", "const": "applied"}],
  "transports": [
    {"channel": "chat-frame", "kind": "hermes.send", "template": "mark {listing_id} as applied"},
    {"channel": "served", "kind": "http", "method": "POST", "path": "/api/mark",
     "body": {"id": "{listing_id}", "status": "{status}"}},
    {"channel": "static", "kind": "cli", "command": "python3 -B tools/dashboard.py mark {listing_id} applied"}
  ]
}
```

Rules the validator enforces:

- Every `{placeholder}` in a template or command is a declared argument.
- A primary action keeps a fallback that works from a page opened from disk.
- Marking is never optimistic. The GUI shows the CLI command or sends the
  chat prompt; it does not paint success before the pipeline agrees.
- `toggle_bookmark` is a tracker write like any other: the declared transport is
  `dashboard.py bookmark <id>`, which flips `bookmarked` and `bookmarked_at` in
  the AMS store through the single writer, so the Bookmark label appears in
  both the main list and expanded listing, survives a restart and follows the device.
- A surface that reads a feed file (the Mac client, a page opened from disk)
  shows the write at once: the tracker's own JSON echo is folded into the row
  the key acted on (`Nav.apply_write_echo`), so the star and the gauge move
  before the feed is rebuilt. A write that fails copies nothing, and only the
  fields the writer printed (`stage`, `status`, `stage_at`, `note`,
  `bookmarked`, `bookmarked_at`) are ever taken from the echo.

## Projection, step by step

`Projection(spec, ctx, surface)` in `tools/ui.py`:

1. Merge the node's `surface` override for this surface.
2. Drop nodes hidden by `hidden`, an active `state.hides`, or a missing
   capability (recorded for the footer).
3. Evaluate `when` clauses against declared facts. Missing facts hide.
4. Recurse into children; beyond `max_depth` keep the node and set
   `truncated` so a renderer can say "+3 more".
5. Drop a group that ended with no children, then sort by `order` (stable).

## Renderer contract

Any renderer for this file must:

- read only declared facts and collection fields;
- escape every listing value before it reaches markup or a terminal escape;
- show labels from the vocabulary, never raw keys;
- keep one reachable path to every node (clicks, keys, or a printed command);
- honour `role` semantically, not only by colour;
- print or expose the CLI command when an action has no live channel;
- never write tracker state itself.

## Recipes

Add a new screen or menu entry:

1. Add a node under the right group with a new unique `id`, a `label`, and a
   `keys` letter that is not used on that surface. Run `validate`.
2. If it needs data, declare the fact in `facts` first. Undeclared facts fail.
3. If it needs a command, add it to `actions` with a transport per channel and
   reference it with `"action": "<ref>"`.

Add a new status:

1. Append to `vocabulary.statuses` with `filter: true` when it should be
   reachable, plus a `role`.
2. Add the matching `filter.<key>` node, otherwise validation fails on
   reachability.

Add filtering by exact application stage:

1. Add a `filter` node with `"mode": "select"`, bind it to
   `{"vocabulary": "stages"}`, and give it a TUI key.
2. The browser renders a select with an `All stages` option; the TUI opens a
   picker. The chosen stage combines with status, scope, and search filters.

Add a third surface (for example a webview or a mobile shell):

1. Add an entry under `surfaces` with its capabilities and `max_depth`.
2. Give `roles` an entry for the new surface and add `keys` where useful.
3. Write a renderer that consumes `Projection(spec, ctx, surface)`; do not
   copy the tree into the new surface.

## Commands

One command name, once the wrapper is installed:

```bash
jobhunter            # menu on the live feed
jobhunter dump       # terminal projection, no interaction
jobhunter web        # build and open the browser projection
jobhunter validate   # structure check
jobhunter refresh    # rebuild the feed, then open the menu
```

Install or update it with `cp ui/jobhunter ~/.local/bin/jobhunter && chmod +x
~/.local/bin/jobhunter`. The old `jobui` command remains as a compatibility
alias during the rename. Without a local checkout the same wrapper falls back to
`ssh <configured-host>`. Raw entry points:

```bash
python3 -B tools/ui.py validate
python3 -B tools/ui.py flatten --surface gui --format csv
python3 -B tools/ui.py tui --context ui/examples/context.sample.json
python3 -B tools/ui.py browse --feed .ai/feed/jobs.json
python3 -B tools/ui.py gui --context ui/examples/context.sample.json --out ui/out/gui.html
python3 -B tools/ui.py context --feed .ai/feed/jobs.json --out ui/examples/context.sample.json
python3 -B tools/ui.py build
```

`build` writes both projections using the live feed when it exists and an empty
context otherwise, which is also the cheapest way to look at the empty and
loading states.

## Interactive terminal menu

`tui` prints a snapshot; `browse` walks the same projection as a menu. The
Listings tab begins with the title on its own line, then a compact numeric
score chip and a coloured location chip together on the line directly beneath
it, left aligned with the title. Score, title and pay are three columns of one
grid: the widths follow the widest cell in the visible page, so every card puts
the city, the stage strip, the pay cell and the flags in the same place instead
of drifting with the text length. Pay is the monthly figure the ad states; when
the ad only gives a year, the Austrian 14 payments a year turn it into a
monthly figure that is labelled as calculated (`≈ EUR 3,800/mo (yr/14)`). The item node supplies
all other fields, flags, and actions when a listing is expanded. Both renderers
read this node; they do not maintain separate field lists.

The location chip carries one colour per city. The city key drops the postal
code and district suffix, so `Sample City`, `12345, Sample City` and
`67890, Sample City, North District` resolve to the same label and share a
colour; the chip shows the city name and keeps the full address as its tooltip.
Colours are assigned deterministically from the loaded feed without a pinned
location or personal preference, so feed order does not affect the palette and
no two known cities share a hue. The chip keeps neutral geometry (14px in the
list, 18px in the detail) and takes the colour on its 1.5px border, text, and a
12 percent background tint, so colour reinforces the label and is never the
only cue. The palette is computed from the entire loaded feed, not the
currently filtered subset: selecting a status or searching cannot reassign a
city's colour.

```bash
python3 -B tools/ui.py browse --feed .ai/feed/jobs.json     # live feed snapshot
python3 -B tools/ui.py browse --context ui/examples/context.sample.json
python3 -B tools/ui.py browse --width 68                    # narrow terminal
```

```
j / down      next job in the list; in detail j jumps to the next paragraph, down scrolls a line
k / up        previous job in the list; in detail k jumps to the prior paragraph, up scrolls a line
l / right     expand the focused listing; inside it, fold or unfold the section at the window top
h / left      back to the list at the same position
enter         expand the focused listing; inside it, fold or unfold that section
a i b K u     on an expanded listing: show a mark command, never run it
/             search listings (enter applies, esc clears)
?             help
tab / ]       next tab
shift-tab / [ previous tab
q             back from details; quit from the list
```

## Tabs

Views are grouped into tabs, declared once at the top level. Each tab names the
node it scopes to; the renderers do not hardcode a layout per tab.

```json
"tabs": [
  {"id": "tab.listings", "label": "Listings", "root": "listings", "order": 10},
  {"id": "tab.profile", "label": "Profile", "root": "profile", "order": 35}
]
```

* `tab.listings` is the default and shows one row per job, sorted by score
  descending and then by monthly pay descending at equal scores: a stated
  monthly figure first, then a figure calculated from a yearly one, then
  unknown. Unknown score and unknown pay sort last within their comparison.
* A tab whose subtree is entirely hidden by `when` conditions is dropped from the
  bar, so an empty Tracker does not advertise itself.
* The browser draws one `role="tabpanel"` per rendered tab; the terminal draws
  the active tab. The Listings projection is a flat list and a one-record
  drill-in; other tabs retain their branch views.

Both surfaces answer `j`/`k` to move in the list, `l` to open a job, `h` to go
back, and `[`/`]` to switch views. The browser also supports clickable rows,
the Back button, and arrow keys. In expanded view the cursor steps through the
ad in three levels (listing, section, paragraph); arrows scroll in small
steps, and terminal arrows scroll one line. Its view switcher stays collapsed
by default.

### Stepping through a listing

The detail is a ladder, not a scrolling page. One cursor per level, and the
same keys on both surfaces:

| key | in the list | section level | paragraph level |
|-----|-------------|---------------|-----------------|
| `j` / `k` | next or previous listing | next or previous section | next or previous paragraph |
| `l` / `right` / `enter` | open the focused listing | step into the section under the cursor | - |
| `h` / `left` | - | leave the listing | back to the section level |
| `space` | - | fold or unfold the section | fold it, back to the section level |

* Sections are the cards: `Properties`, `Unternehmensbeschreibung`, `Beruf`,
  `Stellenbeschreibung`, `Allgemeine Informationen`, the full-text card. The
  cursor sits on one of them and is marked twice: the box border and title take
  the accent colour, and the title carries a trailing `›` (or `2/3` while
  inside), so the position survives a colourless terminal.
* Inside a section the title reads `Stellenbeschreibung  ·  3/20` and `j`/`k`
  walk that section's paragraphs only. The focused paragraph is drawn in accent
  bold.
* Folding is `space` and nothing else. Forward only steps in, so a keypress that
  means "go deeper" can never hide text; `l` on a folded section opens it again
  as its first step, because there is nothing to walk inside a closed section.
* A manual scroll (arrows, wheel) takes the window off the cursor and stops it
  following; the next `j`/`k` puts the cursor back in view. The window never
  lands the cursor on its bottom rows, because the last one can be replaced by
  the `scroll for more` edge and the one above it would be covered by that edge.
* A folded card keeps its title with `(folded)` appended and nothing else, its
  border turns muted, and it exposes no paragraphs, so nothing can be entered.
  `l` on a folded card unfolds it instead. A card without content cannot fold:
  there is nothing to hide and the fallback sentence is the whole card.
* Opening a listing starts at the first section, unfolded, so cursor and fold
  state never leak between records.
* Browser: each section is a `<details open>` with its own `<summary>`, so a
  click still folds it, and the ladder keys drive the same three levels through
  `focusSection`, `enterSection`, `foldAllSections` and `leaveSection`. The
  focused section takes an accent rule and heading through `.is-focused`. The
  paragraph walk skips text inside a closed section by testing
  `details:not([open])`, because a paragraph inside a closed `details` still
  reports a client rect and the geometry check alone would walk invisible text.

## Listing detail

A field may declare where it belongs:

```json
"layout": {"tui": "inline", "gui": "detail"}
```

The Listings tab ignores column hints in its compact list. It lifts the score,
location, and known pay from the item's own fields. Score and the city
chip share the line directly beneath the title (score carries the red-to-green
ramp, the city its own colour), and pay stays on the line under them. It shows its
other fields and flags only inside the expanded record. Other views
may still use `inline`, `detail`, and `preview` layout hints. The mark actions
are hidden with the detail and are read-only in the terminal. The public browser
snapshot labels these as command previews; clicking shows the selected job's
validated SSH command, which can be selected/copied and run from a Mac. A click
does not change tracker state. `jobhunter live` opens `127.0.0.1:8766/gui.html`
through an SSH tunnel to a VPS loopback-only server; there the mark buttons
POST to `/api/mark`, which invokes `ams_nudge.py` and reads the exact job back
from the feed before the browser reloads. Cross-origin POSTs are rejected.
The public HTTP snapshot never accepts POSTs. Action templates
are retargeted with the selected job's id and URL at expansion, not the first
row's id. The source control is one native anchor styled as a button, not an
unchecked `window.open` popup or a duplicated link. It retargets to the
selected safe HTTP(S) URL and opens a new tab. If AMS redirects to maintenance
or an expired ad, the UI says so without claiming the source page loaded.
Expanded actions are compact, icon-and-label controls. The icon is decorative;
the visible text and accessible name still describe each action. On public HTTP,
one caption identifies the read-only command mode instead of repeating
`Show ... command` inside every control. Private loopback mode identifies live
status changes separately; this styling does not change the writer path.
The list's search, status and scan filters come from `listings.controls`. They
filter the actual job list (not just a hidden table), update its visible count,
and move keyboard selection to a visible job; when none match, the empty state
is shown. `Reminder` is the UI name for the legacy `nudged` status: an internal
reminder raised with no application sent or employer contact implied. The
`Set reminder` action changes that tracker status through the normal writer.
The Bookmark button is also a tracker write: it updates `bookmarked` through the
same writer. A `Bookmark` label on the row and in expanded detail reflects the
tracker state; neither view keeps a browser-local copy.

The score field declares `layout: {"tui": "badge", "gui": "badge"}`. Its
14px-high list chip sits on the chip line under the job title, next to the city
chip. In the expanded detail it uses the flag geometry: same 29px box, same
12px label and 10px side padding as the other tags, first in the flags row,
with a 2px border instead of the flag's 1.5px so the score still reads as the
heavier chip. Only the number is visible inside it; its accessible name
identifies it as a score, and it progresses continuously from red at 0 to green
at 10; an unknown score is grey and shows `?`. It is not repeated among the
detail fields. "Seen in latest AMS scan: Yes/No" reports observation, not
whether the vacancy remains open.

`listing.company_description`, `listing.occupation`, and
`listing.job_description` are the AMS sections Unternehmensbeschreibung, Beruf,
and Stellenbeschreibung. They come respectively from `companyIntroduction`,
`title`, and `summary` in the detail response for the **same** listing id.
`listing.general_info` holds `generalEqualitiyInfo` when supplied. None is
inferred from the search-card title, occupational group, or snippet. The
expanded browser and terminal views show each section under its own heading;
when a source field is absent, they say it was not captured. The browser keeps
an additional archived full-text body collapsed when structured sections are
available, instead of repeating the whole ad in the detail card. Properties
sit in their own bordered block; the AMS description is a separate bordered
native `details` panel, open initially and collapsible by click or Enter/Space.
Each captured line is an escaped paragraph target for `j`/`k` navigation.

`listing.full_text` is a captured detail-page body, separate from the
`listing.snippet` search-card preview. Never promote the preview into the full
ad. AMS currently serves the detail through its browser app: its own XHR to
`/public/emps/api/joboffer/uuid/<uuid>` succeeds, while plain urllib without
the page-issued access token gets 401. Browser captures can be imported with:

```bash
python3 -B tools/listing_text.py import-browser --file /path/to/captured-ads.json
python3 -B tools/feed.py --enrich-existing
python3 -B tools/ui.py build
```

The importer validates the AMS detail-page URL and the response UUID before
writing `.ai/feed/listing_text.json`, keyed by listing id. The feed merge only
adds cached fields and keeps the existing scan, scores, application state and
ordering. The importer accepts `[{"id": "<uuid>", "page":
"https://jobs.ams.at/public/emps/jobs/<uuid>", "status": 200, "data":
{...the AMS response...}}]`. A 404 is not ad text: it leaves the sections
unavailable. When there is no full body, the short search card appears inline
with a caption identifying it as incomplete. `tools/listing_text.py fetch`
remains a best-effort HTML fallback; the existing two-hour capture cron tries
it, but cannot obtain these structured JSON fields when AMS requires the
browser-issued access token. Do not claim that cron has captured them.

The HTML fallback collects the schema.org `JobPosting` description, `<main>`,
selected ad-body containers, and `role="main"` candidates and picks the
longest. A candidate no longer than the card preview is rejected. `probe`
reports the HTML response, maintenance status, candidates, and the actual AMS
detail API endpoint for a selected id:

```bash
python3 -B tools/listing_text.py probe --id <uuid>
```

The terminal menu uses rounded boxes for its header, filters, listings, details,
actions, and keyboard help. Active buttons use role colours and a filled
background; inactive buttons are dim gray. The focused listing has a blue
border, the score uses the browser's red-to-green ramp, and city names keep
their stable city colours. Colour is enabled only when stdout is a real
terminal and `NO_COLOR` is unset. Width fitting happens on plain text first,
so ANSI styling never changes a line's measured width.

In the Listings tab, `v` opens the focused listing in a new browser tab. This
action accepts only HTTPS URLs on `jobs.ams.at`; missing or other-host links
stay disabled and are never handed to the browser.

## Profile Skills draft

`tab.profile` scopes to `profile.skills`. The browser editor can add, remove,
import, save, and export skill claims with required source evidence. Importing
`modules/profile/profile.json` takes its `strong_signals` into an unsaved draft
with blank sources; every source must be filled before saving/exporting. The
draft is stored only in the browser's local storage. Export creates a
`profile-skills-proposal.json` file for review; it does not write the canonical
private profile. No private profile content is embedded in `gui.html`.

The editor is disabled on public HTTP. Use `jobhunter web` on the Mac for a local
file draft, or `jobhunter live` for the loopback-only live page. Neither mode sends
profile changes to the canonical file. The public preview still shows the
Profile view and why editing is disabled. The terminal has no editor.

### Coverage the list cannot fix

`Group` (`listing.occupation_group`, from AMS `berufsgruppe`) is filled for the
245 rows of the current scan and empty for the 385 older ones, because AMS only
ships the field with the scan payload. `posted_basis: "extraction"` marks the
370 rows whose date is the last observed extraction date rather than the
posting date; the expanded record labels the date and displays `EST`.

No source has a structured salary field. The normalizer searches the search
card, the AMS `general_info` sentence, and the captured ad body, in that order,
and extracts a monthly amount when one of them states a monthly basis and a
yearly amount when one states a yearly basis. Both are kept as `pay_monthly_min`
and `pay_yearly_min` with `pay_basis` naming which was found. A yearly figure
also fills `pay_monthly_calc` as `yearly / 14`, because Austrian gross pay runs
on fourteen monthly payments (twelve salary months plus holiday and Christmas
pay); the UI always labels that number as calculated and never presents it as
the ad's own wording. There is no invented zero or assumed monthly rate for
texts such as `Gehalt: EUR 3.144,20` that do not specify a period, and a figure
sitting next to both a monthly and a yearly word is treated as ambiguous and
left unknown. Pay is only a secondary sort key when scores tie.

## Flags

Each expanded listing carries chips built from `flags` on `item.listing`: a flag is a
short uppercase label plus a role (`primary`, `success`, `info`, `warning`,
`danger`, `muted`) and a `when` condition. Colour is never the only cue - the
label text is the identifier.

Semantics are read from the node kind, never hardcoded per status:

- `filter` with `bind: {vocabulary: statuses, key: X}` toggles that status.
- `filter` with a plain `bind` fact toggles the boolean fact.
- `sort` cycles the keys declared in `vocabulary.sort_keys`, starting at the
  node's `default`.
- `item` expands into one entry per context row, filtered by the active
  filters and search, and its child fields become the detail pane.
- `action` resolves the `static` channel command with the structure's own
  `args` (`const` and `from: selection.*`) and prints it.

The menu is read only on purpose. The static channel is a printed command, so
`ams_nudge.py` remains the only writer and a stray keypress cannot change a
tracked listing. In an expanded terminal record, `a`, `i`, `b`, `K`, and `u`
print the corresponding command in the message line; they do not run it. The
browser action is separate and only opens the validated AMS source URL.

## Colour themes

Nine colourways ship in `THEMES`: Catppuccin Mocha, Catppuccin Latte, Monokai
Pro, Tokyo Night, Dracula, Nord, Gruvbox Dark, Solarized Dark, and One Dark.
Anchors come from the upstream palettes (Catppuccin `palette.json`, Tokyo Night
VSCode theme JSON, Dracula and Nord published tables, Gruvbox palette block,
Solarized palettes, One Dark `colors.less`). Monokai Pro is proprietary and has
no published hex table, so its values follow the MIT port and match community
ports; treat them as close, not vendor-certified. Latte's accent, success,
warning, danger, cyan, and magenta are darkened from their upstream values so
text on the light background clears AA; the base, text, and border anchors are
unchanged. Each one
declares the same semantic roles - `bg, surface, raised, fg, muted, border,
accent, success, warning, danger, cyan, magenta` - and every component colour
is derived from those roles at render time, so a switch recolours borders,
chips, buttons, and score ramps together instead of leaving hard-coded
leftovers behind.

- Selection order: `JOBUI_THEME` environment variable, then the persisted
  choice in `~/.config/jobhunter/theme`, then `catppuccin-mocha`.
- In the terminal, `t` cycles forward and `T` backward. Persistence only
  happens through the interactive loop, never in a render call, so tests and
  printed frames cannot change the user's stored theme.
- In the browser, the header carries a theme selector; the choice lives in
  `localStorage["jobhunter.theme.v1"]` and never reaches the server. Each theme is
  a `body[data-theme="..."]` block that redefines the `--ui-*` tokens, and no
  component colour is written literally in CSS.
- Both surfaces are independent: the terminal theme is a terminal preference,
  the browser theme is a browser preference, and neither writes the other.

## Terminal chrome

The listing surface is deliberately quiet:

- No tab bar is drawn. `ctrl-t` opens a one-line-per-tab menu; a digit picks a
  tab, `esc` closes it, and picking the current tab changes nothing. `tab`,
  `]`, and `[` still cycle tabs for muscle memory.
- The header is a single line and carries everything about the focused record:
  `Job Search OS | Listings [597] | <title> | <score> <flags> | <stage>` with the
  stage hard against the right edge. So no `Match` box, no `Process` box, and no
  colourway name in the header: the stage gauge in the header replaces the whole
  block above the fields, and the stage timing note lives in `Properties`.
  Fitting is priority ordered: the stage never moves, the title always survives,
  the flags decay from the right down to the score, and the title is only cut
  when it still cannot fit.
- `Match` holds exactly one row: score and flags side by side. What each flag
  means is not printed in every listing. The first time a listing is opened the
  app shows a popup with the flag definitions and writes
  `~/.config/jobhunter/flags-explained` when any key dismisses it. That file is the
  contract: while it exists the popup never returns. A build or a render never
  writes it, because the popup only exists in the interactive loop, and keeping
  the definitions out of the body means the detail view stays short.
- Read-only actions are a filled band with an `ACTIONS` leader, not another
  bordered box. Active commands carry their role colour as a background. The
  band is one row: the live steps are named, and the remaining stage jumps
  collapse to `keys m e a c i W d b U K` so twelve actions still cost one line.
  It only wraps when the row genuinely does not fit.
- Keyboard hints are one inline, dim line at the very bottom, with no box and no
  blank line after it, so the last line can never read as content.
- Mouse reporting blocks the terminal's own text selection, so `M` hands the
  mouse back (`1002l`) with a notice, and pressing it again reclaims the wheel
  and the buttons (`1002h`). The mode is re-applied after every key, not at
  startup only.
- Mouse wheel: the loop turns on SGR reporting (`1002` plus `1006`) and turns it
  off in `finally`, so the terminal emulator stops scrolling its scrollback.
  Wheel moves three lines inside the expanded detail and one card in the list,
  and `wheel_up` at the top clamps to zero.
- Mouse click: while the mouse is claimed, a press on the left button is
  decoded to `("click", x, y)` and resolved against the frame that is on screen.
  A click inside an ACTIONS pill runs exactly the action its key runs; the
  pills are the only controls, so a click anywhere else changes nothing, and a
  release or a drag never fires twice. With the mouse handed back (the default)
  the terminal delivers no click at all and the keys are the way in.

## Process stage gauge

Every listing carries one stage, and the stage is the source of truth in the
tracker. The coarse `status` is derived from it, so readers that only know
`new` / `applied` / `interview` keep working.

    (unread) read > generated > checked > sent > confirmed > waiting > contact
                                                                  |
                          interview  success  rejected  blocked  unclear

`unread` is stage 0 and has no circle of its own: a listing that was never
opened shows seven empty circles, `○○○○○○○ Not read`. Opening it writes
`read` through the writer and fills the first circle, `◉○○○○○○ Read`.

- `read` the ad was seen, `generated` the letter exists, `checked` it was
  proof read, `sent` it went out, `confirmed` the employer acknowledged it,
  `waiting` is the quiet week, `contact` is the call after that week.
- `stage_due` is `stage_at` plus seven days for `sent`, `confirmed`, and
  `waiting`, so the quiet week ends on a date instead of in silence.
- The gauge lives in the header line, one strip per focused listing:
  `●●●●●◉○ Waiting`. Glyphs are done `●`, current `◉`, todo `○`, and the strip
  takes the role colour of the current stage.
- The detail view folds the timing into `Properties` as a `Stage` row, for
  example `in stage since 2026-11-07 (8 d) · call/contact 1 d overdue`, so the
  seven day rule is readable without leaving the listing.
- Stage 0 to 1 is automatic: `l` or `enter` on an unread listing runs the
  `stage_read` transport once. Surfaces without `capabilities.writes` only
  show the command, and a listing that already has a stage is never rewritten
  by opening it.
- Stage keys work from the list and from the open listing: the write binds to
  the focused row, so the list does not have to be opened first.
- The action band is the cycle, not a wall of buttons: `p` back, `n` forward,
  then `b` blocked, `U` unclear, `K` skipped. Forward walks the pipeline one
  step at a time; back walks it in reverse, out of an outcome and back through
  the flow. At the last flow step (`contact`) forward does not guess: it opens
  the outcome question with `1` interview, `2` rejected, `3` success, and
  `esc` cancels without writing. The browser shows the same five buttons and
  reveals the three endings in a panel under them.
- Mouse reporting is off by default so drag-select and copy work in the
  terminal; `M` claims the wheel and `M` again gives the mouse back.
- The individual jumps stay bound even though they are out of the band:
  `m` generating, `e` control, `a` sending out, `c` confirming, `u` waiting,
  `C` call/contact, `i` interview, `z` interview preparation, `X` waiting for
  outcome, `d` fail, `W` won, plus `b` blocked,
  `U` unclear, `K` skipped. A `band: false` node keeps its key and stays out of
  the button row; the band ends with a `? keys` hint instead of a wrapped
  list of letters.
- A stage key is a real write, never a preview. The key resolves against the
  row under the cursor, runs the declared local transport
  (`tools/dashboard.py mark <id> <stage>`), which shells out to
  `ams_nudge.py`, still the only writer of `applications.json`. Only after the
  writer returns zero does the menu reload the feed, move the gauge, and print
  a notice naming the stage that landed.
- Surfaces without `capabilities.writes` show `run: <command>` instead and
  never touch tracker state, which keeps frame dumps and tests read only.
- The same stage key pressed twice turns the move back. A listing already in
  that stage runs `undo_stage` instead of writing the stage again:
  `python3 -B tools/dashboard.py unstage <id>`, which asks the writer for the
  stage recorded before the last one (from `stage_history`) and stores that.
  So `b` blocks, `b` again un-blocked, and the notice reads
  `Back at Sending out · the stage key was pressed twice`. When nothing
  earlier was recorded the writer refuses and the message says so; the stage
  is never guessed from the pipeline order, because a listing blocked while
  `sent` must come back to `sent`, not to `contact`. The browser checks the
  row's `data-stage` in the same way and posts `/api/unstage`.

## Application packet

Press `R` from the list or an open listing, or click **Generate application**
(enable terminal mouse reporting with `M`). The listing shows a spinner in the
list and in its detail header. You can keep browsing other listings during
that run. When the draft is saved, Jobhunter marks that listing as generated
and opens the review if you are still on that listing. Otherwise, use `V` or
**View application packet** when you return. The served browser view also
shows progress on that listing without interrupting navigation.
`Esc`, `q`, or `h` from the left pane
returns to the listing with **Application packet** focused;
`;` reopens it without generating. `V` opens a saved packet directly.

The TUI opens a two-pane cover-letter window. The left pane is the complete
letter (subject, greeting, body, sign-off, name), as exported to DOCX. The right
pane has one card per excerpt, quoting the exact wording and explaining its
purpose. `l` enters the wording pane, `h` returns to the letter, and `j`/`k`
selects the next/previous block on both sides. `Tab` also switches panes;
arrows and Page Up/Down scroll the focused pane without changing the selection.
`y` copies the selected paragraph with its exact excerpt and explanation;
`Y` copies only the full letter, without notes. On the Mac
this uses the system clipboard; over SSH the terminal must accept OSC 52.
`i` toggles application details (attachments, CV notes, company, and channel).
Older drafts display as saved; edited drafts do not show stale wording notes.
Reopening never regenerates a draft.

Press `e` for the revision workspace: the saved block and the Flash suggestion
sit side by side, with a feedback composer below. `Enter` sends feedback in
the separate Hermes Flash session; edit again as needed. Clear the composer
and press `Tab` to have Pro propose lasting rules and sourced CV facts. Read
the exact additions/removals, press `a`, then `Enter` to save. `Esc` goes back
without saving. `R`, choose a model, then `Enter` regenerates with the approved profile; saving
rules never edits the old letter or CV. `jobhunter revise ... --plain` is the
line-based fallback. You can also ask to add a Jobhunter cover-letter rule in
ordinary Hermes chat, without opening a draft. See `modules/profile/MODULE.md`.

Press `c` for CV History, or run `jobhunter cv`. `[`/`]` select an older/newer
version, `{`/`}` change the baseline, `t` toggles between the two documents, and
`d` opens their Git-style text diff. `j`/`k` scroll and `q` returns. The history
reads existing versioned PDFs through `pdftotext`, not Git or a model. It does
not compare layout or change the CV selected for applications.

Press `,` from the listings or cover-letter review to open Settings. `Esc`
returns to the same view. Choose **Generation Language** (`1` German or `2`
English) or choose **Generation Model**:

| Settings key | Run picker key | Preset | Route |
| --- | --- | --- | --- |
| `3` | `1` | Fast & free | Nous Portal `tencent/hy4-flash:free` |
| `4` | `2` | Fastest | Codex OAuth `gpt-6-luna`, low reasoning |
| `5` | `3` | Regular (default) | DeepSeek API `deepseek-flash` (V4.1 Flash) |
| `6` | `4` | Smart | Codex OAuth `gpt-6.2-sol`, medium reasoning |

Settings are saved locally. `R` opens the model picker with your saved default
highlighted. Select with a number or the arrow keys, then press `Enter` to run
or `Esc` to cancel. The per-run choice is sent over SSH from the Mac without
changing your default. The served browser also asks for a model before running. Presets use their named route
only; a provider error does not switch to another model or replace the saved
letter. The CLI accepts `--language de|en` and `--generation-model
fast-free|fastest|regular|smart`. Packet metadata records the requested
language, model, and provider. Changing settings does not itself regenerate.

- One folder per listing, under `modules/applications/packages/<listing id>/`
  (git-ignored, together with `records/`), holding `cover-letter.md`,
  `cover-letter-review.json` (paragraphs, exact excerpts, and explanations),
  `cv-notes.md`, `documents.json`, `company.md`, `rationale.md` and
  `meta.json`. `tools/generate.py` writes the whole packet or nothing: it
  stages the files in a temporary directory and moves it into place, so a
  failed model call cannot leave a half application behind.
- The gate is `meta.json` parsing and naming this listing, not a folder
  existing. If only the server has the packet, its refusal to overwrite prompts
  for the same confirmation. A nonempty incomplete folder is left untouched
  rather than overwritten without authorization.
- On a saved packet, `R` opens the model picker in Regenerate mode. `Enter`
  runs the selected model (`y` is also accepted from the listing, never inside
  review). Only confirmation passes `--force`. A successful regeneration keeps
  the previous draft in Versions.
- A listing with under 400 characters of advert text is refused with one line
  instead of a letter invented from its title; 149 of the 640 feed rows are in
  that state.
- The stage only moves after the generator exits zero, and it moves through
  the writer (`tools/dashboard.py mark <id> generated`), never by editing the
  store. The review window opens right after, showing the model and timestamp,
  the cover letter and its wording cards, with attachments, CV notes, company
  summary, and channel rationale in the separate details panel.

```text
TUI      R  python3 -B tools/dashboard.py generate <id>    (add --force after y)
TUI      V  reads the packet folder or fetches it on demand over SSH; no write or model call
browser  POST /api/generate {id}        409 + needs_confirm when a packet exists
browser  GET  /api/packet?id=<id>       the review text, section by section
browser  POST /api/note {id, note}      the feedback note
```

`tools/dashboard.py generate` decides the host: where a local `hermes` exists it
runs `tools/generate.py` here, and a client without one hops through
`jobhunter-remote generate`, so one spec command serves the VPS, the Mac and the
served page. The TUI can read it over `jobhunter-remote packet` when the Mac
has not received the files, without waiting for Syncthing. The browser asks
before overwriting with a `confirm()` when the endpoint answers 409.

The packet also holds `cover-letter.docx`, written by `tools/render_letter.py`
with the standard library only, because neither host has Word, LibreOffice or
python-docx. Wording explanations stay out of the markdown and DOCX letter.
A failed render keeps the markdown packet and drops the docx from
`meta.json`; the review window lists whatever is there.

## Feedback notes

`N` (`note_application`) asks for the words and writes them only on `enter`;
`esc` cancels. The note goes through `tools/dashboard.py note <id> <text>`,
which runs the writer's own `note` verb.

- A note is not a stage change: `stage`, `status` and `stage_at` stay where the
  last move left them, the words land in `note` with a `note_at` stamp, and the
  writer's history keeps them against the stage they were written at. Success
  and fail both need that, because the employer's reasons arrive after the
  stage has already moved.
- The note is one shell-quoted argument, so an apostrophe or a full sentence
  cannot split the command.
- Surfaces without `capabilities.writes` show `run: <command>` and write
  nothing, exactly like a stage key.

## Local terminal and canonical writer

`ui/jobhunter` prefers the repository on the current machine. It runs `tools/ui.py
browse` in the user's current terminal; the menu is not hosted remotely. If that
checkout lacks `~/.hermes/scripts/ams_nudge.py`, the focused TUI action forwards
only the requested `mark` or `bookmark` operation to the server's single writer
through SSH. The list itself reads the local synchronized feed and refreshes from
that feed; it does not rebuild from remote paths. If SSH is unavailable, the UI
reports the write failure and leaves the gauge unchanged.

The terminal renderer repaints the same screen region and clears stale cells
past shorter lines and below the frame. It does not enter the alternate screen,
so terminal selection and copy remain available when mouse reporting is off
(`M` toggles it on temporarily). The terminal's normal scrollback remains outside
the live viewport; the frame is repainted in place rather than appended on each
key press.

## Bookmarks

A bookmark is tracker state, not browser state, so it survives a restart and is
the same on every surface.

- `B` toggles the bookmark on the focused row and writes it through
  `tools/dashboard.py bookmark <id>`, which shells out to
  `ams_nudge.py bookmark <id>`. The tracker stores `bookmarked` and
  `bookmarked_at`; turning it off sets `bookmarked_at` to null.
- A bookmarked card carries a star right after the cursor marker:
  `01 ▶ ★ Example role`. The star never replaces the stage gauge.
- The `Scope` row of filters holds `* Bookmarked`, which keeps only starred
  rows and is cleared by `r` with every other filter.
- `POST /api/bookmark` takes `{"id"}` on the loopback server and answers the
  record the tracker now holds.

## Current-stage filter

The `Current stage` selector filters by the tracker's exact workflow stage, such
as `Generating application`. It is independent of the coarse status, bookmark,
latest-scan, and search facets. In the browser, choose a value from the select;
in the TUI, press `S`, choose a stage from the picker, or press `0` to clear it.
Press `r` to reset all filters. A stage choice is one-at-a-time and combines
with the other active facets.

## Keys on demand

`?` toggles the key list in the listings tab: a `Keys` box that names every
binding in five groups, `Move`, `Open`, `Stage`, `Filter`, `Other`, in one
column, or two columns when the window is at least twice as wide as a row.
Any other key closes it, `?` closes it again, and nothing is written while it
is open. The footer of both the list and the detail view carries a `? keys`
hint so the list is findable without knowing it exists.

## Write contract

`POST /api/mark` accepts `{"id", "status", "note"}` on the loopback server and
answers `{"ok", "id", "status", "previous_status", "stored_stage",
"stored_status", "tracked_date", "output"}`. `ok` is true only when the feed read
back after the write shows exactly what was asked for: `stored_stage` for a stage
request, `stored_status` for a coarse status request. A stage button therefore
cannot report success from a status word the tracker derived on its own, and a
writer that silently drops the value fails the click instead of the data.

After `/api/generate` writes the packet, it requests the `generated` stage and
reads it back. The response keeps packet success separate from tracker success:
`packet_written` may be true while `stage_updated` is false, with `stage_error`
explaining the missing confirmation.

## Limits

- The GUI projection is a reference implementation: it renders the structure
  and its sample rows, and it is not yet the production `DASHBOARD.html`.
  Wiring it into `tools/dashboard.py` is a separate, reviewed change.
- Context is passed in as JSON. Nothing in the structure file reads AMS data
  directly; `tools/feed.py` stays the only normaliser.
- Chips carry no bracket or parenthesis glyphs: a score, a city, a flag, or a
  key hint is plain text separated by spaces and distinguished by colour and
  weight. Tags are never fenced in parentheses, brackets, or corner glyphs.
- Every text group owns a rounded box: list rows, `Match`, `Properties`, each
  AMS section, the action list, the keyboard footer, and transient notices.
  Navigation hints never sit inside listing text.
- Plain hyphens only: no em dash and no en dash in any rendered surface.
- `browse` needs a tty. Without one it prints a single frame and says so, which
  keeps it usable in pipes and test runs.
