# Forensic Ledger UI Makeover — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restyle seluruh UI Truth of Auditor menjadi "Forensic Ledger" — halaman kerja paper-light, login ink-dark dengan wordmark partikel Three.js — tanpa mengubah view/model/URL/alur HTMX (satu pengecualian: +1 baris context di `dashboard`).

**Architecture:** Design system diekstrak dari inline `<style>` `app_base.html` ke `web/static/web/css/app.css` dan di-retheme (vocabulary class dipertahankan: `.card/.badge/.btn/.stat/.side/...` → template edit minimal). Login dapat shell ink + partikel teks via Three.js ESM importmap (login-only). JS presentasional diekstrak ke module static dengan re-init hook htmx. No build toolchain.

**Tech Stack:** Django 5.2 templates, CSS custom (no Tailwind), GSAP 3.12.5 UMD (CDN, existing), htmx 1.9.12 (CDN, existing), Three.js 0.167.1 ESM via importmap (login only), font self-host woff2 (Zodiak, Supreme, IBM Plex Mono), whitenoise `CompressedManifestStaticFilesStorage`.

**Spec:** `docs/superpowers/specs/2026-07-03-forensic-ledger-ui-makeover-design.md`

## Global Constraints

- Branch kerja: `feat/forensic-ledger-ui`. Commit per task.
- **102 test existing harus tetap pass setiap task** (`.venv/bin/python -m django test --settings=truth_auditor.settings`). Test count akan bertambah — yang dilarang: regresi.
- Tidak ada perubahan `views.py`/`models.py`/`urls.py` KECUALI pengecualian tunggal Task 4 (context `batches` di `dashboard`).
- Lenis DIBUANG dari halaman kerja & login. GSAP + htmx CDN tetap (pinned versi yang sudah ada).
- Grain hanya di: sidebar/topbar (chrome), login, empty-state (`.dz`, `no_toko`). DILARANG di tabel/panel data.
- Zodiak hanya untuk `h1` page-head, folio, wordmark login. Body = Supreme. SEMUA angka = IBM Plex Mono (`.num`/`.mono` + `tabular-nums`).
- Warna semantik bucket dipertahankan maknanya: ok/hijau `#1F7A4D`, warn/amber `#9A6B15`, bad/merah `#B3362A`.
- `prefers-reduced-motion` mematikan semua animasi; no-WebGL → fallback CSS. DPR clamp ≤2, partikel ≤20k desktop / ≤7k mobile.
- File CSS/JS baru harus lolos `collectstatic` dengan manifest hashing (path relatif di CSS: `url("../fonts/...")`).

---

### Task 1: Font self-host + `fonts.css`

**Files:**
- Create: `web/static/web/fonts/` (woff2: Zodiak-Bold, Supreme-Variable/Regular+Medium+Bold, IBMPlexMono-Regular+Medium)
- Create: `web/static/web/css/fonts.css`
- Test: `web/tests_ui.py` (baru)

**Interfaces:**
- Produces: font-family names `'Zodiak'`, `'Supreme'`, `'IBM Plex Mono'` dipakai `app.css` (Task 2) dan `login.css` (Task 8). Path CSS: `{% static 'web/css/fonts.css' %}`.

- [ ] **Step 1: Tulis failing test**

```python
# web/tests_ui.py
from django.contrib.staticfiles import finders
from django.test import TestCase


class StaticAssetTests(TestCase):
    def test_fonts_css_ditemukan(self):
        self.assertIsNotNone(finders.find("web/css/fonts.css"))

    def test_font_woff2_ada(self):
        for f in [
            "web/fonts/Zodiak-Bold.woff2",
            "web/fonts/Supreme-Regular.woff2",
            "web/fonts/Supreme-Medium.woff2",
            "web/fonts/Supreme-Bold.woff2",
            "web/fonts/IBMPlexMono-Regular.woff2",
            "web/fonts/IBMPlexMono-Medium.woff2",
        ]:
            self.assertIsNotNone(finders.find(f), f)
```

- [ ] **Step 2: Run test, verifikasi FAIL**

Run: `.venv/bin/python -m django test web.tests_ui --settings=truth_auditor.settings`
Expected: FAIL (assets belum ada)

- [ ] **Step 3: Download font**

```bash
cd /tmp && mkdir -p fontdl && cd fontdl
curl -sL -o zodiak.zip "https://api.fontshare.com/v2/fonts/download/zodiak"
curl -sL -o supreme.zip "https://api.fontshare.com/v2/fonts/download/supreme"
unzip -o zodiak.zip -d zodiak && unzip -o supreme.zip -d supreme
# IBM Plex Mono dari repo resmi IBM
curl -sL -o plex.zip "https://github.com/IBM/plex/releases/download/%40ibm%2Fplex-mono%401.1.0/ibm-plex-mono.zip"
unzip -o plex.zip -d plex
mkdir -p /Users/macmini/projects/Truth-Auditor/web/static/web/fonts
# salin woff2 yang dibutuhkan (nama file di zip bisa beda tipis — cari dengan find lalu rename ke nama kanonik test)
find zodiak -name "*Zodiak-Bold*.woff2" -not -name "*Italic*" -exec cp {} /Users/macmini/projects/Truth-Auditor/web/static/web/fonts/Zodiak-Bold.woff2 \;
for w in Regular Medium Bold; do
  find supreme -name "*Supreme-$w*.woff2" -not -name "*Italic*" -exec cp {} /Users/macmini/projects/Truth-Auditor/web/static/web/fonts/Supreme-$w.woff2 \;
done
for w in Regular Medium; do
  find plex -path "*woff2*" -name "IBMPlexMono-$w.woff2" -exec cp {} /Users/macmini/projects/Truth-Auditor/web/static/web/fonts/IBMPlexMono-$w.woff2 \;
done
ls -la /Users/macmini/projects/Truth-Auditor/web/static/web/fonts/
```

