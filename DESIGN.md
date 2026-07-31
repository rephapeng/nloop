# DESIGN.md — the nloop dashboard visual system

A document for the next session (human or agent) so nobody has to guess again:
where the tokens are, what the components are, and how motion works.

Everything here lives in `server/static/` — vanilla CSS + JS, **no build step, no
framework, no dependencies**. That is deliberate (see "Resource frugality" in
CLAUDE.md); don't quietly add Tailwind/React/Framer Motion.

Files:

```
server/static/style.css    the only stylesheet — tokens + every component
server/static/common.js    helpers + sidebar shell + the scroll-reveal engine
server/static/runs.js      Runs page
server/static/run.js       Run page (waterfall + log)
server/static/tasks.js     Tasks page + Task detail
server/static/schedules.js Schedules page + watchdog
```

---

## 1. Design tokens

Every color, radius, shadow, duration, and easing lives in `:root` at the top of
`style.css`. **Hard rule: never write a hex literal in a rule.** If you need a new
color, add a token first, then use it. The reason is simple: dark/light mode is
overridden through `@media (prefers-color-scheme: light)`, which only overwrites
tokens — stray hex values don't get overridden and will look wrong in light mode.

### Color

| token | used for |
|---|---|
| `--bg` | page background |
| `--surface` / `--surface-2` | cards & sidebar / elements on top of a card (bars, chips, hover) |
| `--border` / `--border-strong` | normal rules / hover and active-element rules |
| `--text` / `--muted` / `--faint` | primary / secondary / tertiary text |
| `--accent` (+`--accent-soft`) | blue — running, links, primary, selection |
| `--green` (+`-soft`) | succeeded, verify pass |
| `--red` (+`-soft`) | failed, danger buttons |
| `--amber` (+`-soft`) | queued, tool, task, warning |
| `--purple` (+`-soft`) | quality gate |
| `--on-accent` | text on an `--accent` surface (primary button, live badge) |
| `--hatch` | hatching on span bars whose duration is only estimated |

The `-soft` pattern is a 12–14% alpha version of the same color, for pill/chip
backgrounds. Their borders use `color-mix(in srgb, var(--x) 35%, transparent)` —
keep it consistent, don't invent new alpha variants.

### Shape & elevation

`--radius: 10px` (cards), 8px (inputs/buttons), 999px (pills/chips/bars).

| token | when |
|---|---|
| `--shadow` | resting state of every `.card` |
| `--shadow-sm` | small rise: buttons & chips on hover |
| `--shadow-hover` | clickable cards on hover (task card, sched row) |

### Motion

| token | value | used for |
|---|---|---|
| `--dur-fast` | 110ms | button transforms (press/lift) |
| `--dur` | 180ms | hover in general (color, border, shadow) |
| `--dur-slow` | 480ms | scroll reveal |
| `--ease` | `cubic-bezier(.22,.61,.36,1)` | ordinary transitions |
| `--ease-out` | `cubic-bezier(.16,1,.3,1)` | anything entering (reveal, pop, grow) |
| `--lift` | `-2px` | the standard hover rise |
| `--stagger` | 55ms | delay between grid/table items during reveal |

---

## 2. Components

The shapes that already exist — reuse them, don't add variants without a reason.

- **`.card`** — surface + border + `--shadow`. Every panel uses it. Add `.pad` when
  you want the standard padding.
- **`.badge.<status>`** — run status pill (`running` `queued` `succeeded` `failed`
  `stopped`), dot included. `running` has a pulsing dot. Built by the `badge(status)`
  helper in `common.js` — don't hand-write the markup.
- **`.chip`** — small metadata. Variants: `.task` (amber), `.role` (blue), `.gate`
  (purple), `.step` (schedule step, follows status color). An `<a class="chip">`
  automatically gets the hover rise.
- **`.bar`** — mini progress (`<div class="bar"><i style="width:%"></i></div>`).
  Variants `.ok` `.bad` `.warn`. Its width is animated, so **don't re-render the
  element on every poll** — just update `style.width` (see §4).
- **`.pill` / `.pills`** — filter buttons. `.on` = selected.
- **`.tbl`** inside `.table-wrap.card` — dense table. `<tr data-goto="/url">` makes a
  row clickable and gives it hover treatment.
- **`.task-card`** — card in the Tasks grid; its hover is the most pronounced (rise +
  shadow + icon zoom) because this is the main entry point.
