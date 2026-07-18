"""W6-3: recompute row_hash baris lama ke formula parser BARU.

Tiga formula hash berubah tanpa migrasi data — re-upload file periode lama akan
lolos dedup dan menghasilkan duplikat massal (CLAUDE.md menjanjikan re-import
aman): bracket (cabang manual + TICKET_RE 6→11 digit), bca_pdf (idx global →
occurrence-counter), rpay DP (buang nominal). Logika di
`sources/rowhash_recompute.py` — memakai fungsi parser aktual supaya tidak
drift dari formula.

Guard collision: hash baru yang sudah dipakai baris lain pada (source_type,
toko) yang sama dilewati (baris tetap ber-hash lama; biasanya justru duplikat
sejati yang dulu lolos) — tidak ada data yang dihapus.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    from sources.rowhash_recompute import recompute_all

    Transaction = apps.get_model("transactions", "Transaction")
    stats = recompute_all(Transaction)
    if stats["updated"] or stats["collision"]:
        print(f"\n  recompute row_hash: {stats}")


def backwards(apps, schema_editor):
    """Sengaja NO-OP: hash lama tak bisa dipulihkan deterministik (idx global
    file & varian format nominal tak tersimpan), dan hash baru tetap kunci
    dedup yang valid — rollback kode tak butuh rollback data."""


class Migration(migrations.Migration):

    dependencies = [
        ("sources", "0009_upload_duplicate_transactions"),
        ("transactions", "0007_transaction_uniq_tx_source_rowhash_toko_null"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
