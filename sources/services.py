"""Service ingest: parse file -> simpan Transaction kanonik (idempoten via row_hash)."""
import os
import tempfile
from pathlib import Path

from django.db import IntegrityError, transaction as db_tx

from transactions.models import Transaction, owner_from_filename

from .models import SourceType, Upload
from .parsers.banks import BCACSVParser, BRIParser, MandiriParser
from .parsers.bca_pdf import BCAPDFParser
from .parsers.bni_pdf import BNIPDFParser
from .parsers.bracket import BracketParser
from .parsers.cor import (
    CORPanelBankParser,
    CORPanelQRISParser,
    CORQRISGatewayParser,
    CORQRISWDGatewayParser,
)
from .parsers.gateways import (
    NXPayParser,
    QRFlyerParser,
    QHokiParser,
    RPayGatewayParser,
    RPayWDGatewayParser,
    RPayDPXlsxParser,
    RPayWDXlsxParser,
)
from .parsers.panel import PanelParser

# parser_key -> kelas parser (parser.source_key menentukan SourceType-nya)
PARSERS = {
    "bracket": BracketParser,
    "panel": PanelParser,
    "bri": BRIParser,
    "bca_csv": BCACSVParser,
    "bca_pdf": BCAPDFParser,
    "bni_pdf": BNIPDFParser,
    "mandiri": MandiriParser,
    "nxpay": NXPayParser,
    "qrflyer": QRFlyerParser,
    "cor_panel_bank": CORPanelBankParser,
    "cor_panel_qris": CORPanelQRISParser,
    "cor_qris_gateway": CORQRISGatewayParser,
    "cor_qris_wd_gateway": CORQRISWDGatewayParser,
    "rpay": RPayGatewayParser,
    "rpay_wd": RPayWDGatewayParser,
    "rpay_xlsx": RPayDPXlsxParser,
    "rpay_wd_xlsx": RPayWDXlsxParser,
    "qhoki": QHokiParser,
}

# Magic bytes: OLE2/CDFV2 compound-file header (e-statement terenkripsi "agile encryption").
_OLE2_MAGIC = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"


def _ole2_kind(path):
    """Klasifikasi file ber-magic OLE2 (xlsx normal diawali ``PK\\x03\\x04``).

    -> 'encrypted' (ada stream EncryptionInfo/EncryptedPackage),
       'biff'      (stream Workbook/Book tanpa enkripsi = .xls lawas),
       'unknown'   (OLE2 tapi olefile gagal membacanya — konservatif),
       None        (bukan OLE2).
    """
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None
    if head != _OLE2_MAGIC:
        return None
    try:
        import olefile

        with olefile.OleFileIO(path) as ole:
            if ole.exists("EncryptionInfo") or ole.exists("EncryptedPackage"):
                return "encrypted"
            if ole.exists("Workbook") or ole.exists("Book"):
                return "biff"
    except Exception:
        pass
    return "unknown"


def is_encrypted_xlsx(path):
    """True bila `path` adalah xlsx terenkripsi. .xls BIFF lawas juga ber-magic
    OLE2 tapi BUKAN terenkripsi — dulu diminta password lalu buntu \"Password
    salah\". OLE2 yang tak terbaca olefile tetap dianggap terenkripsi
    (konservatif, perilaku lama)."""
    return _ole2_kind(path) in ("encrypted", "unknown")


def _decrypt_to_temp(path, password):
    """Dekripsi xlsx terenkripsi ke file .xlsx sementara; kembalikan path-nya.

    Gagal (password salah / file rusak) → temp dibersihkan + ValueError berpesan
    jelas (pesan ini tampil apa adanya di form upload)."""
    import msoffcrypto

    import contextlib

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)

    def _cleanup():
        tmp.close()
        with contextlib.suppress(OSError):  # gagal hapus jangan menutupi error asli
            os.remove(tmp.name)

    try:
        with open(path, "rb") as f:
            off = msoffcrypto.OfficeFile(f)
            off.load_key(password=password)
            off.decrypt(tmp)
    except OSError:
        _cleanup()
        raise  # file sumber hilang/tak terbaca — BUKAN soal password
    except Exception as e:
        _cleanup()
        raise ValueError(
            "Password salah atau file terenkripsi rusak — periksa kembali."
        ) from e
    tmp.close()
    return tmp.name