- **`.span-row`** — waterfall row on the Run page.
- **`.empty`** — empty state. Always say *how to fill it*, never just "no data yet".
- **`.hint-box`** — small note inside a panel.

---

## 3. Motion

### Scroll reveal

The engine is `reveal()` / `revealChildren()` in `common.js` — a plain
IntersectionObserver, no library. An element carrying `reveal` starts transparent and
animates in (`nl-rise`: fade + 12px slide up) once it enters the viewport.

```js
revealChildren($('#tasks'));                  // grid: each child gets a stagger
revealChildren($('#runs'), ':scope > tr');    // table: per row
reveal();                                     // static sections that declared the class in HTML
```

Three things that are easy to miss:

1. **It is gated on `html.motion`**, set by an inline script in each page's `<head>`.
   With no JS, `reveal` does nothing at all → content still shows. If you add a page,
   don't forget that one-line script.
2. **There is a safety net.** If the observer never fires (background tab, an element
   under `display:none`), a 1.5-second timer forces everything visible. Animation must
   never blank out the dashboard.
3. **`animation-fill-mode: backwards`, not `forwards`** — once the animation ends,
   `transform` reverts to the element's own, so the hover lift keeps working. With
   `forwards`, the keyframe's `transform: none` would lock hover out.

`--i` (the stagger index) is capped at 12 so item #30 doesn't wait 1.6 seconds.

### Hover

One movement language throughout: **rise + shadow**, `translateY(var(--lift))` plus
`--shadow-sm`/`--shadow-hover` over `--dur`, with `:active` returning to
`translateY(0)` so it feels pressed. It applies to:

- buttons (all variants), chips that are links, filter pills
- `.task-card` — plus its icon at `scale(1.15) rotate(-8deg)` and the name turning accent
- `.sched-row`, `.run-row`
- table rows — not a rise but a background plus a 3px inset accent bar on the left,
  with the goal text sliding 3px (table rows look bad under a transform)
- sidebar nav — slides 3px with the icon scaling up; the brand logo rotates slightly
- `.span-row` — background plus a brighter bar

Every control also has a `:focus-visible` accent ring. Hover alone isn't enough — this
page gets driven from the keyboard too.

### Reduced motion

`@media (prefers-reduced-motion: reduce)` kills every animation and transition
globally, and `.reveal` shows immediately. `common.js` also reads `REDUCED` and skips
installing the observer entirely. Once more: **no content may be visible only through
an animation.**

---

## 4. Render rules (why polling must not rewrite innerHTML)

The dashboard polls: Runs every 3 seconds, Schedules every 5, the run page trace every
1.5. Each tick used to rewrite a whole block's `innerHTML`, and that quietly broke
several things:

- `transition: width` on `.bar` **never ran once** — the element was new every time, so
  there was no old value to transition from. The animation existed in CSS but never played.
- hover state reset every 3 seconds (cursor still, highlight flickering)
- selected text disappeared, keyboard focus jumped

The pattern now:

- **`runs.js`** — `paintRows()` compares the ordered run ids. Same → `updateRow()`
  touches only the cells that changed via `[data-c="..."]` and sets the bar's
  `style.width` (so the transition actually plays). Different → full re-render + reveal.
- **`schedules.js`** — `paint(sel, html)` compares the HTML string; the DOM is only
  touched when it differs. It returns `changed` so listeners aren't bound twice. The
  consequence: buttons disabled by hand (`Run now`, `Poll now`) must be restored
  manually when the panel turns out not to have been re-rendered.
- **`run.js`** — clicking a span goes through `selectSpan()` (a class toggle), not a
  full waterfall redraw.

If you add a polling panel, follow one of these. The rule of thumb: **don't touch what
didn't change.**

Related: the live log on the Run page only auto-scrolls when the user is already stuck
to the bottom. Scroll up to read something and a "↓ latest" button appears instead —
every event used to force `scrollTop`, which yanked the view away mid-read.

---

## 5. Adding a new page

1. Copy the `<head>` structure from an existing page — including the inline
   `classList.add('motion')` script.
2. `<body data-page="x" data-nav="runs|tasks|schedules">` + `<aside id="sidebar">`.
3. Load `common.js` first, then the page script.
4. Put `class="reveal"` on static sections; call `revealChildren()` for lists
   rendered by JS.
5. If it polls, follow §4.