Expected: 6 file woff2. **Fallback bila URL mati:** unduh manual dari fontshare.com / github.com/IBM/plex, nama file kanonik sama. Verifikasi magic bytes: `file *.woff2` → "Web Open Font Format".

- [ ] **Step 4: Tulis `web/static/web/css/fonts.css`**

```css
@font-face{font-family:'Zodiak';src:url("../fonts/Zodiak-Bold.woff2") format("woff2");font-weight:700;font-style:normal;font-display:swap}
@font-face{font-family:'Supreme';src:url("../fonts/Supreme-Regular.woff2") format("woff2");font-weight:400;font-style:normal;font-display:swap}
@font-face{font-family:'Supreme';src:url("../fonts/Supreme-Medium.woff2") format("woff2");font-weight:500;font-style:normal;font-display:swap}
@font-face{font-family:'Supreme';src:url("../fonts/Supreme-Bold.woff2") format("woff2");font-weight:700;font-style:normal;font-display:swap}
@font-face{font-family:'IBM Plex Mono';src:url("../fonts/IBMPlexMono-Regular.woff2") format("woff2");font-weight:400;font-style:normal;font-display:swap}
@font-face{font-family:'IBM Plex Mono';src:url("../fonts/IBMPlexMono-Medium.woff2") format("woff2");font-weight:500;font-style:normal;font-display:swap}
```

- [ ] **Step 5: Run test → PASS; verifikasi collectstatic**

Run: `.venv/bin/python -m django test web.tests_ui --settings=truth_auditor.settings` → PASS
Run: `.venv/bin/python manage.py collectstatic --noinput 2>&1 | tail -2` → tanpa error manifest (url relatif `../fonts/` ter-resolve)

- [ ] **Step 6: Commit**

```bash
git add web/static/web/fonts web/static/web/css/fonts.css web/tests_ui.py
git commit -m "feat(ui): self-host font Zodiak/Supreme/IBM Plex Mono + fonts.css"
```

---

### Task 2: `app.css` — design system Forensic Ledger (paper-light)

**Files:**
- Create: `web/static/web/css/app.css`
- Test: `web/tests_ui.py` (tambah)

**Interfaces:**
- Consumes: font-family dari Task 1.
- Produces: seluruh class existing (`.side .topbar .content .page-head .grid .cols-* .card .stat .badge .btn .field .row .twrap .table-wrap .tabs .msg .dz .reveal .num .mono .muted .faint .pager .toko-pick .who .sec .link .brand .crumb .spacer .main .pad0 .r .flush`) tetap valid + class baru: `.folio` (numeral ghost), `.stamp` (varian badge), `.hrail` (rail horizontal), `.grain` (overlay chrome), `.margin-note`. Dipakai Task 3–7.

- [ ] **Step 1: Failing test**

```python
# tambah di web/tests_ui.py
    def test_app_css_ditemukan(self):
        self.assertIsNotNone(finders.find("web/css/app.css"))
```

Run → FAIL.

- [ ] **Step 2: Tulis `app.css`**

Struktur file — **salin semua rule struktural dari `web/templates/web/app_base.html` baris 24–149 apa adanya** (layout flex/grid/padding TIDAK berubah), lalu terapkan perubahan berikut. Bagian A–C ditulis lengkap di bawah; sisanya substitusi nilai.

**A. Token `:root` (ganti seluruh blok lama):**

```css
:root{
  --bg:#EDE9DE; --panel:#F7F5EE; --panel-2:#F1EDE2;
  --ink:#1C1A15; --muted:#5C574B; --faint:#8A8474;
  --line:rgba(28,26,21,.14); --line-2:rgba(28,26,21,.24);
  --side:#161511; --side-2:#1E1C16; --side-ink:#E9E4D6; --side-muted:#8A8474;
  --brand:#2E7D5B; --brand-2:#B3362A; --grad:none;
  --ok:#1F7A4D; --ok-bg:#E3EFE4; --warn:#9A6B15; --warn-bg:#F4EAD2; --bad:#B3362A; --bad-bg:#F6E3DF;
  --radius:6px; --radius-sm:4px;
  --shadow:0 1px 0 rgba(28,26,21,.06); --shadow-lg:0 14px 30px -18px rgba(28,26,21,.25);
  --font-display:'Zodiak',Georgia,serif; --font-body:'Supreme',system-ui,sans-serif; --font-mono:'IBM Plex Mono',ui-monospace,monospace;
}
body{font-family:var(--font-body)}
h1,h2,h3,.disp{font-family:var(--font-body);letter-spacing:-.01em}
.page-head h1{font-family:var(--font-display);font-weight:700;letter-spacing:-.015em}
.num,.mono{font-family:var(--font-mono);font-variant-numeric:tabular-nums;font-feature-settings:"tnum";font-size:.94em}
.grad-text{background:none;color:var(--brand);-webkit-text-fill-color:currentColor}
```

**B. Komponen baru (tulis lengkap):**

