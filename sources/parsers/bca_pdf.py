"""Parser PDF rekening BCA (text-based, tanpa OCR).

Tiap transaksi = 1 baris tanggal + nominal + CR/DB, kadang diikuti baris lanjutan
(nama/jenis: 'TRSF E-BANKING', 'BI-FAST', 'TRFDN-<nama>ESPAY...'). Tidak ada saldo per baris.
"""
import re
from decimal import Decimal

import pdfplumber

from .banks import extract_bca_name, is_bca_fee
from .base import BaseParser, parse_decimal, parse_dt, row_hash

DATE_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(.*)$")
AMT_RE = re.compile(r"([\d.,]+\.\d{2})\s+(CR|DB)\s*$")
SKIP = (
    "Bersambung", "TANGGAL KETERANGAN", "MUTASI REKENING", "NO. REKENING",
    "NAMA :", "HALAMAN", "JENIS TRANSAKSI", "PERIODE", "MATA UANG", "CATATAN",
    "Apabila nasabah", "dengan akhir", "tercantum pada", "SALDO AWAL",
    "SALDO AKHIR", "MUTASI CR", "MUTASI DB",
)


def _is_skip(s):
    return any(k in s for k in SKIP)


OWNER_RE = re.compile(r"^NAMA\s*:\s*(.+?)\s*$")

# Transfer antar-bank (bi-fast/online) di BCA = kanal SWITCHING, dipecah jadi
# baris 'TRF <nama> <kode> MYBCA' bernilai NET (bruto - fee) + baris fee
# 'BIAYA TXN ... MYBCA' Rp6.500. Panel mencatat BRUTO -> dua baris digabung.
# Nama bisa menempel ke kode 3-digit ('...ON535'), jadi non-greedy s/d kode.
SWITCHING_TRF_RE = re.compile(r"\bTRF\s+(.+?)\s*\d{3}\s+MYBCA\b")
SWITCHING_FEE = Decimal("6500")


def _merge_switching(rows):
    """Gabung pasangan SWITCHING BCA jadi satu WD bruto.

    Baris fee 'BIAYA TXN ... MYBCA' (admin, -6.500) selalu tepat SEBELUM baris
    'TRF <nama> <kode> MYBCA' (wd, NET). Gabungkan: money_delta = -(net+fee),
    fee dicatat, nama diekstrak dari baris TRF (extract_bca_name gagal di format
    ini karena nama ada SEBELUM nominal). Hasil cocok pass-1 ke nominal panel
    (bruto). Baris non-SWITCHING lolos apa adanya."""
    out, i, n = [], 0, len(rows)
    while i < n:
        r = rows[i]
        nxt = rows[i + 1] if i + 1 < n else None
        if (
            r["jenis"] == "admin"
            and "BIAYA TXN" in r["description"]
            and "MYBCA" in r["description"]
            and r["money_delta"] == -SWITCHING_FEE
            and nxt is not None
            and nxt["jenis"] == "wd"
        ):
            m = SWITCHING_TRF_RE.search(nxt["description"])
            if m:
                gross = nxt["amount"] + SWITCHING_FEE
                name = re.sub(r"\s+", " ", m.group(1)).strip(" -.,:/")
                out.append({
                    **nxt,
                    "amount": gross,
                    "money_delta": -gross,
                    "fee": SWITCHING_FEE,
                    "counterparty": name or nxt["counterparty"],
                })
                i += 2
                continue
        out.append(r)
        i += 1
    return out


def extract_pdf_owner(lines):
    """Pemilik rekening dari header statement ('NAMA : HENDI'). '' bila absen.
    Baris ini tetap di-SKIP dari transaksi — hanya dibaca sebagai metadata."""
    for ln in lines:
        m = OWNER_RE.match(ln.strip())
        if m:
            return m.group(1)
    return ""


def _clean_name(middle, cont):
    """Isolasi nama: gabung baris utama + baris lanjutan, lalu ekstrak lewat
    helper BCA bersama (buang teks struktural dulu, baru normalisasi di engine)."""
    return extract_bca_name(" ".join([middle, *cont]))


class BCAPDFParser(BaseParser):
    source_key = "bank"

    def parse(self, path, flow=""):
        lines = []
        with pdfplumber.open(path) as pdf:
            for pg in pdf.pages:
                lines += (pg.extract_text() or "").split("\n")

        owner = extract_pdf_owner(lines[:40])  # header selalu di awal dokumen
        if owner:
            self.meta["owner_name"] = owner

        txns, cur = [], None
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            m = DATE_RE.match(s)
            if m:
                cur = {"date": m.group(1), "rest": m.group(2), "cont": []}
                txns.append(cur)
            elif cur is not None and not _is_skip(s):
                cur["cont"].append(s)

        out = []
        occ_counter = {}  # (tanggal, nominal, CR/DB, desc[:40]) -> kemunculan sejauh ini
        for t in txns:
            am = AMT_RE.search(t["rest"])
            if not am:
                continue
            amount = parse_decimal(am.group(1))
            money = amount if am.group(2) == "CR" else -amount
            middle = t["rest"][: am.start()].strip()
            occurred = parse_dt(t["date"], dayfirst=True)
            desc = (middle + " " + " ".join(t["cont"])).strip()
            jenis = "admin" if is_bca_fee(desc) else ("depo" if money > 0 else "wd" if money < 0 else "lainnya")
            row = {
                "source_type": "bank",
                "occurred_at": occurred,
                "posted_date": occurred.date() if occurred else None,
                "jenis": jenis,
                "amount": amount,
                "credit_delta": Decimal("0"),
                "money_delta": money,
                "fee": Decimal("0"),
                "bonus": Decimal("0"),
                "balance_after": None,
                "ticket_no": "",
                "username": "",
                "reference": "",
                "counterparty": _clean_name(middle, t["cont"]),
                "description": desc,
                "raw": {"date": t["date"], "line": t["rest"], "cont": " ".join(t["cont"])},
            }
            # Kemunculan ke-N dari kunci stabil (tanggal, nominal, CR/DB, desc),
            # dihitung urut dalam FILE INI — bukan posisi baris global (idx),
            # yang bergeser antar export tumpang-tindih (1-15 vs 1-31) dan
            # membuat transaksi lama masuk dobel. Pola sama dgn bni_pdf.
            key = (t["date"], str(amount), am.group(2), desc[:40])
            occ = occ_counter.get(key, 0)
            occ_counter[key] = occ + 1
            row["row_hash"] = row_hash(
                "bca_pdf", [t["date"], amount, am.group(2), desc[:40], occ]
            )
            out.append(row)
        return _merge_switching(out)
