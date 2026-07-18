"""Nomor batch per-toko POSISIONAL (count id <= pk utk toko itu) — satu sumber.

Rumus lama `ReconBatch.objects.filter(toko=..., id__lte=pk).count()` terduplikasi
di ±7 titik dan dipanggil DALAM loop (kalender dashboard 14×, review_queue
40×/halaman, export_center ≤200×) → satu query COUNT per baris. Modul ini:

- `batch_no(batch)`   — pemakaian tunggal (satu COUNT, semantik identik).
- `batch_no_map(...)` — pemakaian massal: SATU query id per toko + bisect.
"""
from bisect import bisect_right

from reconciliation.models import ReconBatch


def batch_no(batch):
    """Nomor posisional satu batch (None → None). Satu query COUNT."""
    if batch is None:
        return None
    return ReconBatch.objects.filter(toko_id=batch.toko_id, id__lte=batch.id).count()


def batch_no_map(toko, ids):
    """{batch_id: nomor posisional} utk id batch milik `toko` (obj/pk).

    SATU query (semua id batch toko, terurut) + bisect: posisi id di daftar
    terurut ≡ count(id__lte) karena id unik menaik. Id yang bukan milik toko
    tidak dijamin benar — tanggung jawab pemanggil (pola lama juga begitu).
    """
    ids = {i for i in ids if i}
    if not ids:
        return {}
    all_ids = list(
        ReconBatch.objects.filter(toko=toko).order_by("id").values_list("id", flat=True)
    )
    return {i: bisect_right(all_ids, i) for i in ids}


def batch_no_map_multi(pairs):
    """{batch_id: nomor} utk iterable (batch_id, toko_id) LINTAS toko —
    satu query per toko yang terlibat (export_center 'semua toko')."""
    per_toko = {}
    for b_id, t_id in pairs:
        per_toko.setdefault(t_id, []).append(b_id)
    out = {}
    for t_id, ids in per_toko.items():
        out.update(batch_no_map(t_id, ids))
    return out
