# Spec: UI Makeover "Forensic Ledger"

**Tanggal:** 2026-07-03
**Status:** Disetujui (desain di-approve user; menunggu review spec)
**Cakupan:** Restyle seluruh UI (13 template) — tanpa perubahan view/model/URL/fitur.
**Metode:** build-premium-website-twist-threejs (diadaptasi ke Django server-rendered MPA), roll RNG tercatat; 2nd opinion Codex CLI (session `019f2400-0033-7182-b650-a79a1df07957`).

## 1. Konsep & roll

Aplikasi diframe sebagai **dossier audit forensik**. Cerita brand: *transaksi tersebar ter-assemble menjadi kebenaran* — metafora rekonsiliasi.

Roll RNG (wajib dicatat di ledger skill + comment block JS):

| Axis | Hasil |
|---|---|
| WebGL effect | Text rendered as particles, assemble/scatter |
| Motion (2) | Page transition wipe/preloader · Horizontal scroll section |
| Visual | Grainy/noise texture overlay (re-roll dari "Editorial" — konflik ledger LumisLinks) |
| Anchor | Lusion / Locomotive |

Trend-confirmed: Codrops "Gommage" MSDF dissolve (Jan 2026), dissolve particles (Feb 2025).

Keputusan user:
- **Tema kerja: paper-light forensic** (sesuai keputusan spec 2026-07-02 "halaman kerja terang & padat"; anti-konvergen vs 5 build ledger terakhir yang semua dark). Login tetap ink-dark.
- **Scope: full app sekaligus** — 13 template, satu design system, satu PR.

## 2. Design tokens

- Surface kerja: bone paper `#EDE9DE`, panel `#F7F5EE`, ink text `#1C1A15`
- Hairline `rgba(28,26,21,.14)`; document-frame inset 12px (desktop)
- Semantik bucket (dipertahankan): cocok `#1F7A4D` · tinjau `#9A6B15` · tidak cocok `#B3362A` — **stamp chips**: uppercase mono, border 1px, warna semantik
- Login: ink-black `#12100C`, partikel bone `#EDE9DE` + aksen verdigris
- Grain: SVG feTurbulence 4–5% opacity — **hanya** login, chrome (sidebar saja — topbar translucent bone tanpa grain, accepted deviation), empty-state. **Tabel bebas grain.**
- Font (self-host via whitenoise, bukan CDN runtime):
  - **Zodiak** (Fontshare) — display: judul halaman besar + folio numeral. **Dilarang** di tabel/form/button.
  - **Supreme** (Fontshare) — body/UI.
  - **IBM Plex Mono** — semua angka (`font-variant-numeric: tabular-nums`), label margin, stamp chips.

## 3. Arsitektur front-end (no-build)

Keputusan: **tanpa toolchain build** (Vite/Tailwind ditolak — friction deploy Railway + kontributor; dikonfirmasi Codex). Pola existing dipertahankan, di-ekstrak:

```
web/static/web/css/app.css        ← design system dari inline <style> app_base.html
web/static/web/css/login.css      ← dari inline base.html/login.html
web/static/web/js/motion.js       ← GSAP header reveals + re-init hook htmx:afterSwap
web/static/web/js/login-hero.js   ← Three.js ESM (importmap, pinned r167): partikel teks
web/static/web/js/recon-overlay.js← overlay processing run batch (canvas 2D ringan, non-WebGL)
web/static/web/fonts/             ← woff2 Zodiak/Supreme/IBM Plex Mono
```

- CDN dipertahankan: GSAP UMD (pinned), htmx (pinned).
- **Three.js r128 UMD → ESM importmap pinned** (mis. r167) — hanya di login.
- **Lenis dibuang** dari halaman kerja (smooth-scroll mengganggu tabel panjang; login tidak scroll).
- `app_base.html` menyediakan aset per halaman lewat block existing `{% block head %}` dan `{% block scripts %}` (implementasi memakai block ini, bukan `{% block page_css %}`/`{% block page_js %}`).
- Font preload + `font-display: swap` (no-FOIT); jalur whitenoise manifest hashing diverifikasi.