```css
/* Folio — numeral dossier ghost per halaman */
.page-wrap{position:relative}
.folio{position:absolute;top:-6px;right:0;font-family:var(--font-mono);font-size:12px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--faint);user-select:none}
.folio b{font-family:var(--font-display);font-weight:700;font-size:64px;line-height:.8;display:block;
  text-align:right;color:rgba(28,26,21,.07);letter-spacing:-.02em}
@media(max-width:900px){.folio b{font-size:40px}}

/* Stamp — status chip gaya cap dokumen (menimpa gaya .badge lama) */
.badge{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border-radius:3px;
  font-family:var(--font-mono);font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.07em;
  border:1px solid currentColor}
.badge:before{display:none}
.badge.ok{color:var(--ok);background:var(--ok-bg)} .badge.warn{color:var(--warn);background:var(--warn-bg)}
.badge.bad{color:var(--bad);background:var(--bad-bg)} .badge.src{color:#4A4433;background:#ECE7D8}

/* Grain — chrome & empty-state only (data-URI feTurbulence) */
.grain{position:relative;isolation:isolate}
.grain::after{content:"";position:absolute;inset:0;z-index:-1;opacity:.05;pointer-events:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
.side.grain::after{z-index:0;opacity:.07;mix-blend-mode:overlay}

/* Rail horizontal (dashboard batch) */
.hrail{display:flex;gap:14px;overflow-x:auto;scroll-snap-type:x mandatory;padding:2px 2px 10px;scrollbar-width:thin}
.hrail>*{scroll-snap-align:start;flex:0 0 300px}

/* Document frame + margin note */
@media(min-width:1200px){.content{border-left:1px solid var(--line)}}
.margin-note{display:none}
@media(min-width:1440px){.margin-note{display:block;font-family:var(--font-mono);font-size:11px;
  color:var(--faint);text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px}}

/* View Transitions (progressive) */
@view-transition{navigation:auto}
@media(prefers-reduced-motion:reduce){::view-transition-group(*),::view-transition-old(*),::view-transition-new(*){animation:none!important}}

/* Print — transaksi & run detail */
@media print{
  .side,.topbar,.btn,.tabs,.pager,form.toko-pick,.folio{display:none!important}
  body{background:#fff;color:#000;display:block}
  .content{max-width:none;padding:0;border:none}
  .card,.twrap,.table-wrap{border:1px solid #999;box-shadow:none;border-radius:0}
  thead th{background:#eee;color:#000}
}
```

**C. Substitusi nilai pada rule struktural yang disalin** (mapping eksplisit — terapkan persis):

| Selector lama | Perubahan |
|---|---|
| `.side .brand .logo` | `background:var(--side-2);box-shadow:none;border:1px solid rgba(233,228,214,.2)`; svg `color:var(--side-ink)` |
| `.side a.link.active:before` | `background:var(--brand)` (bukan grad) |
| `.side a.link.active svg` | `color:var(--side-ink)` |
| `.topbar` | `background:rgba(237,233,222,.85)` (bone translucent) |
| `.toko-pick svg` | `color:var(--brand)` |
| `.stat .ic` | `background:var(--panel-2);color:var(--ink);border:1px solid var(--line)` |
| `.stat .v` | `font-family:var(--font-mono);font-weight:500` |
| `.btn.primary` | `background:var(--ink);color:var(--panel);box-shadow:none;border:1px solid var(--ink)` hover: `background:#000` |
| `.btn` | hapus `transform:translateY(-1px)` hover → ganti `border-color:var(--line-2);background:var(--panel-2)` |
| `input:focus,...` | `border-color:var(--brand);box-shadow:0 0 0 3px rgba(46,125,91,.15)` |
| `thead th` | `font-family:var(--font-mono);letter-spacing:.08em` |
| `.tabs a.active` | `background:var(--ink);border-color:var(--ink)` |
| `.dz:hover` | `border-color:var(--brand);background:var(--panel-2)` |
| `.dz .di` | `background:var(--panel-2);color:var(--brand);border:1px solid var(--line)` |
| `.badge.*` blok lama (baris 92–97) | JANGAN disalin — sudah diganti blok stamp di B |

- [ ] **Step 3: Run test → PASS; suite penuh → PASS; collectstatic OK**

- [ ] **Step 4: Commit**

```bash
git add web/static/web/css/app.css web/tests_ui.py
git commit -m "feat(ui): app.css design system Forensic Ledger paper-light"
```

---

### Task 3: Rewire `app_base.html` + `motion.js`

**Files:**
- Modify: `web/templates/web/app_base.html` (head baris 8–13, hapus `<style>` 14–150, hapus `<script>` 203–239, markup sidebar/topbar/content)
- Create: `web/static/web/js/motion.js`
- Test: `web/tests_ui.py` (tambah)

**Interfaces:**
- Consumes: `app.css`/`fonts.css` (Task 1–2).
- Produces: block template `{% block folio %}` (dipakai Task 4–7); `motion.js` global `window.ToaMotion.init(root)` + auto re-init pada `htmx:afterSwap`; block existing `{% block head %}`/`{% block scripts %}` tetap.

- [ ] **Step 1: Failing test**

```python
# tambah di web/tests_ui.py
from django.contrib.auth import get_user_model
from sources.models import Toko


class ShellTests(TestCase):
    def setUp(self):
        self.toko = Toko.objects.filter(is_active=True).first() or Toko.objects.create(key="lbs", name="LBS", is_active=True)
        U = get_user_model()
        self.admin = U.objects.create_superuser("uiadmin", password="rahasia-123")
        self.client.force_login(self.admin)

    def test_shell_pakai_app_css_dan_motion_js(self):
        r = self.client.get("/")
        self.assertContains(r, "web/css/app.css")
        self.assertContains(r, "web/js/motion.js")
        self.assertNotContains(r, "lenis")          # Lenis dibuang
        self.assertNotContains(r, "fonts.googleapis") # Google Fonts dibuang
        self.assertContains(r, 'class="folio"')
```

