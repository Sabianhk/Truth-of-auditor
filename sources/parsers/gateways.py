"""Parser gateway pembayaran (sumber UANG, setara bank): NXPAY, QR FLYER, QHOKI, RPAY."""
import csv
from decimal import Decimal

from .base import BaseParser, parse_decimal, parse_dt, read_xlsx_grid, read_xlsx_rows, row_hash


def _require_flow(flow):
    """Parser dua-arah WAJIB tahu arah file. Dulu flow selain "wd" diam-diam
    dianggap DP — file WD tanpa token "wd" di nama = seluruh file kebalik
    tanda. Fail-loud: pesan ini tampil sebagai error file saat commit upload."""
    if flow not in ("dp", "wd"):
        raise ValueError("flow tidak diketahui — pilih DP/WD saat upload")
    return flow


def _money(amount, flow):
    """Tanda money_delta berdasarkan flow file (dp = masuk +, wd = keluar -)."""
    return -amount if flow == "wd" else amount


class NXPayParser(BaseParser):
    source_key = "gateway"

    def parse(self, path, flow=""):
        _require_flow(flow)
        _, rows = read_xlsx_rows(path, header_row=2)  # baris 1 = judul report
        out = []
        for r in rows:
            ticket = str(r.get("Ticket Number", "") or "").strip()
            # Footer "Total"/"Grand Total" dideteksi equality/prefix — substring
            # pernah ikut membuang player asli bernama "totalwin88".
            uname = str(r.get("Username", "") or "").strip().lower()
            if not ticket or uname == "total" or uname.startswith(("total ", "grand total")):
                continue  # skip footer / Grand Total
            amt = abs(parse_decimal(r.get("Amount")))
            occurred = parse_dt(r.get("Date"))  # format US: M/D/YYYY h:m:s AM/PM
            row = {
                "source_type": "gateway",
                "occurred_at": occurred,
                "posted_date": occurred.date() if occurred else None,
                "jenis": "wd" if flow == "wd" else "depo",
                "amount": amt,
                "credit_delta": Decimal("0"),
                "money_delta": _money(amt, flow),
                "fee": parse_decimal(r.get("Admin Fee")),
                "bonus": Decimal("0"),
                "balance_after": None,
                "ticket_no": ticket,
                "username": str(r.get("Username", "") or "").strip(),
                "reference": "",
                "counterparty": str(r.get("Account Title", "") or "").strip(),
                "description": f"NXPAY {r.get('Payment Type','')} {r.get('Status','')}".strip(),
                "raw": {k: ("" if v is None else str(v)) for k, v in r.items()},
            }
            row["row_hash"] = row_hash("nxpay", [ticket, row["username"], amt, occurred])
            out.append(row)
        return out


class QRFlyerParser(BaseParser):
    source_key = "gateway"

    def parse(self, path, flow=""):
        _require_flow(flow)
        _, rows = read_xlsx_rows(path, header_row=1)
        out = []
        for r in rows:
            amt = abs(parse_decimal(r.get("Transaction Value")))
            occurred = parse_dt(r.get("Transaction Date"))
            settle = parse_dt(r.get("Settlement Time"))
            ticket = str(r.get("TXN ID", "") or "").strip()
            ref = str(r.get("Client Reference", "") or "").strip()
            if not ticket and not ref:  # skip footer/total
                continue
            row = {
                "source_type": "gateway",
                "occurred_at": occurred,
                "posted_date": (settle or occurred).date() if (settle or occurred) else None,
                "jenis": "wd" if flow == "wd" else "depo",
                "amount": amt,
                "credit_delta": Decimal("0"),
                "money_delta": _money(amt, flow),
                "fee": Decimal("0"),
                "bonus": Decimal("0"),
                "balance_after": None,
                "ticket_no": ticket,
                "username": str(r.get("Customer ID / User Account", "") or "").strip(),
                "reference": ref,
                "counterparty": "",
                "description": f"QRFLYER {r.get('Payment Status','')}".strip(),
                "raw": {k: ("" if v is None else str(v)) for k, v in r.items()},
            }
            row["row_hash"] = row_hash("qrflyer", [ticket, ref, amt])
            out.append(row)
        return out


