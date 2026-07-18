"""Parser untuk file FR BRACKET (xlsx). Header di baris 1.

Kolom: Tanggal, Jam, Asset Bank, ID, Description, Member, Username, Product,
Expense, No. Rek Bank Member, Bank, Total, Saldo Akhir, Credit Awal, Credit Akhir,
Kategori, Status, OP, Transaction ID, Transaction Date, Status Backdated.
"""
from decimal import Decimal

from .base import (
    BaseParser,
    derive_bank_fields,
    extract_ref,
    extract_ticket,
    parse_decimal,
    parse_dt,
    read_xlsx_rows,
    row_hash,
)

KATEGORI_MAP = {
    "deposit": "depo",
    "withdraw": "wd",
    "withdrawal": "wd",
    "bonus": "bonus",
    "beban admin bank": "admin",
    "beban admin qris": "admin",
    "beban other expense": "admin",
    "biaya transaksi": "admin",
    # 'sesama cm', 'adjustment', 'pending dp' -> 'lainnya' (belum dipetakan khusus)
}


class BracketParser(BaseParser):
    source_key = "bracket"

    def parse(self, path, flow=""):
        _, rows = read_xlsx_rows(path, header_row=1)
        out = []
        for r in rows:
            kategori = str(r.get("Kategori", "") or "").strip().lower()
            jenis = KATEGORI_MAP.get(kategori, "lainnya")

            # Total di file SUDAH bertanda (deposit +, WD/admin/keluar -).
            total = parse_decimal(r.get("Total"))
            amount = abs(total)
            money_delta = total
            credit_delta = parse_decimal(r.get("Credit Akhir")) - parse_decimal(
                r.get("Credit Awal")
            )

            desc = str(r.get("Description", "") or "")
            occurred = parse_dt(r.get("Transaction Date"))
            posted = parse_dt(r.get("Tanggal"), dayfirst=True)
            saldo = r.get("Saldo Akhir")

            raw = {k: ("" if v is None else str(v)) for k, v in r.items()}
            player_bank, bank_title = derive_bank_fields("bracket", raw)
            row = {
                "source_type": "bracket",
                "occurred_at": occurred,
                "posted_date": posted.date() if posted else None,
                "jenis": jenis,
                "amount": amount,
                "credit_delta": credit_delta,
                "money_delta": money_delta,
                "fee": parse_decimal(r.get("Expense")),
                "bonus": Decimal("0"),
                "balance_after": parse_decimal(saldo) if saldo not in (None, "") else None,
                "ticket_no": extract_ticket(desc),
                "username": str(r.get("Username", "") or "").strip(),
                "reference": extract_ref(desc),
                "counterparty": str(r.get("Member", "") or "").strip(),
                "description": desc,
                "player_bank": player_bank,
                "bank_title": bank_title,
                "raw": raw,
            }
            # Baris manual ledger (Beban Admin QRIS/Hutang/Piutang, dst): tanpa
            # Transaction ID DAN tanpa ticket, nominal sama lintas riwayat =
            # hash identik -> baris kedua dibuang senyap saat ingest. Tambahkan
            # Tanggal+Jam+Description sebagai pembeda. Baris ber-ID/ticket TETAP
            # formula lama agar idempotensi data lama terjaga.
            parts = [r.get("Transaction ID", ""), row["ticket_no"], row["username"], row["amount"]]
            if not str(r.get("Transaction ID", "") or "").strip() and not row["ticket_no"]:
                parts += [r.get("Tanggal", ""), r.get("Jam", ""), desc]
            row["row_hash"] = row_hash("bracket", parts)
            out.append(row)
        return out