Run → FAIL.

- [ ] **Step 2: Edit head `app_base.html`**

Ganti baris 8–13 menjadi:

```html
<link rel="preload" href="{% static 'web/fonts/Supreme-Regular.woff2' %}" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="{% static 'web/fonts/Zodiak-Bold.woff2' %}" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="{% static 'web/css/fonts.css' %}">
<link rel="stylesheet" href="{% static 'web/css/app.css' %}">
<script src="https://unpkg.com/htmx.org@1.9.12"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js"></script>
```

Hapus blok `<style>…</style>` (baris 14–150).

- [ ] **Step 3: Edit markup shell**

1. `<aside class="side">` → `<aside class="side grain">`
2. Dalam `.content`, bungkus: setelah `{% if messages %}…{% endif %}` tambahkan sebelum `{% block content %}`:

```html
<div class="page-wrap">
  <div class="folio" aria-hidden="true">{% block folio %}{% endblock %}</div>
  {% block content_inner %}{% endblock %}
</div>
```

CATATAN: `{% block content %}` LAMA dipertahankan untuk kompatibilitas — struktur final:

```html
<div class="content">
  {% if messages %}{% for m in messages %}<div class="msg {{ m.tags }}">{{ m }}</div>{% endfor %}{% endif %}
  <div class="page-wrap">
    <div class="folio" aria-hidden="true">{% block folio %}{% endblock %}</div>
    {% block content %}{% endblock %}
  </div>
</div>
```

3. Ganti blok `<script>(function(){…})()</script>` (baris 203–239) dengan:

```html
<script src="{% static 'web/js/motion.js' %}" defer></script>
```

- [ ] **Step 4: Tulis `web/static/web/js/motion.js`** (lengkap)

```js
/* Truth of Auditor — motion layer (Forensic Ledger). GSAP UMD global; htmx-aware. */
(function () {
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  function countUp(el) {
    var to = parseFloat(el.getAttribute('data-count')) || 0, dur = 900, t0 = null;
    function step(ts) {
      if (!t0) t0 = ts;
      var p = Math.min((ts - t0) / dur, 1), e = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(to * e).toLocaleString('id-ID');
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function init(root) {
    root = root || document;
    if (window.gsap && !reduce) {
      var els = root.querySelectorAll('.reveal:not([data-motion-done])');
      if (els.length) {
        els.forEach(function (el) { el.setAttribute('data-motion-done', '1'); });
        gsap.set(els, { opacity: 1 });
        gsap.from(els, { y: 14, opacity: 0, duration: .55, ease: 'power3.out', stagger: .05 });
      }
    }
    root.querySelectorAll('[data-count]:not([data-motion-done])').forEach(function (el) {
      el.setAttribute('data-motion-done', '1');
      reduce ? el.textContent = (parseFloat(el.getAttribute('data-count')) || 0).toLocaleString('id-ID') : countUp(el);
    });
  }

  window.ToaMotion = { init: init };
  if (document.readyState !== 'loading') init(); else document.addEventListener('DOMContentLoaded', function () { init(); });
  document.body && document.body.addEventListener('htmx:afterSwap', function (e) { init(e.target); });
  // htmx belum tentu loaded saat defer — pasang juga saat DOM siap:
  document.addEventListener('DOMContentLoaded', function () {
    document.body.addEventListener('htmx:afterSwap', function (e) { init(e.target); });
  });
})();
```

(Magnetic button lama sengaja tidak dibawa — brief Lusion restraint; Lenis dibuang.)

- [ ] **Step 5: Run test tambahan → PASS; suite penuh 102+ → PASS**

- [ ] **Step 6: Commit**

```bash
git add web/templates/web/app_base.html web/static/web/js/motion.js web/tests_ui.py
git commit -m "feat(ui): rewire shell ke static app.css/motion.js, folio block, drop Lenis"
```

---

### Task 4: Dashboard — folio, KPI, rail batch horizontal

**Files:**
- Modify: `web/views.py:53-61` (context `dashboard` +1 baris — PENGECUALIAN yang diizinkan spec)
- Modify: `web/templates/web/dashboard.html`
- Test: `web/tests_ui.py` (tambah)

**Interfaces:**
- Consumes: `.folio`, `.hrail`, `.badge`, `.stat` dari Task 2–3; `ReconBatch` (`reconciliation.models`).
- Produces: pola folio per halaman `{% block folio %}<b>01</b>DASBOR{% endblock %}` — Task 5–7 mengikuti pola sama dengan nomor 02–06.

- [ ] **Step 1: Failing test**

```python
class DashboardUiTests(ShellTests.__bases__[0]):  # TestCase
    def setUp(self):
        from sources.models import Toko
        from django.contrib.auth import get_user_model
        self.toko = Toko.objects.filter(is_active=True).first() or Toko.objects.create(key="lbs", name="LBS", is_active=True)
        self.admin = get_user_model().objects.create_superuser("uiadmin2", password="rahasia-123")
        self.client.force_login(self.admin)

    def test_dashboard_folio_dan_rail(self):
        r = self.client.get("/")
        self.assertContains(r, "DASBOR")
        self.assertContains(r, "hrail")
        self.assertIn("batches", r.context)
```

Run → FAIL (`batches` belum di context).

- [ ] **Step 2: View — tambah context**

