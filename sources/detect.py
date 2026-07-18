"""Auto-deteksi jenis sumber file dari tanda-tangan header/isi.

detect_source(path, filename) -> [{"parser_key", "confidence"}] terurut menurun.
parser_key mengacu ke kunci sources.services.PARSERS.
"""
import os

import openpyxl


def _ext(filename):
    return os.path.splitext(filename)[1].lower()


def _xlsx_tokens(path, max_rows=3):
    from .parsers.base import _raw_xlsx_rows
    toks = set()
    grid = None
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        grid = [list(r) for _, r in zip(range(max_rows), ws.iter_rows(values_only=True))]
        wb.close()
    except Exception:
        grid = None
    # openpyxl kadang mengembalikan baris 'garbled' (sel mayoritas None) untuk
    # exporter non-standar -> pakai reader mentah bila sel terisi < 2.
    n_cells = sum(1 for row in (grid or []) for c in row if c not in (None, ""))
    if n_cells < 2:
        try:
            grid = _raw_xlsx_rows(path, nrows=max_rows)
        except Exception:
            grid = grid or []
    for row in grid:
        for c in row:
            if c is not None and c != "":
                toks.add(str(c).strip().lower())
    return toks


def _csv_text(path, n=6):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            return "\n".join(line.strip().lower() for _, line in zip(range(n), f))
    except Exception:
        return ""


def _pdf_text(path, max_chars=1500):
    """Teks halaman-1 PDF (lower-case) untuk sniff tanda-tangan. '' bila gagal."""
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return (pdf.pages[0].extract_text() or "")[:max_chars].lower()
    except Exception:
        return ""


def _pdf_key(text):
    """Rute PDF: mutasi BNI 'HISTORI TRANSAKSI' -> bni_pdf; selain itu bca_pdf."""
    t = (text or "").lower()
    if "histori transaksi" in t and ("uraian transaksi" in t or "saldo akhir" in t):
        return "bni_pdf"
    return "bca_pdf"


def _has(toks, needle):
    return any(needle in t for t in toks)


def detect_source(path, filename=""):
    filename = filename or os.path.basename(path)
    ext = _ext(filename)
    fn = filename.lower()
    scored = {}

    def add(key, conf):
        scored[key] = max(scored.get(key, 0.0), conf)

    if ext in (".xlsx", ".xls"):
        t = _xlsx_tokens(path)
        if _has(t, "ticket number") and _has(t, "user name") and (_has(t, "deposit amount") or _has(t, "withdrawal amount")):
            add("panel", 0.95)
        if _has(t, "ticket number") and (_has(t, "admin fee") or _has(t, "account title")) \
                and not _has(t, "deposit amount") and not _has(t, "payment gateway"):
            add("nxpay", 0.90)
        if _has(t, "payment gateway") and _has(t, "rrn") and _has(t, "amount (chip)"):
            add("rpay_xlsx", 0.95)  # RafflesPay DP varian XLSX (BBS)
        if _has(t, "source of funds") and _has(t, "disbursed amount") and _has(t, "beneficiary"):
            add("rpay_wd_xlsx", 0.95)  # RafflesPay WD XLSX dua-tingkat (BBS)
        if _has(t, "kategori") and (_has(t, "credit awal") or _has(t, "credit akhir")):
            add("bracket", 0.95)
        if _has(t, "client reference") and (_has(t, "settlement time") or _has(t, "txn id")):
            add("qrflyer", 0.90)
        # Sinyal nama-file-murni (tanpa dukungan header) = 0.75, di bawah ambang
        # konfirmasi 0.8: nama file saja tak boleh merutekan parser tanpa tanya.
        if _has(t, "qris") or _has(t, "qr flyer"):
            add("qrflyer", 0.85)
        elif "qrflyer" in fn or "qris" in fn or "qr flyer" in fn:
            add("qrflyer", 0.75)
        if _has(t, "e-statement") or _has(t, "rekening koran"):
            add("mandiri", 0.80)
        elif "mandiri" in fn:
            add("mandiri", 0.75)
        if _has(t, "orderid") and _has(t, "grandtotal") and _has(t, "branchnominal"):
            add("cor_qris_gateway", 0.95)
        if _has(t, "order id (merchant)") and (_has(t, "recipientname") or _has(t, "accountnumber")):
            add("cor_qris_wd_gateway", 0.95)
        if _has(t, "whitelabel transaction id") and _has(t, "nmid"):
            add("qhoki", 0.95)
        if _has(t, "from bank") and _has(t, "destination bank") and _has(t, "approved date"):
            add("cor_panel_bank", 0.95)
        if _has(t, "transaction id") and _has(t, "username") and _has(t, "requested date") \
                and not _has(t, "kategori") and not _has(t, "orderid"):
            add("cor_panel_qris", 0.90)
    elif ext == ".csv":
        c = _csv_text(path)
        if "mutasi_debet" in c or "mutasi_kredit" in c or "tgl_tran" in c:
            add("bri", 0.95)
        if "cabang" in c and "keterangan" in c and "saldo" in c:
            add("bca_csv", 0.85)
        elif "bca" in fn:  # nama file saja -> di bawah ambang konfirmasi 0.8
            add("bca_csv", 0.75)
        if "customer username" in c and "acquirer merchant" in c:
            add("rpay", 0.95)
        if "external id" in c and "disbursed amount" in c and "transfer status" in c:
            add("rpay_wd", 0.95)  # RafflesPay WD/disbursement (BBS/BO7)
        if "whitelabel transaction id" in c:
            add("qhoki", 0.95)  # sebagian brand ekspor laporan QRIS-HOKI sbg CSV
    elif ext == ".pdf":
        key = _pdf_key(_pdf_text(path))
        add(key, 0.9 if key == "bni_pdf" else 0.75)

    ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)
    return [{"parser_key": k, "confidence": c} for k, c in ranked]
