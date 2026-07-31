# DESIGN.md — sistem visual dashboard nloop

Dokumen buat sesi berikutnya (manusia atau agent) biar nggak nebak-nebak lagi:
di mana tokennya, komponennya apa aja, dan aturan motion-nya gimana.

Semua yang dibahas di sini ada di `server/static/` — vanilla CSS + JS, **tanpa build
step, tanpa framework, tanpa dependency**. Itu disengaja (lihat "Resource frugality"
di CLAUDE.md); jangan diam-diam nambahin Tailwind/React/Framer Motion.

File:

```
server/static/style.css   satu-satunya stylesheet — token + semua komponen
server/static/common.js   helper + shell sidebar + mesin scroll reveal
server/static/runs.js     halaman Runs
server/static/run.js      halaman Run (waterfall + log)
server/static/tasks.js    halaman Tasks + Task detail
server/static/schedules.js halaman Schedules + watchdog
```

---

## 1. Design token

Semua warna, radius, bayangan, durasi, dan easing ada di `:root` paling atas
`style.css`. **Aturan keras: jangan tulis hex literal di rule manapun.** Kalau butuh
warna baru, tambahin token dulu, baru dipakai. Alasannya sepele: dark/light mode
di-override lewat `@media (prefers-color-scheme: light)` yang cuma nimpa token —
hex yang berserakan nggak ikut ke-override dan bakal keliatan salah di light mode.

### Warna

| token | dipakai buat |
|---|---|
| `--bg` | latar halaman |
| `--surface` / `--surface-2` | card & sidebar / elemen di atas card (bar, chip, hover) |
| `--border` / `--border-strong` | garis normal / garis pas hover & elemen aktif |
| `--text` / `--muted` / `--faint` | teks utama / sekunder / tersier |
| `--accent` (+`--accent-soft`) | biru — running, link, primary, seleksi |
| `--green` (+`-soft`) | succeeded, verify pass |
| `--red` (+`-soft`) | failed, tombol danger |
| `--amber` (+`-soft`) | queued, tool, task, warning |
| `--purple` (+`-soft`) | quality gate |
| `--on-accent` | teks di atas bidang `--accent` (tombol primary, badge live) |
| `--hatch` | arsir bar span yang durasinya cuma taksiran |

Pola `-soft` = versi 12–14% alpha dari warna yang sama, buat background pill/chip.
Border-nya pakai `color-mix(in srgb, var(--x) 35%, transparent)` — konsisten, jangan
bikin varian alpha baru.

### Bentuk & elevasi

`--radius: 10px` (card), 8px (input/tombol), 999px (pill/chip/bar).

| token | kapan |
|---|---|
| `--shadow` | keadaan diam semua `.card` |
| `--shadow-sm` | angkatan kecil: tombol & chip pas hover |
| `--shadow-hover` | card yang bisa diklik pas hover (task card, sched row) |

### Motion

| token | nilai | dipakai buat |
|---|---|---|
| `--dur-fast` | 110ms | transform tombol (press/lift) |
| `--dur` | 180ms | hover pada umumnya (warna, border, shadow) |
| `--dur-slow` | 480ms | scroll reveal |
| `--ease` | `cubic-bezier(.22,.61,.36,1)` | transisi biasa |
| `--ease-out` | `cubic-bezier(.16,1,.3,1)` | apa pun yang "masuk" (reveal, pop, grow) |
| `--lift` | `-2px` | jarak angkat standar pas hover |
| `--stagger` | 55ms | jeda antar item di grid/tabel pas reveal |

---

## 2. Komponen

Bentuk yang udah ada — pakai ulang, jangan bikin varian baru tanpa alasan.

- **`.card`** — surface + border + `--shadow`. Semua panel pakai ini. Tambah `.pad`
  kalau butuh padding standar.
- **`.badge.<status>`** — pill status run (`running` `queued` `succeeded` `failed`
  `stopped`), lengkap sama titik di depan. `running` titiknya berdenyut. Dirakit
  lewat helper `badge(status)` di `common.js` — jangan tulis HTML-nya manual.
- **`.chip`** — metadata kecil. Varian: `.task` (amber), `.role` (biru), `.gate`
  (ungu), `.step` (langkah schedule, ikut warna status). `<a class="chip">` otomatis
  dapet hover angkat.
- **`.bar`** — progress mini (`<div class="bar"><i style="width:%"></i></div>`).
  Varian `.ok` `.bad` `.warn`. Lebarnya beranimasi, jadi **jangan render ulang
  elemennya tiap poll** — update `style.width`-nya aja (lihat §4).
- **`.pill` / `.pills`** — tombol filter. `.on` = kepilih.
- **`.tbl`** di dalam `.table-wrap.card` — tabel padat. `<tr data-goto="/url">` bikin
  barisnya bisa diklik + dapet hover.
- **`.task-card`** — kartu di grid Tasks; hover-nya paling "kerasa" (angkat + shadow
  + ikon nge-zoom) karena ini entry point utama.
- **`.span-row`** — baris waterfall di halaman Run.
- **`.empty`** — keadaan kosong. Selalu kasih tahu *cara ngisinya*, jangan cuma
  "belum ada data".
- **`.hint-box`** — catatan kecil di panel.

---

## 3. Motion