class QHokiParser(BaseParser):
    """QRIS HOKI (gateway brand panel-Nexus: MUL/WLG/LBS). Whitelabel Transaction
    ID = Ticket Panel (D...), Transaction ID = UUID (juga muncul di Remarks panel).
    Sebagian brand mengekspornya sebagai CSV quoted (kolom identik xlsx)."""

    source_key = "gateway"

    def parse(self, path, flow=""):
        _require_flow(flow)
        if str(path).lower().endswith(".csv"):
            with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
                rows = list(csv.DictReader(f))
        else:
            _, rows = read_xlsx_rows(path, header_row=1)
        out = []
        for r in rows:
            if str(r.get("Status", "") or "").strip().lower() != "success":
                continue
            wl = str(r.get("Whitelabel Transaction ID", "") or "").strip()
            txid = str(r.get("Transaction ID", "") or "").strip()
            if not wl and not txid:
                # Tanpa identitas apa pun row_hash cuma bergantung nominal ->
                # baris senominal saling tabrak & terbuang diam-diam. Skip.
                continue
            amt = abs(parse_decimal(r.get("Amount")))
            occurred = parse_dt(r.get("Transaction Date"))
            row = {
                "source_type": "gateway",
                "occurred_at": occurred,
                "posted_date": occurred.date() if occurred else None,
                "jenis": "wd" if flow == "wd" else "depo",
                "amount": amt,
                "credit_delta": Decimal("0"),
                "money_delta": _money(amt, flow),
                "fee": parse_decimal(r.get("Downline Fee Amount")),
                "bonus": Decimal("0"),
                "balance_after": None,
                "ticket_no": wl,
                "username": str(r.get("Member ID", "") or "").strip(),
                "reference": txid,
                "counterparty": "",
                "description": f"QHOKI {r.get('Rrn','')}".strip(),
                "raw": {k: ("" if v is None else str(v)) for k, v in r.items()},
            }
            row["row_hash"] = row_hash("qhoki", [txid, wl, amt])
            out.append(row)
        return out


class RPayGatewayParser(BaseParser):
    """Gateway QRIS RPay (CSV, dipakai brand panel-Nexus, mis. MUL/M77).

    Membawa `Customer Username` == username panel -> anchor pass-1 username
    exact. `UUID` DISIMPAN di raw saja, TIDAK di `reference`: aturan blocked
    engine mengasingkan gateway ber-reference yang tak dikenal panel dari pass
    identitas, dan panel Nexus TERBUKTI tidak menanam UUID RPay di Remarks
    (verifikasi panel M77 09-Jul-2026: 0 dari 2.058 baris QRISRPAY).
    """

    source_key = "gateway"

    def parse(self, path, flow=""):
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            raw_rows = list(csv.DictReader(f))
        is_wd = flow == "wd"
        out = []
        for r in raw_rows:
            uuid = str(r.get("UUID", "") or "").strip()
            status = str(r.get("Status", "") or "").strip().lower()
            if not uuid or status != "success":
                continue
            # abs: tanda ditentukan flow (konsisten parser gateway lain);
            # dayfirst: vendor Indonesia, 09/07 = 9 Juli (format bernama-bulan
            # "09 Jul 2026" tak terpengaruh).
            amt = abs(parse_decimal(r.get("Amount")))
            occurred = parse_dt(r.get("Date"), dayfirst=True)
            username = str(r.get("Customer Username", "") or "").strip()
            cname = str(r.get("Customer Name", "") or "").strip()
            row = {
                "source_type": "gateway",
                "occurred_at": occurred,
                "posted_date": occurred.date() if occurred else None,
                "jenis": "wd" if is_wd else "depo",
                "amount": amt,
                "credit_delta": Decimal("0"),
                "money_delta": -amt if is_wd else amt,
                "fee": parse_decimal(r.get("Fee")),
                "bonus": Decimal("0"),
                "balance_after": None,
                "ticket_no": "",
                "username": username,
                "reference": "",
                "counterparty": "" if cname.lower() == username.lower() else cname,
                "description": f"RPay {r.get('RRN', '')}".strip(),
                "raw": {k: ("" if v is None else str(v)) for k, v in r.items() if k},
            }
            # UUID unik per transaksi — TANPA nominal (pola sama rpay_wd)
            # supaya idempotensi tak goyah oleh variasi format angka
            # ("25000" vs "25000.0" vs "25000.00").
            row["row_hash"] = row_hash("rpay", [uuid])
            out.append(row)
        return out


