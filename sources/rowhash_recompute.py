"""Recompute row_hash baris LAMA ke formula parser BARU (dipakai migrasi data).

Tiga formula berubah tanpa migrasi (W6-3) — re-upload file periode lama akan
menghasilkan duplikat massal karena hash baris lama tak cocok lagi:

- bracket : baris manual (tanpa Transaction ID + ticket) kini ikut
            Tanggal+Jam+Description; extract_ticket berubah (TICKET_RE 6→11
            digit) sehingga baris ber-description ticket panjang ikut bergeser.
- bca_pdf : komponen terakhir hash idx global file → occurrence-counter per
            kunci stabil (tanggal, nominal, CR/DB, desc[:40]).
- rpay DP : buang komponen nominal — hash = UUID saja.

Prinsip: recompute MEMAKAI fungsi parser aktual (extract_ticket, parse_decimal,
row_hash, AMT_RE) dari kolom `raw` yang tersimpan — bukan menyalin formula —
supaya tidak drift dari parser.

Batasan yang disadari (didokumentasikan, bukan bug):
- raw menyimpan sel kosong xlsx sebagai "" padahal openpyxl membacanya None
  (str(None)="None" ikut di-hash parser). Recompute mengembalikan "" → None
  untuk sel kosong — benar utk jalur openpyxl (dominan); file yang dulu lewat
  reader mentah fallback (styles rusak) bisa meleset utk sel kosong.
- bca_pdf: baris fee SWITCHING yang di-merge parser tak tersimpan sebagai baris
  sendiri, jadi occurrence baris fee kembar yang selamat bisa bergeser; dan
  baris yang di-skip dedup saat ingest tak ikut dihitung occurrence upload ini.
  Kasus sempit. CATATAN JUJUR: guard collision hanya menahan pergeseran yang
  MENABRAK hash hidup; pergeseran ke hash kosong tetap ditulis dan bisa berbeda
  dari occurrence yang akan dihitung parser pada re-upload file yang sama —
  baris kembar-SWITCHING itu bisa terduplikat sekali. Risiko residual diterima
  (jauh lebih kecil dari bug idx-global yang digantikan).
"""
from collections import Counter, defaultdict

from .parsers.base import extract_ticket, parse_decimal, row_hash
from .parsers.bca_pdf import AMT_RE

BCA_PDF_RAW_KEYS = {"date", "line", "cont"}


def _cell(raw, key):
    """Nilai sel utk bagian hash, meniru `r.get(key, "")` parser atas baris
    openpyxl: key hilang → "" (default get), sel kosong (raw "") → None
    (openpyxl membaca sel kosong sebagai None; str(None)="None" ikut di-hash),
    selain itu → string tersimpan (str(v) parser == str tersimpan)."""
    if key not in raw:
        return ""
    v = raw[key]
    return None if v == "" else v


def recompute_bracket_hash(raw):
    """Hash formula BARU utk baris bracket, dari kolom raw tersimpan."""
    raw = raw or {}
    desc = str(raw.get("Description", "") or "")
    ticket = extract_ticket(desc)  # TICKET_RE BARU (6-11 digit)
    username = str(raw.get("Username", "") or "").strip()
    amount = abs(parse_decimal(raw.get("Total")))
    tid = _cell(raw, "Transaction ID")
    parts = [tid, ticket, username, amount]
    if not str(tid or "").strip() and not ticket:
        parts += [_cell(raw, "Tanggal"), _cell(raw, "Jam"), desc]
    return row_hash("bracket", parts)


def is_bca_pdf_raw(raw):
    """Baris bank hasil parser bca_pdf? raw-nya persis {date, line, cont} —
    parser bank lain (BRI/BCA-CSV/Mandiri/BNI) menyimpan struktur berbeda."""
    return isinstance(raw, dict) and set(raw.keys()) == BCA_PDF_RAW_KEYS


def recompute_bca_pdf_hashes(rows):
    """rows = iterable (pk, raw) URUT id menaik SATU upload (id menaik = urutan
    file asli; bulk_create mempertahankan urutan parse). Return {pk: hash baru}.
    Baris tanpa nominal di line (harusnya tak pernah tersimpan) dilewati."""
    occ_counter = {}
    out = {}
    for pk, raw in rows:
        raw = raw or {}
        line = raw.get("line", "") or ""
        am = AMT_RE.search(line)
        if not am:
            continue
        amount = parse_decimal(am.group(1))
        middle = line[: am.start()].strip()
        cont = raw.get("cont", "") or ""
        desc = (middle + " " + cont).strip()
        date = raw.get("date", "")
        key = (date, str(amount), am.group(2), desc[:40])
        occ = occ_counter.get(key, 0)
        occ_counter[key] = occ + 1
        out[pk] = row_hash("bca_pdf", [date, amount, am.group(2), desc[:40], occ])
    return out


def is_rpay_dp_raw(raw):
    """Baris gateway hasil parser rpay (CSV DP RafflesPay)? Satu-satunya parser
    gateway ber-raw `UUID` + `Customer Username` (rpay_wd: UUID + External ID,
    rpay_xlsx: Ticket Number + RRN tanpa UUID)."""
    return (
        isinstance(raw, dict) and "UUID" in raw and "Customer Username" in raw
        and "External ID" not in raw
    )