### Scroll reveal

Mesinnya `reveal()` / `revealChildren()` di `common.js` — IntersectionObserver polos,
nggak ada library. Elemen ber-class `reveal` mulai transparan, terus dianimasiin
(`nl-rise`: fade + geser 12px ke atas) begitu masuk viewport.

```js
revealChildren($('#tasks'));                  // grid: tiap anak dapet stagger
revealChildren($('#runs'), ':scope > tr');    // tabel: per baris
reveal();                                     // section statis yang class-nya ditulis di HTML
```

Tiga hal yang gampang kelewat:

1. **Digate `html.motion`**, yang dipasang inline script di `<head>` tiap halaman.
   Tanpa JS, `.reveal` nggak ngefek sama sekali → konten tetap kelihatan. Kalau nambah
   halaman baru, jangan lupa bawa script satu baris itu.
2. **Ada jaring pengaman.** Kalau observer nggak pernah nembak (tab background,
   elemen ke-`display:none`), timer 1,5 detik maksa semua muncul. Animasi nggak boleh
   sampai bikin dashboard blank.
3. **`animation-fill-mode: backwards`, bukan `forwards`** — begitu animasi kelar,
   `transform` balik ke milik elemen sendiri, jadi lift pas hover tetap jalan. Kalau
   pakai `forwards`, `transform: none` dari keyframe bakal ngunci hover-nya.

`--i` (index stagger) di-cap di 12 biar item ke-30 nggak nunggu 1,6 detik.

### Hover

Bahasa gerakannya satu: **angkat + bayangin**, `translateY(var(--lift))` +
`--shadow-sm`/`--shadow-hover`, `--dur`, dan `:active` balik ke `translateY(0)`
biar kerasa ketekan. Yang dapet:

- tombol (semua varian), chip yang berupa link, pill filter
- `.task-card` — plus ikonnya `scale(1.15) rotate(-8deg)` dan nama-nya jadi accent
- `.sched-row`, `.run-row`
- baris tabel — bukan diangkat, tapi background + garis accent `inset 3px` di kiri,
  dan teks goal-nya geser 3px (baris tabel jelek kalau dikasih transform)
- nav sidebar — geser 3px + ikon membesar; logo brand muter dikit
- `.span-row` — background + bar-nya lebih terang

Semua kontrol juga punya `:focus-visible` ring accent. Hover doang nggak cukup —
halaman ini dipakai lewat keyboard.

### Reduced motion

`@media (prefers-reduced-motion: reduce)` matiin semua animasi & transisi global,
dan `.reveal` langsung tampil. `common.js` juga baca `REDUCED` dan nggak masang
observer sama sekali. Sekali lagi: **jangan ada konten yang cuma kelihatan lewat
animasi.**

---

## 4. Aturan render (kenapa polling nggak boleh nulis innerHTML terus)

Dashboard-nya ngepoll: Runs tiap 3 detik, Schedules 5 detik, trace di halaman Run
tiap 1,5 detik. Dulu tiap poll nulis ulang `innerHTML` satu blok penuh, dan itu
diam-diam ngerusak banyak hal:

- `transition: width` di `.bar` **nggak pernah kepakai** — elemennya baru terus, jadi
  nggak ada nilai lama buat ditransisiin. Animasinya ada di CSS tapi nggak pernah jalan.
- state hover ke-reset tiap 3 detik (kursor diam, tapi highlight-nya kedip-kedip)
- teks yang lagi diblok user ilang, fokus keyboard lompat

Polanya sekarang:

- **`runs.js`** — `paintRows()` bandingin urutan id run. Sama → `updateRow()` yang
  cuma nyentuh sel yang berubah lewat `[data-c="..."]` dan nge-set `style.width`
  bar-nya (jadi transisinya beneran jalan). Beda → baru render ulang + reveal.
- **`schedules.js`** — `paint(sel, html)` bandingin string HTML-nya; DOM disentuh
  cuma kalau beda. Fungsinya balikin `changed` supaya listener nggak dipasang dobel.
  Konsekuensinya: tombol yang di-disable manual (`Run now`, `Poll now`) harus
  dibalikin sendiri kalau panelnya ternyata nggak dirender ulang.
- **`run.js`** — klik span pakai `selectSpan()` (toggle class doang), bukan gambar
  ulang seluruh waterfall.

Kalau nambah panel yang ngepoll, ikutin salah satu pola ini. Patokannya:
**yang nggak berubah, jangan disentuh.**

Terkait: log live di halaman Run cuma auto-scroll kalau user emang lagi nempel di
bawah. Kalau lagi scroll ke atas baca sesuatu, tombol "↓ log terbaru" yang muncul —
dulu tiap event maksa `scrollTop`, jadi layarnya ketarik terus pas lagi dibaca.

---

## 5. Nambah halaman baru

1. Copy struktur `<head>` dari halaman yang udah ada — termasuk inline script
   `classList.add('motion')`.
2. `<body data-page="x" data-nav="runs|tasks|schedules">` + `<aside id="sidebar">`.
3. Muat `common.js` dulu, baru script halamannya.
4. Kasih `class="reveal"` ke section statis; panggil `revealChildren()` buat list
   yang dirender JS.
5. Kalau ngepoll, ikutin §4.