## 4. Halaman (semua 13 template)

| Halaman | Perlakuan |
|---|---|
| Login (`registration/login.html`, `base.html`) | Ink + grain + **wordmark partikel** assemble/scatter; sukses login → scatter terimplementasi (wipe login→app TIDAK — View Transitions hanya antar halaman app) |
| Shell (`app_base.html`) | Sidebar ink di atas paper (dokumen-di-atas-meja); **folio numeral ghost** per halaman (01 DASBOR, 02 UNGGAH, …); margin-note rail metadata ≥1440px |
| Dashboard | KPI cards + **rail horizontal batch terakhir** (scroll-snap, bukan pinned scrub) |
| Upload | Drop-zone + tabel preview/riwayat: table chrome baru |
| Transaksi | Table chrome: sticky header, row hairlines, mono numerals, filter bar |
| Reconcile | Checklist kelengkapan + form; tombol run → overlay processing |
| Batch detail | Kartu DP/WD selisih + stamp Balanced ✓/⚠; daftar run |
| Run detail | Tabel hasil + stamp chips bucket + review HTMX (re-init hook) |
| Kelola user/toko/user_edit | Form + tabel gaya sama |
| `no_toko.html`, `_result_row.html` | Empty-state bergrain; partial row konsisten |

**Fungsi tidak berubah**: view, URL, model, HTMX flow, form field — semua utuh. Perubahan = template markup styling + CSS + JS presentasional.

Tambahan: `@media print` untuk transaksi & run detail (kebutuhan auditor; saran Codex).

## 5. Motion & degradasi

- Header reveal GSAP (opacity/translateY, ~400ms, power3) + re-init pasca `htmx:afterSwap`
- Run rekonsiliasi → overlay "menyusun partikel" (canvas 2D) sampai redirect batch detail
- View Transitions API antar halaman = progressive enhancement; tanpa dukungan → navigasi biasa
- `prefers-reduced-motion` → semua animasi mati, login tampil wordmark statis
- No-WebGL → fallback flat ink background + wordmark statis (bukan CSS gradient)
- Partikel: cap 20k desktop / 7k mobile; DPR clamp ≤2; init langsung (RAF ditunda saat tab hidden via visibilitychange guard, bukan lazy-init saat canvas terlihat); dispose saat unload; target 60fps

## 6. Testing & verifikasi

1. **102 test Django tetap pass.** Test yang assert markup (mis. `tests_kelola` cek sidebar render) diperiksa dan disesuaikan bila selector berubah.
2. Visual QA (gstack browse / screenshot): 375 / 768 / 1440 di semua halaman.
3. Matrix login: WebGL on/off, `prefers-reduced-motion`, mobile.
4. Kontras WCAG AA pada tabel paper-light (ink di atas bone).
5. `collectstatic` + whitenoise manifest sukses (font & static hash).
6. Ledger skill di-append + comment block roll di `login-hero.js`.

## 7. Risiko

- Font self-host vs whitenoise `CompressedManifestStaticFilesStorage`: path font di CSS harus lewat `static()`/relative agar ter-hash. Verifikasi di step collectstatic.
- htmx swap menimpa DOM yang sudah di-animate → re-init hook wajib, idempoten.
- Test suite meng-assert string HTML lama → jalankan penuh tiap fase.
- CSP belum diset di app — CDN GSAP/htmx tetap dipakai (status quo); keputusan CSP di luar scope spec ini.

## 8. Di luar lingkup (non-goals)

- Fitur baru, perubahan view/model/URL/alur HTMX.
- Vite/Tailwind/bundler.
- Dark theme halaman kerja / dual-theme toggle.
- CSP hardening, rate limiting (masuk backlog hardening terpisah).