def ingest(parser_key, file_path, recon_date=None, account=None, flow="", user=None, toko=None, provider="", password=""):
    """Parse `file_path` dengan parser `parser_key`, simpan sebagai Transaction.

    File terenkripsi (Mandiri e-statement) didekripsi dulu memakai `password`.
    Mengembalikan (upload, created, duplicate).
    """
    if parser_key not in PARSERS:
        raise ValueError(f"Parser '{parser_key}' tidak dikenal. Pilihan: {', '.join(PARSERS)}")

    parser = PARSERS[parser_key]()

    parse_path, tmp_path = file_path, None
    if _ole2_kind(file_path) == "biff":
        raise ValueError(
            "File .xls format lama tidak didukung — buka di Excel lalu Save As .xlsx"
        )
    if is_encrypted_xlsx(file_path):
        if not password:
            raise ValueError("File terenkripsi — butuh password untuk membukanya.")
        tmp_path = _decrypt_to_temp(file_path, password)
        parse_path = tmp_path
    try:
        rows = parser.parse(parse_path, flow=flow)
        st = SourceType.objects.get(key=parser.source_key)
        # Pemilik rekening: header file (BCA/Mandiri) dulu, fallback nama file (BRI).
        # getattr: parser double di test boleh tanpa .meta.
        meta = getattr(parser, "meta", {}) or {}
        owner = meta.get("owner_name", "") or owner_from_filename(Path(file_path).name)
        try:
            return _persist_rows(rows, st, file_path, recon_date, account, flow, user, toko, provider, owner)
        except IntegrityError:
            # Balapan ingest ganda (double-submit / dua worker): constraint DB
            # menolak baris kembar. Ulang SEKALI — percobaan kedua membaca ulang
            # row_hash yang baru saja di-commit proses lain → terhitung duplikat.
            return _persist_rows(rows, st, file_path, recon_date, account, flow, user, toko, provider, owner)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def _persist_rows(rows, st, file_path, recon_date, account, flow, user, toko, provider, owner=""):
    """Simpan hasil parse sebagai Upload + Transaction (atomic, dedup row_hash)."""
    with db_tx.atomic():
            up = Upload.objects.create(
                source_type=st,
                account=account,
                toko=toko,
                provider=provider,
                flow=flow or "",
                recon_date=recon_date,
                original_name=Path(file_path).name,
                owner_name=(owner or "")[:100],
                status=Upload.PARSED,
                uploaded_by=user,
            )
            # Dedup: cek HANYA hash yang ada di file ini (chunk 900 — batas
            # variabel sqlite), bukan memuat SEMUA hash (source_type, toko)
            # historis (ratusan ribu baris per ingest di prod).
            existing = set()
            hashes = list({row["row_hash"] for row in rows})
            for i in range(0, len(hashes), 900):
                existing.update(
                    Transaction.objects.filter(
                        source_type=st, toko=toko, row_hash__in=hashes[i:i + 900]
                    ).values_list("row_hash", flat=True)
                )
            objs, seen, dup, dup_tercatat = [], set(), 0, set()
            for row in rows:
                rh = row["row_hash"]
                if rh in existing or rh in seen:
                    dup += 1
                    # Baris yang sudah tercatat lewat UPLOAD TERDAHULU di-link ke
                    # upload ini agar "isi file" tetap bisa ditampilkan utuh
                    # (repeat DALAM file yang sama bukan isi file terdahulu).
                    if rh in existing:
                        dup_tercatat.add(rh)
                    continue
                seen.add(rh)
                objs.append(
                    Transaction(
                        upload=up,
                        source_type=st,
                        account=account,
                        toko=toko,
                        occurred_at=row["occurred_at"],
                        posted_date=row["posted_date"],
                        jenis=row["jenis"],
                        amount=row["amount"],
                        credit_delta=row["credit_delta"],
                        money_delta=row["money_delta"],
                        fee=row["fee"],
                        bonus=row["bonus"],
                        balance_after=row["balance_after"],
                        ticket_no=row["ticket_no"],
                        username=row["username"],
                        reference=row["reference"],
                        counterparty=row["counterparty"],
                        description=row["description"],
                        player_bank=row.get("player_bank", ""),
                        bank_title=row.get("bank_title", ""),
                        raw=row["raw"],
                        row_hash=rh,
                    )
                )
            Transaction.objects.bulk_create(objs, batch_size=1000)
            if dup_tercatat:
                up.duplicate_transactions.add(
                    *Transaction.objects.filter(
                        source_type=st, toko=toko, row_hash__in=dup_tercatat
                    ).values_list("id", flat=True)
                )
            up.rows_parsed = len(objs)
            up.rows_duplicate = dup
            up.save(update_fields=["rows_parsed", "rows_duplicate"])
    return up, len(objs), dup