Di `web/views.py` fungsi `dashboard`, import sudah ada `MatchRun` dari `reconciliation.models` — tambahkan `ReconBatch` pada import yang sama, lalu di `ctx` tambah:

```python
        "batches": ReconBatch.objects.filter(toko=active).order_by("-id")[:10],
```

- [ ] **Step 3: Template `dashboard.html`**

1. Tambah di atas: `{% block folio %}<b>01</b>DASBOR{% endblock %}`
2. Tambah section rail sebelum tabel/list recent yang ada:

```html
<div class="card reveal" style="margin-bottom:16px">
  <div class="h-row"><h3>Batch Rekonsiliasi Terakhir</h3><span class="margin-note">arsip · geser →</span></div>
  <div class="hrail">
    {% for b in batches %}
    <a class="card" href="{% url 'batch_detail' b.pk %}" style="box-shadow:none">
      <div class="muted" style="font-size:12px">Batch <span class="num">#{{ b.pk }}</span></div>
      <div class="num" style="font-size:20px;font-weight:500;margin:6px 0">{{ b.date_from|date:"d M" }}–{{ b.date_to|date:"d M Y" }}</div>
      {% with s=b.summary %}
      <span class="badge ok">{{ s.cocok|default:0 }} cocok</span>
      <span class="badge warn">{{ s.perlu_tinjau|default:0 }} tinjau</span>
      {% endwith %}
    </a>
    {% empty %}<span class="muted">Belum ada batch.</span>{% endfor %}
  </div>
</div>
```

(Perhatikan struktur `summary` JSON aktual — kunci agregat batch ada di `reconciliation/engine.py:run_batch`; sesuaikan nama kunci `cocok`/`perlu_tinjau` dengan yang tertulis di engine bila berbeda.)

3. Angka KPI existing: pastikan elemen nilai memakai class `num` + `data-count`.

- [ ] **Step 4: Run test → PASS; suite penuh → PASS**

- [ ] **Step 5: Commit**

```bash
git add web/views.py web/templates/web/dashboard.html web/tests_ui.py
git commit -m "feat(ui): dashboard folio 01 + rail horizontal batch + KPI mono"
```

---

### Task 5: Upload + Transaksi

**Files:**
- Modify: `web/templates/web/upload.html`, `web/templates/web/transactions.html`
- Test: `web/tests_ui.py` (tambah)

**Interfaces:**
- Consumes: pola folio Task 4; class `.dz .badge .twrap .num`.
- Produces: —

- [ ] **Step 1: Failing test**

```python
    def test_folio_upload_transaksi(self):
        self.assertContains(self.client.get("/upload/"), "UNGGAH")
        self.assertContains(self.client.get("/transactions/"), "TRANSAKSI")
```

- [ ] **Step 2: Edit template**

- `upload.html`: `{% block folio %}<b>02</b>UNGGAH{% endblock %}`; drop-zone dapat class `grain` (`<div class="dz grain">`); tabel riwayat: kolom angka pakai `num`; status upload pakai `badge` semantik yang sudah ada.
- `transactions.html`: `{% block folio %}<b>03</b>TRANSAKSI{% endblock %}`; semua sel nominal/ticket pakai `num`; filter bar tetap (styling ikut app.css otomatis).
- Markup form/input/htmx TIDAK diubah.

- [ ] **Step 3: Test → PASS; suite penuh → PASS. Commit**

```bash
git add web/templates/web/upload.html web/templates/web/transactions.html web/tests_ui.py
git commit -m "feat(ui): folio + table chrome upload & transaksi"
```

---

### Task 6: Reconcile + batch/run detail + overlay processing

**Files:**
- Modify: `web/templates/web/reconcile.html`, `batch_detail.html`, `run_detail.html`, `_result_row.html`
- Create: `web/static/web/js/recon-overlay.js`
- Test: `web/tests_ui.py` (tambah)

**Interfaces:**
- Consumes: folio (04 REKONSILIASI / 04·B / 04·R), `.badge` stamp, `.num`.
- Produces: overlay dipicu submit form run rekonsiliasi (`form#run-form` di `reconcile.html`).

- [ ] **Step 1: Failing test**

```python
    def test_folio_reconcile(self):
        r = self.client.get("/reconcile/")
        self.assertContains(r, "REKONSILIASI")
        self.assertContains(r, "recon-overlay.js")
```

- [ ] **Step 2: Template**

- `reconcile.html`: folio `<b>04</b>REKONSILIASI`; checklist kelengkapan pakai `badge ok/warn`; form run diberi `id="run-form"`; tambah `{% block scripts %}<script src="{% static 'web/js/recon-overlay.js' %}" defer></script>{% endblock %}` (+ `{% load static %}` bila belum).
- `batch_detail.html`: folio `<b>04</b>BATCH`; kartu DP/WD: nilai `num`, indikator Balanced pakai `badge ok` / `badge warn` (stamp otomatis).
- `run_detail.html` + `_result_row.html`: bucket chip = `badge ok|warn|bad` (cek mapping class yang sudah dipakai template; stamp restyle otomatis dari Task 2). Angka `num`. HTMX attrs TIDAK disentuh.

- [ ] **Step 3: Tulis `recon-overlay.js`** (lengkap — canvas 2D ringan, non-WebGL)