class RPayWDGatewayParser(BaseParser):
    """Gateway RafflesPay sisi WD/disbursement (CSV, brand panel-Nexus mis. BBS/BO7).

    Beda dari `RPayGatewayParser` (rail DP: anchor `Customer Username` == username
    panel). Laporan disbursement ini TANPA username — kunci pasti = `External ID`
    (nomor tiket `W...`) == `Ticket Number` panel WD -> pass 0 ticket-join engine
    (pola sama NXPay/QHoki). `UUID` RafflesPay DISIMPAN di raw saja, TIDAK di
    `reference`: Remarks panel Nexus terbukti tak memuatnya, dan aturan blocked
    engine mengasingkan gateway ber-reference asing dari pencocokan (pelajaran
    sama dgn RPay DP). `Disbursed Amount` = uang riil keluar (== Withdrawal Amount
    panel, terverifikasi 12-07-2026). Hanya baris `Transfer Status` = Success
    (uang benar-benar keluar); selalu WD (`flow` diabaikan).
    """

    source_key = "gateway"

    def parse(self, path, flow=""):
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            rows = list(csv.DictReader(f))
        out = []
        for r in rows:
            ticket = str(r.get("External ID", "") or "").strip()
            # Transfer Status = Success => uang benar-benar keluar. Ini satu-satunya
            # penentu (deteksi pun mengunci token "transfer status", jadi konsisten:
            # tak ada baris tersaring diam-diam karena kolom lain hilang).
            transfer = str(r.get("Transfer Status", "") or "").strip().lower()
            if not ticket or transfer != "success":
                continue
            amt = abs(parse_decimal(r.get("Disbursed Amount")))
            occurred = parse_dt(r.get("Date"), dayfirst=True)
            # Selalu WD: laporan disbursement tak pernah jadi deposit. `flow`
            # diabaikan supaya salah-pilih DP di UI tak bisa membalik tanda.
            row = {
                "source_type": "gateway",
                "occurred_at": occurred,
                "posted_date": occurred.date() if occurred else None,
                "jenis": "wd",
                "amount": amt,
                "credit_delta": Decimal("0"),
                "money_delta": -amt,
                "fee": parse_decimal(r.get("Fee")),
                "bonus": Decimal("0"),
                "balance_after": None,
                "ticket_no": ticket,
                "username": "",
                "reference": "",
                "counterparty": str(r.get("Account Name", "") or "").strip(),
                "description": f"RPAY WD {r.get('Bank Name', '')}".strip(),
                "raw": {k: ("" if v is None else str(v)) for k, v in r.items() if k},
            }
            # UUID RafflesPay unik per percobaan disbursement; + ticket sebagai
            # cadangan. TANPA nominal supaya idempotensi tak goyah oleh format
            # angka ("1000000" vs "1000000.0" vs "1000000.00").
            row["row_hash"] = row_hash(
                "rpay_wd", [str(r.get("UUID", "") or "").strip(), ticket])
            out.append(row)
        return out