def recompute_rpay_dp_hash(raw):
    uuid = str((raw or {}).get("UUID", "") or "").strip()
    return row_hash("rpay", [uuid]) if uuid else None


def recompute_all(Transaction):
    """Jalankan recompute utk tiga parser di atas seluruh tabel.

    `Transaction` boleh model historis (apps.get_model dari migrasi) maupun
    model live (unit test). Guard collision: hash baru yang sudah dipakai baris
    LAIN pada (source_type, toko) yang sama TIDAK diterapkan (baris dibiarkan
    ber-hash lama; biasanya justru duplikat sejati yang dulu lolos dedup —
    jangan hapus data, cukup catat). Return dict statistik.
    """
    new_hashes = {}   # pk -> hash baru
    meta = {}         # pk -> (source_type_id, toko_id)

    # --- bracket ---
    for pk, st_id, toko_id, raw in Transaction.objects.filter(
        source_type__key="bracket"
    ).values_list("pk", "source_type_id", "toko_id", "raw").iterator(chunk_size=2000):
        h = recompute_bracket_hash(raw)
        new_hashes[pk] = h
        meta[pk] = (st_id, toko_id)

    # --- bca_pdf (per upload, urut id = urutan file) ---
    per_upload = defaultdict(list)
    for pk, st_id, toko_id, up_id, raw in Transaction.objects.filter(
        source_type__key="bank"
    ).values_list("pk", "source_type_id", "toko_id", "upload_id", "raw")\
     .order_by("pk").iterator(chunk_size=2000):
        if is_bca_pdf_raw(raw):
            per_upload[up_id].append((pk, raw))
            meta[pk] = (st_id, toko_id)
    for rows in per_upload.values():
        new_hashes.update(recompute_bca_pdf_hashes(rows))

    # --- rpay DP ---
    for pk, st_id, toko_id, raw in Transaction.objects.filter(
        source_type__key="gateway"
    ).values_list("pk", "source_type_id", "toko_id", "raw").iterator(chunk_size=2000):
        if is_rpay_dp_raw(raw):
            h = recompute_rpay_dp_hash(raw)
            if h:
                new_hashes[pk] = h
                meta[pk] = (st_id, toko_id)

    # --- terapkan dgn guard collision per (source_type, toko) ---
    by_group = defaultdict(dict)
    for pk, h in new_hashes.items():
        if pk in meta:
            by_group[meta[pk]][pk] = h

    stats = {"unchanged": 0, "updated": 0, "collision": 0}
    for (st_id, toko_id), items in by_group.items():
        rows = Transaction.objects.filter(
            source_type_id=st_id, toko_id=toko_id
        ).values_list("pk", "row_hash")
        live = Counter()
        old_of = {}
        for pk, h in rows:
            live[h] += 1
            if pk in items:
                old_of[pk] = h
        to_update = []
        for pk, new in sorted(items.items()):
            old = old_of.get(pk)
            if old is None:
                continue  # defensif: baris hilang di tengah jalan
            if new == old:
                stats["unchanged"] += 1
                continue
            if live.get(new, 0) > 0:
                # Hash baru sudah dipakai baris lain (duplikat sejati yang dulu
                # lolos, atau tabrakan rekonstruksi) — biarkan hash lama.
                stats["collision"] += 1
                continue
            to_update.append((pk, new))
            live[old] -= 1
            if live[old] <= 0:
                del live[old]
            live[new] = 1
        for i in range(0, len(to_update), 1000):
            chunk = to_update[i:i + 1000]
            objs = {
                t.pk: t
                for t in Transaction.objects.filter(pk__in=[pk for pk, _ in chunk])
            }
            # W7-2: remap bisa BERANTAI (B mengklaim hash yang baru
            # ditinggalkan A pada run yang sama; terjadi saat idx global
            # memampat ke occurrence). Postgres mengecek unique constraint
            # per-baris (non-deferrable) dan urutan tulis dalam SATU statement
            # tak dijamin → klaim B bisa dicek selagi A belum pindah = unique
            # violation. DUA FASE: parkir semua baris chunk ke hash sementara
            # yang mustahil tabrakan (bukan hexdigest — mengandung ':', unik
            # per pk, muat max_length=64), lalu tulis hash final. Rantai
            # LINTAS chunk aman tanpa fase global: guard live di atas hanya
            # meloloskan klaim setelah pemilik lamanya diproses lebih dulu
            # (urut pk), jadi pelepas selalu berada di chunk yang sama/lebih
            # awal dari pengklaim.
            for pk, _new in chunk:
                objs[pk].row_hash = f"mig0010:{pk}"
            Transaction.objects.bulk_update(list(objs.values()), ["row_hash"])
            for pk, new in chunk:
                objs[pk].row_hash = new
            Transaction.objects.bulk_update(list(objs.values()), ["row_hash"])
        stats["updated"] += len(to_update)
    return stats