```js
/* Overlay "menyusun dossier" saat run rekonsiliasi disubmit. */
(function () {
  var form = document.getElementById('run-form');
  if (!form) return;
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  form.addEventListener('submit', function () {
    var ov = document.createElement('div');
    ov.setAttribute('style',
      'position:fixed;inset:0;z-index:99;display:grid;place-items:center;' +
      'background:rgba(28,26,21,.86);color:#E9E4D6;font-family:"IBM Plex Mono",monospace');
    ov.innerHTML = '<div style="text-align:center"><canvas id="ov-c" width="260" height="80"></canvas>' +
      '<div style="font-size:12px;letter-spacing:.14em;text-transform:uppercase;margin-top:14px">Menyusun rekonsiliasi…</div></div>';
    document.body.appendChild(ov);
    if (reduce) return;
    var c = ov.querySelector('#ov-c'), x = c.getContext('2d'), N = 140, P = [];
    for (var i = 0; i < N; i++) P.push({ x: Math.random() * 260, y: Math.random() * 80, tx: 20 + (i % 28) * 8, ty: 24 + Math.floor(i / 28) * 8, v: .02 + Math.random() * .04 });
    (function tick() {
      x.clearRect(0, 0, 260, 80); x.fillStyle = '#E9E4D6';
      P.forEach(function (p) { p.x += (p.tx - p.x) * p.v; p.y += (p.ty - p.y) * p.v; x.fillRect(p.x, p.y, 2, 2); });
      if (document.body.contains(ov)) requestAnimationFrame(tick);
    })();
  });
})();
```

- [ ] **Step 4: Test → PASS; suite penuh → PASS. Commit**

```bash
git add web/templates/web/reconcile.html web/templates/web/batch_detail.html web/templates/web/run_detail.html web/templates/web/_result_row.html web/static/web/js/recon-overlay.js web/tests_ui.py
git commit -m "feat(ui): folio recon/batch/run + stamp bucket + overlay processing"
```

---

### Task 7: Kelola (user/toko/edit) + `no_toko` empty-state

**Files:**
- Modify: `web/templates/web/kelola/users.html`, `kelola/user_edit.html`, `kelola/toko.html`, `web/templates/web/no_toko.html`
- Test: `web/tests_ui.py` (tambah)

**Interfaces:**
- Consumes: folio 05 PENGGUNA / 06 TOKO; class existing.

- [ ] **Step 1: Failing test**

```python
    def test_folio_kelola(self):
        self.assertContains(self.client.get("/kelola/user/"), "PENGGUNA")
        self.assertContains(self.client.get("/kelola/toko/"), "TOKO")
```

- [ ] **Step 2: Edit** — folio per halaman; `no_toko.html`: bungkus pesan dalam `<div class="card grain" style="text-align:center;padding:60px">`; tabel & form ikut app.css. **HATI-HATI:** `tests_kelola.py` meng-assert isi halaman — jangan ubah teks label/menu ("Pengguna", "Toko", dst).

- [ ] **Step 3: Test → PASS; suite penuh → PASS. Commit**

```bash
git add web/templates/web/kelola web/templates/web/no_toko.html web/tests_ui.py
git commit -m "feat(ui): folio kelola + empty-state bergrain"
```

---

### Task 8: Login — shell ink + wordmark partikel Three.js

**Files:**
- Modify: `web/templates/web/base.html` (retheme ink forensic; hanya di-extend login)
- Modify: `web/templates/registration/login.html` (ganti particle field lama → text-as-particles)
- Create: `web/static/web/css/login.css`, `web/static/web/js/login-hero.js`
- Test: `web/tests_ui.py` (tambah)

**Interfaces:**
- Consumes: fonts (Task 1).
- Produces: — (terminal; tak ada task lain bergantung)

- [ ] **Step 1: Failing test**

```python
class LoginUiTests(TestCase):
    def test_login_pakai_hero_baru(self):
        r = self.client.get("/accounts/login/")
        self.assertContains(r, "login-hero.js")
        self.assertContains(r, "importmap")
        self.assertNotContains(r, "three.js/r128")   # UMD lama dibuang
        self.assertNotContains(r, "lenis")
```

(Cek URL login aktual via `python manage.py show_urls` atau `reverse("login")` — settings `LOGIN_URL="login"`.)

- [ ] **Step 2: `base.html` retheme**

Ganti token `:root` (baris 15–22) → palet ink forensic:

```css
:root{
  --bg:#12100C; --bg2:#171410; --panel:rgba(233,228,214,.04); --panel2:rgba(233,228,214,.07);
  --border:rgba(233,228,214,.12); --border2:rgba(233,228,214,.2);
  --text:#E9E4D6; --muted:#A39D8C; --faint:#6E6A5D;
  --brand:#3FA478; --brand2:#C98A2B; --grad:none;
  --ok:#3FA478; --warn:#C98A2B; --bad:#D06A5C;
  --radius:6px; --radius-sm:4px;
}
```

- Hapus `radial-gradient` background-image body (baris 28–31) → ganti `background:var(--bg)`.
- Google Fonts (baris 8–10) → `{% static 'web/css/fonts.css' %}`; `h1,h2,h3,.brand{font-family:'Zodiak',Georgia,serif}`.
- Hapus `<script>` Lenis (baris 13). GSAP tetap.
- Tambah grain body: `body::after{content:"";position:fixed;inset:0;pointer-events:none;opacity:.06;background-image:url("data:image/svg+xml,...")}` (data-URI sama dengan Task 2).

- [ ] **Step 3: `login.html` — hapus Three UMD + particle field lama, pasang hero baru**

- Hapus `<script src=".../three.js/r128/three.min.js">` dan seluruh IIFE particle field lama (blok `<script>` berisi comment roll 2026-07-02).
- Tambah di head (block head):

```html
<link rel="stylesheet" href="{% static 'web/css/login.css' %}">
<script type="importmap">{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.167.1/build/three.module.js"}}</script>
```