class RPayDPXlsxParser(BaseParser):
    """Gateway RafflesPay sisi DP, varian XLSX (brand panel-Nexus mis. BBS).

    Beda dari `rpay` (CSV ber-`Customer Username`/`UUID`): varian ini laporan
    "Deposit QRIS" panel ber-gateway RafflesPay yang membawa `Ticket Number`
    (D...) == panel DP -> pass 0 ticket-join engine. `RRN` DISIMPAN di raw
    saja, TIDAK di `reference`: ada duplikat nyata (9 dari 1.233, sampel BBS
    16-07-2026) dan aturan blocked engine mengasingkan reference asing.
    `Amount (IDR)` sudah rupiah penuh (`Amount (Chip)` = ribuan versi panel —
    JANGAN dipakai). Baris `Status=Success` diambil TERMASUK yang
    `Ticket Status=failed`: uang masuk tanpa kredit panel harus muncul sebagai
    "Tidak Ada di Panel", bukan hilang di parser. Selalu DP: `flow` diabaikan.
    """

    source_key = "gateway"

    def parse(self, path, flow=""):
        _, rows = read_xlsx_rows(path)
        out = []
        for r in rows:
            ticket = str(r.get("Ticket Number", "") or "").strip()
            status = str(r.get("Status", "") or "").strip().lower()
            if not ticket or status != "success":
                continue
            amt = abs(parse_decimal(r.get("Amount (IDR)")))
            occurred = parse_dt(r.get("Date"), dayfirst=True)
            rrn = str(r.get("RRN", "") or "").strip()
            row = {
                "source_type": "gateway",
                "occurred_at": occurred,
                "posted_date": occurred.date() if occurred else None,
                "jenis": "depo",
                "amount": amt,
                "credit_delta": Decimal("0"),
                "money_delta": amt,
                "fee": parse_decimal(r.get("Admin Fee")),
                "bonus": Decimal("0"),
                "balance_after": None,
                "ticket_no": ticket,
                "username": str(r.get("Player", "") or "").strip(),
                "reference": "",
                "counterparty": "",
                "description": f"RPAY QR {rrn}".strip(),
                "raw": {k: ("" if v is None else str(v)) for k, v in r.items() if k},
            }
            row["row_hash"] = row_hash("rpay_xlsx", [ticket, rrn])
            out.append(row)
        return out


class RPayWDXlsxParser(BaseParser):
    """Gateway RafflesPay sisi WD, varian XLSX header dua-tingkat (brand BBS).

    Beda dari `rpay_wd` (CSV ber-`External ID`/`Transfer Status`): header grup
    di baris 1 (Beneficiary / Amount / Status) + sub-kolom di baris 2 (Bank,
    Name, Number / Amount, Disbursed Amount, Fee / Status, Approve, Reject,
    Transfer), data mulai baris 3 -> di-flatten manual (sub-kolom menang bila
    terisi). Kunci pasti = `Ticket` (W...) == `Ticket Number` panel WD -> pass
    0. Hanya baris `Transfer=success` (uang benar-benar keluar). `Disbursed
    Amount` = uang riil keluar. `Beneficiary Number` (nomor rekening/e-wallet
    tujuan) tersimpan di raw. Selalu WD: `flow` diabaikan.
    """

    source_key = "gateway"

    def parse(self, path, flow=""):
        grid = read_xlsx_grid(path)
        if len(grid) < 3:
            return []
        top, sub = grid[0], grid[1]
        width = max(len(top), len(sub))

        def _cell(row, i):
            v = row[i] if i < len(row) else None
            return str(v).strip() if v is not None else ""

        headers = [(_cell(sub, i) or _cell(top, i)) for i in range(width)]
        out = []
        for raw_row in grid[2:]:
            r = {h: c for h, c in zip(headers, raw_row) if h}
            ticket = str(r.get("Ticket", "") or "").strip()
            transfer = str(r.get("Transfer", "") or "").strip().lower()
            if not ticket or transfer != "success":
                continue
            amt = abs(parse_decimal(r.get("Disbursed Amount")))
            occurred = parse_dt(r.get("Date"), dayfirst=True)
            row = {
                "source_type": "gateway",
                "occurred_at": occurred,
                "posted_date": occurred.date() if occurred else None,
                "jenis": "wd",
                "amount": amt,
                "credit_delta": Decimal("0"),
                "money_delta": -amt,
                "fee": parse_decimal(r.get("Fee")),
                "bonus": Decimal("0"),
                "balance_after": None,
                "ticket_no": ticket,
                "username": str(r.get("Player", "") or "").strip(),
                "reference": "",
                "counterparty": str(r.get("Name", "") or "").strip(),
                "description": f"RPAY WD {r.get('Bank', '')}".strip(),
                "raw": {k: ("" if v is None else str(v)) for k, v in r.items() if k},  # Nomor tujuan (rekening/e-wallet) ada di raw["Number"] — kunci hasil flatten, BUKAN "Beneficiary Number".
            }
            # ID RafflesPay unik per baris; + ticket cadangan. TANPA nominal
            # supaya idempotensi tak goyah oleh variasi format angka.
            row["row_hash"] = row_hash(
                "rpay_wd_xlsx", [str(r.get("ID", "") or "").strip(), ticket])
            out.append(row)
        return out