- Canvas tetap `<canvas id="gl"></canvas>`; tambah elemen fallback wordmark: `<div id="wordmark-fallback" class="wm-fallback" aria-hidden="true">TRUTH OF AUDITOR</div>` (tampil bila WebGL mati).
- Di akhir body: `<script type="module" src="{% static 'web/js/login-hero.js' %}"></script>`

- [ ] **Step 4: Tulis `login.css`** (lengkap)

```css
.wm-fallback{position:fixed;inset:0;display:none;place-items:center;font-family:'Zodiak',Georgia,serif;
  font-size:clamp(28px,6vw,72px);color:#E9E4D6;letter-spacing:.02em;pointer-events:none;opacity:.9}
.no-webgl .wm-fallback,.reduced .wm-fallback{display:grid}
.no-webgl #gl,.reduced #gl{display:none}
#gl{position:fixed;inset:0;width:100%;height:100%}
.auth-card{position:relative;z-index:2}
```

- [ ] **Step 5: Tulis `login-hero.js`** (lengkap — sertakan comment block roll WAJIB)

```js
/* ───────────────────────────────────────────────────────────────
 * build-premium-website-twist-threejs — random roll (this round)
 *   WebGL effect : Text rendered as particles that assemble/scatter (trend-confirmed: Codrops Gommage 2026-01, dissolve particles 2025-02)
 *   Motion       : Page transition wipe/preloader · Horizontal scroll rail (app-adapted)
 *   Visual       : Grainy/noise texture overlay (re-rolled from Editorial — ledger conflict LumisLinks)
 *   Anchor       : Lusion / Locomotive agency style
 *   Refs adapted : tympanus.net Gommage → noise-driven progress; codrops dissolve → per-particle seed ease; existing login r128 field → replaced
 *   Date         : 2026-07-03
 * ─────────────────────────────────────────────────────────────── */
import * as THREE from 'three';

const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
const canvas = document.getElementById('gl');
const isMobile = matchMedia('(max-width: 768px)').matches;

function bail(cls) { document.documentElement.classList.add(cls); }
if (reduce) { bail('reduced'); }
else {
  let renderer = null;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: true, powerPreference: 'high-performance' });
  } catch (e) { renderer = null; }
  if (!renderer) { bail('no-webgl'); }
  else init(renderer);
}

function sampleText(text) {
  const W = 1200, H = 220, c = document.createElement('canvas');
  c.width = W; c.height = H;
  const x = c.getContext('2d');
  x.fillStyle = '#fff';
  x.font = '700 150px Zodiak, Georgia, serif';
  x.textAlign = 'center'; x.textBaseline = 'middle';
  x.fillText(text, W / 2, H / 2);
  const img = x.getImageData(0, 0, W, H).data, pts = [];
  const step = isMobile ? 4 : 2;                       // kepadatan sampling
  for (let y = 0; y < H; y += step) for (let px = 0; px < W; px += step)
    if (img[(y * W + px) * 4 + 3] > 128) pts.push([(px - W / 2) / 90, -(y - H / 2) / 90]);
  return pts;
}

function init(renderer) {
  const CAP = isMobile ? 7000 : 20000;
  let pts = sampleText('TRUTH OF AUDITOR');
  if (pts.length > CAP) pts = pts.filter((_, i) => i % Math.ceil(pts.length / CAP) === 0);
  const N = pts.length;

  const scene = new THREE.Scene();
  const cam = new THREE.PerspectiveCamera(45, innerWidth / innerHeight, .1, 50);
  cam.position.z = 9;
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setSize(innerWidth, innerHeight);

  const pos = new Float32Array(N * 3), tgt = new Float32Array(N * 3), seed = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    pos[i * 3] = (Math.random() - .5) * 22; pos[i * 3 + 1] = (Math.random() - .5) * 14; pos[i * 3 + 2] = (Math.random() - .5) * 8;
    tgt[i * 3] = pts[i][0]; tgt[i * 3 + 1] = pts[i][1]; tgt[i * 3 + 2] = 0;
    seed[i] = Math.random();
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  geo.setAttribute('aTarget', new THREE.BufferAttribute(tgt, 3));
  geo.setAttribute('aSeed', new THREE.BufferAttribute(seed, 1));

  const uni = { uProgress: { value: 0 }, uPointer: { value: new THREE.Vector2(99, 99) }, uTime: { value: 0 } };
  const mat = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false,
    uniforms: uni,
    vertexShader: `
      attribute vec3 aTarget; attribute float aSeed;
      uniform float uProgress; uniform float uTime; uniform vec2 uPointer;
      varying float vSeed;
      void main(){
        vSeed = aSeed;
        float p = clamp(uProgress * (1.2 - aSeed * .4), 0., 1.);
        p = 1. - pow(1. - p, 3.);                       // ease-out cubic per partikel
        vec3 base = mix(position, aTarget, p);
        base.x += sin(uTime * .6 + aSeed * 6.28) * .015; // idle breathing
        base.y += cos(uTime * .5 + aSeed * 6.28) * .015;
        vec2 d = base.xy - uPointer;                     // cursor repulsion
        float r = length(d);
        if (r < 1.2) base.xy += normalize(d) * (1.2 - r) * .7;
        vec4 mv = modelViewMatrix * vec4(base, 1.);
        gl_PointSize = (2.4 - aSeed) * (3.2 / -mv.z) * ${isMobile ? '55.' : '80.'};
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: `
      varying float vSeed;
      void main(){
        float d = length(gl_PointCoord - .5); if (d > .5) discard;
        vec3 bone = vec3(.914, .894, .839);
        vec3 verdigris = vec3(.247, .642, .471);
        vec3 col = mix(bone, verdigris, step(.93, vSeed));  // ~7% partikel "matched"
        gl_FragColor = vec4(col, smoothstep(.5, .15, d) * .9);
      }`
  });
  scene.add(new THREE.Points(geo, mat));

  const ndc = new THREE.Vector2(99, 99);
  addEventListener('pointermove', (e) => {
    ndc.set((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
    // proyeksikan ke plane z=0 kamera sederhana:
    uni.uPointer.value.set(ndc.x * 6.2, ndc.y * 3.9);
  });
  addEventListener('resize', () => {
    cam.aspect = innerWidth / innerHeight; cam.updateProjectionMatrix();
    renderer.setSize(innerWidth, innerHeight);
  });

  // assemble saat load (GSAP UMD sudah ada di halaman)
  if (window.gsap) gsap.to(uni.uProgress, { value: 1, duration: 2.2, ease: 'power2.inOut', delay: .3 });
  else uni.uProgress.value = 1;

  // scatter + wipe saat submit login
  const form = document.querySelector('form');
  if (form) form.addEventListener('submit', () => {
    if (window.gsap) gsap.to(uni.uProgress, { value: 0, duration: .8, ease: 'power3.in' });
  });

  let raf = null;
  const clock = new THREE.Clock();
  (function loop() {
    uni.uTime.value = clock.getElapsedTime();
    renderer.render(scene, cam);
    raf = requestAnimationFrame(loop);
  })();
  addEventListener('pagehide', () => { cancelAnimationFrame(raf); geo.dispose(); mat.dispose(); renderer.dispose(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) cancelAnimationFrame(raf);
    else (function loop() { uni.uTime.value = clock.getElapsedTime(); renderer.render(scene, cam); raf = requestAnimationFrame(loop); })();
  });
}
```

CATATAN: font Zodiak harus loaded sebelum sampling — bungkus `init` dengan `document.fonts.load('700 150px Zodiak').then(...)`, fallback timeout 800ms.

- [ ] **Step 6: Run test → PASS; suite penuh → PASS**

- [ ] **Step 7: Commit**

```bash
git add web/templates/web/base.html web/templates/registration/login.html web/static/web/css/login.css web/static/web/js/login-hero.js web/tests_ui.py
git commit -m "feat(ui): login ink forensic + wordmark text-as-particles Three ESM"
```

---

### Task 9: QA sweep + ledger

**Files:**
- Modify: `.gitignore` (tambah `.context/`)
- Modify: `~/.claude/skills/build-premium-website-twist-threejs/.build-ledger.md` (append — di luar repo)

**Interfaces:** — (terminal)

- [ ] **Step 1: Suite penuh + collectstatic**

Run: `.venv/bin/python -m django test --settings=truth_auditor.settings` → OK (≥102 + test UI baru)
Run: `.venv/bin/python manage.py collectstatic --noinput` → manifest OK, font ter-hash

- [ ] **Step 2: Visual QA (runserver + browse/screenshot)**

`.venv/bin/python manage.py runserver` lalu periksa di 375/768/1440:
- Login: partikel assemble; matikan WebGL (Chrome flag / `about:config`) → fallback wordmark; `prefers-reduced-motion` emulation → statis
- Dashboard: folio, rail scroll-snap, count-up
- Transaksi/Run detail: sticky header, stamp chips, mono numerals; `Ctrl+P` print preview bersih
- Kontras AA: ink `#1C1A15` di atas bone `#EDE9DE` (rasio ~13:1 ✓); muted `#5C574B` di atas panel (cek ≥4.5:1)

- [ ] **Step 3: `.gitignore` + ledger**

```bash
echo ".context/" >> .gitignore
```

Append baris ke `.build-ledger.md` (kolom: date | business | webgl effect | direction | display font | layout twist | motion picks | anchor):

```
| 2026-07-03 | Truth of Auditor (Django reconciliation audit app — full app restyle) | Text-as-particles wordmark assemble/scatter (canvas-rasterized Zodiak → Points, cursor repulsion, submit scatter-out; ESM importmap r167, login-only) | Paper-light forensic ledger (bone paper #EDE9DE, ink, stamp chips; grain chrome-only; ink-dark login) | Zodiak (display) + Supreme (body) + IBM Plex Mono (numerals) | Dossier folio framing (ghost numeral 01–06 per page + document hairline frame + margin notes) | Preloader/processing overlay (recon run) · Horizontal batch rail (scroll-snap) · fade-up reveals | Lusion / Locomotive agency style |
```

- [ ] **Step 4: Commit final**

```bash
git add .gitignore
git commit -m "chore: gitignore .context + QA sweep Forensic Ledger"
```

---

## Self-review (dijalankan penulis plan)

- **Spec coverage:** §2 tokens→Task 2 · §3 arsitektur→Task 1–3 · §4 halaman (13 template)→Task 3–8 (app_base, dashboard, upload, transactions, reconcile, batch, run, _result_row, kelola×3, no_toko, base, login) · §5 motion/degradasi→Task 3/6/8 · §6 testing→tiap task + Task 9 · print→Task 2 (@media print) ✓
- **Placeholder scan:** tidak ada TBD/TODO; semua kode tertulis ✓
- **Type consistency:** `ToaMotion.init` (Task 3) tidak dipakai eksternal; `{% block folio %}` konsisten Task 3→4–7; nama file konsisten ✓
