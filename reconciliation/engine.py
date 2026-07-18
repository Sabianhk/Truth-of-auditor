"""Engine pencocokan (rule-based & tunable).

Tiap relasi punya Matcher sendiri (pluggable lewat MATCHERS). Hasil = MatchResult
dengan bucket cocok / tidak_cocok / perlu_tinjau + reason. Toleransi dari ToleranceProfile.
"""
import logging
import re
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta

from django.db import transaction as db_tx
from django.db.models import Count, Q, Sum
from rapidfuzz import fuzz

from sources.parsers.base import clean_name
from transactions.models import Transaction

from .models import MatchResult, MatchRun, ReconBatch, ToleranceProfile

logger = logging.getLogger(__name__)

MONEY_SOURCES = ["bank", "gateway"]

# Nama korporat agregator e-wallet yang muncul di mutasi bank (riset 2026-07-05):
# transfer dari/ke e-wallet tampil atas nama PT pengelolanya, bukan nama pemain.
_AGGREGATORS = [
    ("ESPAY", "DANA"), ("ANAK BANGSA", "GoPay"), ("DOMPET ANAK", "GoPay"),
    ("AIRPAY", "ShopeePay"), ("VISIONET", "OVO"), ("FINARYA", "LinkAja"),
    ("BIFAST", "BI-FAST"),
]


def _wallet_label(name):
    """Label kanal e-wallet bila `name` adalah nama korporat agregator."""
    up = (name or "").upper()
    for token, label in _AGGREGATORS:
        if token in up:
            return label
    return None


_NORM_RE = re.compile(r"[^A-Z]")
_DIGIT_RUN_RE = re.compile(r"\d{9,15}")
# WD e-wallet via BRI: 'BRIVA<kode kanal><no HP>' — kode menempel di depan
# nomor sehingga deret totalnya 16-18 digit dan LOLOS dari _DIGIT_RUN_RE
# (terpotong 15 digit + kode ikut terbawa). Kode kanal dari matriks klien:
# DANA 88810, GOPAY 30135, OVO 88099, SHOPEEPAY 112, LINK AJA 91188.
# Dipakai via .sub() yang MENGGANTI span dengan nomornya: sekaligus membuang
# deret tercemar kode dari hasil _DIGIT_RUN_RE (temuan review adversarial).
_BRIVA_RE = re.compile(
    r"BRIVA\s*(?:88810|30135|88099|91188|112)(\d{9,15})", re.IGNORECASE
)


def _panel_phone(t):
    """Nomor HP/rekening wallet pemain dari raw['Player Bank'] (segmen ke-3),
    dinormalisasi: buang non-digit + nol/62 di depan. '' bila tak ada."""
    pb = (t.raw or {}).get("Player Bank") or ""
    parts = pb.split("|")
    digits = re.sub(r"\D", "", parts[2] if len(parts) > 2 else "")
    return digits.lstrip("0").removeprefix("62").lstrip("0")


_MONEY_KEY_RE = re.compile(
    r"saldo|balance|amount|nominal|jumlah|credit|debit|mutasi|fee|total|harga",
    re.IGNORECASE,
)
# Angka uang berformat desimal ('81234567.00') di teks bebas — buang sebelum
# scan deret digit (saldo/nominal ≥Rp100jt = deret ≥9 digit → "nomor HP" palsu).
_MONEY_DECIMAL_RE = re.compile(r"\d+[.,]\d{1,2}(?=\s|$)")


def _money_phones(t):
    """Deret digit (≥9) di baris uang — mutasi VA e-wallet (FTFVA/DANA, GOPAY
    TOPUP, dst) menaruh nomor HP/VA tujuan di teks keterangan.
    W1-8: key raw berisi UANG (saldo/nominal/fee dst) tidak ikut di-scan, dan
    pola angka desimal uang dibuang — angka uang bukan nomor HP."""
    text = " ".join(
        str(v) for k, v in (t.raw or {}).items() if not _MONEY_KEY_RE.search(str(k))
    )
    out = set()
    joined = text + " " + (t.counterparty or "")
    # Span BRIVA diganti nomor bersihnya SEBELUM scan deret digit: nomor ikut
    # terekstrak DAN deret tercemar kode (mis. '301350831448892') hilang.
    joined = _BRIVA_RE.sub(lambda m: f" {m.group(1)} ", joined)
    joined = _MONEY_DECIMAL_RE.sub(" ", joined)
    for run in _DIGIT_RUN_RE.findall(joined):
        norm = run.lstrip("0").removeprefix("62").lstrip("0")
        if len(norm) >= 9:
            out.add(norm)
    return out


def _phone_match(pp, phones):
    """Cocok bila salah satu ujung sama (bank sering memotong digit)."""
    if not pp or len(pp) < 9:
        return False
    for ph in phones:
        if pp == ph or pp.endswith(ph) or ph.endswith(pp) \
           or pp.startswith(ph) or ph.startswith(pp):
            return True
    return False


def _norm_owner(s):
    return _NORM_RE.sub("", (s or "").upper())


def _expected_owner(t):
    """Pemilik rekening TUJUAN menurut panel: segmen tengah raw['Bank Title']
    (mis. 'BCA|HENDI|712...' → 'HENDI'). Kosong bila tidak ada."""
    bt = (t.raw or {}).get("Bank Title") or ""
    parts = bt.split("|")
    return _norm_owner(parts[1] if len(parts) > 1 else bt)


def _route_ok(expected, owner, source_key):
    """Apakah baris uang berada di rekening yang ditunjuk panel?
    None = tak bisa dinilai (tanpa Bank Title / tanpa nama file)."""
    if not expected or not owner:
        return None
    if source_key == "gateway":
        for tok in ("FLYER", "NXPAY"):
            if tok in expected and tok in owner:
                return True
    return fuzz.partial_ratio(expected, owner) >= 85


# --- Kunci kanal gateway ------------------------------------------------------
# Panel mendeklarasikan kanal pembayarannya di bank_title ("QRISRPAY",
# "NXPAY DEPOSIT QR", "QRISHOKI", ...); parser gateway menulis identitasnya di
# awal description ("RPay ...", "NXPAY ...", "QHOKI ...", "QRFLYER ..."). Uang
# gateway X dilarang dipasangkan pass identitas dengan baris panel yang menunjuk
# gateway Y yang dikenal berbeda — mencegah deposit kembar beda kanal saling
# curi saat file uang belum lengkap (kasus M77: uang RPay tersedot baris NXPAY).
# Fail-open: token tak dikenal / kosong -> tanpa larangan (perilaku lama).
_GW_CHANNEL_TOKENS = ("NXPAY", "RPAY", "HOKI", "FLYER")


def _channel_from(text):
    up = (text or "").upper()
    for tok in _GW_CHANNEL_TOKENS:
        if tok in up:
            return tok
    return ""


def _panel_channel(t):
    return _channel_from(getattr(t, "bank_title", ""))


def _money_channel(t):
    if t.source_type.key != "gateway":
        return ""
    # Hanya awalan description (ditulis parser sendiri) — bukan teks bebas.
    return _channel_from((t.description or "")[:12])

# Detail baku hasil no_money — juga dipakai untuk MENGEMBALIKAN hasil yang
# di-flip late settlement, jadi string ini harus tetap satu sumber kebenaran.
NO_MONEY_DETAIL = "Tak ada padanan nominal+tanggal di Mutasi Bank"
# Detail no_money ketika ADA kandidat nominal+tanggal tapi identitasnya beda
# (skor < NAME_REVIEW_FLOOR) → biarkan menunggu settlement tanggal berikutnya.
NO_MONEY_WAIT_DETAIL = "Ada kandidat nominal+tanggal tapi identitas beda — menunggu settlement"

# Anchor UTAMA (identitas unik) yang menentukan pasangan; nominal+tanggal hanya
# PENDUKUNG. Nama mirip pada pita ini → perlu_tinjau ('nama mirip'); di bawahnya
# TIDAK dipasangkan (menunggu settlement). Kalibrasi via `validate_brands`;
# dinaikkan ke ToleranceProfile bila per-toko perlu beda (YAGNI sekarang).
NAME_REVIEW_FLOOR = 60


def _included_money_sources(include):
    """Sumber uang yang ikut run. include=None → semua (bank+gateway, perilaku lama).
    Jika include diberikan, hanya sumber dengan inc_* dicentang yang dipakai."""
    if include is None:
        return list(MONEY_SOURCES)
    return [k for k in MONEY_SOURCES if include.get(k, True)]


def amount_ok(a, b, tol):
    a, b = abs(a), abs(b)
    diff = abs(a - b)
    if diff <= tol.amount_abs_tol:
        return True, diff
    if tol.amount_pct_tol and max(a, b) > 0 and (diff / max(a, b)) <= float(tol.amount_pct_tol):
        return True, diff
    # Sampai sini = di luar semua toleransi → False, titik. (Bentuk lama
    # `return diff == 0, diff` tak pernah True: diff 0 sudah tertangkap
    # cabang abs_tol di atas — jangan menyamarkan cabang mati sebagai logika.)
    return False, diff


def _name_score(a, b):
    """Skor kemiripan nama, toleran nama terpotong (BCA ~18 char, BRI ~17).
    Nama sudah diisolasi di parser; di sini tinggal normalisasi + fuzzy."""
    a = clean_name(a).upper()
    b = clean_name(b).upper()
    if not a or not b:
        return 0.0
    score = fuzz.token_set_ratio(a, b)
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 10:  # nama sangat pendek jangan dianggap potongan nama lain
        score = max(score, fuzz.ratio(shorter, longer[: len(shorter)]))
    return score


def date_ok(left_dt, right_dt, tol):
    """Terarah: sisi uang (right) >= sisi kredit (left), dalam window hari."""
    if left_dt is None or right_dt is None:
        return False
    return 0 <= (right_dt.date() - left_dt.date()).days <= tol.date_window_days


def _day_start(d):
    """date → datetime naive 00:00 (USE_TZ=False, WIB). Basis filter sargable."""
    return datetime.combine(d, time.min)


def _date_range_q(field, dfrom=None, dto=None):
    """Q rentang tanggal INKLUSIF [dfrom..dto] pada DateTimeField, bentuk SARGABLE:
    `field >= dfrom 00:00 AND field < (dto+1) 00:00`.

    Setara `__date__gte/__date__lte`, tapi `__date__` dikompilasi Postgres jadi
    `(occurred_at)::date` sehingga index (source_type, occurred_at) tak terpakai
    (seq-scan di 497rb+ baris). Perbandingan datetime polos memakai index.
    String 'YYYY-MM-DD' dikoersi (CLI match mengirim string)."""
    dfrom, dto = _as_date(dfrom), _as_date(dto)
    q = Q()
    if dfrom:
        q &= Q(**{f"{field}__gte": _day_start(dfrom)})
    if dto:
        q &= Q(**{f"{field}__lt": _day_start(dto + timedelta(days=1))})
    return q


def _date_filter(qs, dfrom, dto):
    if dfrom or dto:
        qs = qs.filter(_date_range_q("occurred_at", dfrom, dto))
    return qs


def _toko_filter(qs, toko):
    return qs.filter(toko=toko) if toko is not None else qs


def _can_still_settle(d, recon_date, window):
    """Baris kredit tanggal `d` masih bisa dapat uang pada run tanggal berikutnya
    (>= recon_date+1): d + window >= recon_date + 1  ⟺  d > recon_date - window.
    Catatan: untuk carried, `window` = toleransi batch ASAL baris itu (W1-4) —
    ganti profil antar hari tidak menggeser deadline baris lama."""
    return d is not None and d > recon_date - timedelta(days=window)


def _active(qs):
    """Pool AKTIF = transaksi yang belum dikonsumsi batch mana pun. Transaksi yang
    sudah dipakai (consumed_by_batch terisi) tidak ikut kelengkapan/pencocokan/total."""
    return qs.filter(consumed_by_batch__isnull=True)


def check_completeness(toko, date_from=None, date_to=None):
    qs = _active(_toko_filter(Transaction.objects.filter(is_duplicate=False), toko))
    qs = _date_filter(qs, date_from, date_to)

    def has(**kw):
        return qs.filter(**kw).exists()

    comp = {
        "panel_dp": has(source_type__key="panel", jenis="depo"),
        "panel_wd": has(source_type__key="panel", jenis="wd"),
        "bracket": has(source_type__key="bracket"),
        "bank": has(source_type__key="bank"),
        "gateway": has(source_type__key="gateway"),
    }
    comp["panel"] = comp["panel_dp"] or comp["panel_wd"]
    comp["minimum_met"] = comp["panel"] and (comp["bank"] or comp["gateway"])
    return comp


class PanelBracketMatcher:
    """Join via Ticket Number (kuat). Cek kecocokan nominal."""

    def sides(self, dfrom, dto, toko=None, include=None):
        left = Transaction.objects.filter(source_type__key="panel", is_duplicate=False)
        # include: hilangkan sisi Panel yang tidak dicentang (depo/wd) bila diberikan.
        if include is not None:
            if not include.get("panel_dp", True):
                left = left.exclude(jenis="depo")
            if not include.get("panel_wd", True):
                left = left.exclude(jenis="wd")
        left = _date_filter(_active(_toko_filter(left, toko)), dfrom, dto)
        right = _date_filter(
            _active(_toko_filter(
                Transaction.objects.filter(source_type__key="bracket", is_duplicate=False).exclude(ticket_no=""), toko
            )),
            dfrom, dto,
        )
        return list(left), list(right)

    def match(self, run, left, right, carried_windows=None):
        tol = run.tolerance
        bidx = {}
        for b in right:
            bidx.setdefault(b.ticket_no, []).append(b)
        used, out = set(), []
        for p in left:
            if not p.ticket_no:   # baris tanpa ticket dinilai money-matcher, bukan bracket
                continue
            chosen = next((b for b in bidx.get(p.ticket_no, []) if b.id not in used), None)
            if chosen:
                used.add(chosen.id)
                ok, diff = amount_ok(p.amount, chosen.amount, tol)
                if ok:
                    out.append(MatchResult(run=run, bucket=MatchResult.Bucket.COCOK, left=p, right=chosen,
                                           score=100, reason_code="ticket+amount"))
                else:
                    out.append(MatchResult(run=run, bucket=MatchResult.Bucket.TINJAU, left=p, right=chosen,
                                           score=70, reason_code="amount_mismatch",
                                           reason_detail=f"selisih {diff}: Panel {p.amount} vs Bracket {chosen.amount}"))
            else:
                out.append(MatchResult(run=run, bucket=MatchResult.Bucket.TIDAK, left=p, right=None,
                                       score=0, reason_code="no_bracket",
                                       reason_detail="Ticket Panel tidak ada di Bracket"))
        for b in right:
            if b.id not in used:
                out.append(MatchResult(run=run, bucket=MatchResult.Bucket.TIDAK, left=None, right=b,
                                       score=0, reason_code="no_panel",
                                       reason_detail="Ticket Bracket tidak ada di Panel"))
        return out


class _MoneyMatcher:
    """Cocokkan sisi kredit (Panel/Bracket) ke sisi UANG (Bank/Gateway) — multi-pass:

    pass 0  ticket-join gateway (kunci pasti; gateway ber-ticket asing TIDAK
            pernah dipasangkan fuzzy — biar tampil sebagai uang tanpa pasangan),
    pass 1  identitas kuat (skor >= threshold) di-assign GLOBAL urut skor —
            baris lemah tidak bisa mencuri kandidat milik baris kuat,
    pass 2  near-miss identitas kuat: nominal beda kecil (fee) / uang H-1,
    pass 3  sisa berbasis nominal+tanggal → perlu_tinjau, prioritas rekening
            yang ditunjuk panel (raw['Bank Title']) supaya tidak salah sanding.
    """

    left_key = "panel"

    def sides(self, dfrom, dto, toko=None, include=None):
        left = Transaction.objects.filter(
            source_type__key=self.left_key, is_duplicate=False
        ).filter(jenis__in=["depo", "wd"])
        # include: sisi Panel — buang depo/wd yang tidak dicentang.
        if include is not None:
            if not include.get("panel_dp", True):
                left = left.exclude(jenis="depo")
            if not include.get("panel_wd", True):
                left = left.exclude(jenis="wd")
        left = _date_filter(_active(_toko_filter(left, toko)), dfrom, dto)

        # right: hanya sumber UANG yang dicentang (bank dan/atau gateway).
        money_keys = _included_money_sources(include)
        right = _date_filter(
            _active(_toko_filter(
                Transaction.objects.filter(source_type__key__in=money_keys, is_duplicate=False).exclude(jenis="admin"),
                toko,
            )),
            dfrom, dto,
        ).select_related("source_type", "upload")
        return list(left), list(right)

    @staticmethod
    def _identity(p, b):
        """Skor identitas: nomor HP/VA wallet persis > username persis > fuzzy nama.
        Mutasi VA e-wallet (FTFVA/DANA, GOPAY TOPUP) tak membawa nama pengirim,
        tapi membawa nomor tujuan — sama dengan raw['Player Bank'] panel."""
        pp = getattr(p, "_phone", None)
        if pp is None:
            pp = p._phone = _panel_phone(p)
        phones = getattr(b, "_phones", None)
        if phones is None:
            phones = b._phones = _money_phones(b)
        if _phone_match(pp, phones):
            return 100.0
        if p.username and b.username:
            s = 100.0 if p.username.lower() == b.username.lower() else 40.0
            if p.counterparty and b.counterparty:
                s = max(s, _name_score(p.counterparty, b.counterparty))
            return s
        return _name_score(p.counterparty, b.counterparty)

    def match(self, run, left, right, carried_windows=None):
        tol = run.tolerance
        carried_windows = carried_windows or {}
        out, used, matched = [], set(), set()

        bidx = defaultdict(list)
        gw_ticket = defaultdict(list)
        gw_ref = defaultdict(list)
        owners = {}
        for b in right:
            bidx[(int(abs(b.money_delta)), b.money_delta > 0)].append(b)
            if b.source_type.key == "gateway" and b.ticket_no:
                gw_ticket[b.ticket_no].append(b)
            if b.source_type.key == "gateway" and b.reference:
                gw_ref[b.reference].append(b)
            if b.upload_id not in owners:
                owners[b.upload_id] = _norm_owner(
                    b.upload.original_name if b.upload else ""
                )
        panel_tickets = {p.ticket_no for p in left if p.ticket_no}
        panel_refs = {p.reference for p in left if p.reference}

        # W4-5a: indeks nominal per arah utk pass 2 — rentang [amt-tol, amt+tol]
        # via bisect, bukan scan linear SEMUA kunci bidx per baris panel
        # (O(sisa × distinct nominal)). Urutan kunci hasil HARUS identik dgn
        # urutan insersi bidx (pemenang tie skor pass 2 = kandidat pertama),
        # maka posisi insersi disimpan dan subset bisect diurutkan menurutnya.
        amt_sorted = {True: [], False: []}
        amt_order = {True: [], False: []}
        for i, (a, s) in enumerate(bidx):
            amt_sorted[s].append((a, i))
        for s in (True, False):
            amt_sorted[s].sort()
            amt_order[s] = [i for _, i in amt_sorted[s]]
            amt_sorted[s] = [a for a, _ in amt_sorted[s]]

        # W4-5b: memo skor identitas — pass 3 menilai ulang pasangan yang sudah
        # diskor pass 1/2. _identity murni per (p, b) → hasil identik, sekali hitung.
        ident_memo = {}

        def identity(p, b):
            k = (p.id, b.id)
            s = ident_memo.get(k)
            if s is None:
                s = ident_memo[k] = self._identity(p, b)
            return s

        def emit(p, b, bucket, score, reason, detail=""):
            matched.add(p.id)
            if b is not None:
                used.add(b.id)
            out.append(MatchResult(run=run, bucket=bucket, left=p, right=b,
                                   score=score, reason_code=reason, reason_detail=detail))

        # --- pass 0: ticket-join gateway (seperti Panel↔Bracket) ---
        # W7-5 — KEPUTUSAN (disengaja, jangan "diperbaiki"): join TX-ID /
        # reference PERSIS di pass 0/0b TIDAK menghormati window tanggal —
        # termasuk baris CARRIED yang sudah lewat window batch asalnya (panel
        # D27 window 1 tetap boleh settle ke tiket sama D29). TX-ID dibuat
        # gateway dan hadir di KEDUA sisi = identitas uang pasti; menolaknya
        # karena window berarti auditor kehilangan settle sah dan baris malah
        # kadaluarsa palsu. Window hanya membatasi pencocokan FUZZY/nominal
        # (pass 1-3) yang bisa salah pasang. Dikunci tes
        # TicketPersisTembusWindowTests (tests_carry).
        # W1-7a: di antara duplikat ber-ticket sama, nominal PERSIS menang
        # (fallback selisih terkecil) — urutan insert arbitrer, duplikat salah
        # nominal tidak boleh mengalahkan kandidat yang persis.
        for p in left:
            if not p.ticket_no:
                continue
            cands = [
                b for b in gw_ticket.get(p.ticket_no, [])
                if b.id not in used and (p.money_delta > 0) == (b.money_delta > 0)
            ]
            if not cands:
                continue
            amt = int(abs(p.money_delta))
            b = min(cands, key=lambda b_: abs(amt - int(abs(b_.money_delta))))
            diff = abs(amt - int(abs(b.money_delta)))
            if diff == 0:
                emit(p, b, MatchResult.Bucket.COCOK, 100, "ticket")
            else:
                emit(p, b, MatchResult.Bucket.TINJAU, 90, "ticket_amount",
                     f"ticket sama, selisih nominal {diff:,}")
        # --- pass 0b: reference-join gateway (kunci pasti non-ticket, mis. UUID QRIS) ---
        for p in left:
            if p.id in matched or not p.reference:
                continue
            for b in gw_ref.get(p.reference, []):
                if b.id in used or (p.money_delta > 0) != (b.money_delta > 0):
                    continue
                diff = abs(int(abs(p.money_delta)) - int(abs(b.money_delta)))
                if diff == 0:
                    emit(p, b, MatchResult.Bucket.COCOK, 100, "reference")
                else:
                    emit(p, b, MatchResult.Bucket.TINJAU, 90, "reference_amount",
                         f"reference sama, selisih nominal {diff:,}")
                break
        # Gateway ber-ticket/ber-reference yang TAK dikenal panel bukan kandidat
        # fuzzy siapa pun. W1-7b: duplikat sisa ber-ticket DIKENAL panel yang tak
        # terpakai pass 0 juga diblokir — ticketnya sudah menunjuk baris panel
        # tertentu, jangan sampai dipinang fuzzy baris panel lain (jadi kandidat
        # no_panel biasa di B2).
        blocked = {
            b.id for t, lst in gw_ticket.items()
            for b in lst if t not in panel_tickets or b.id not in used
        } | {
            b.id for ref, lst in gw_ref.items() if ref not in panel_refs for b in lst
        }

        def kandidat(p, *, lo=0, hi=None, tol_amt=0):
            if hi is None:
                # W6-2: window baris CARRIED = window batch ASALnya penuh —
                # kontrak T+n yang dijanjikan saat baris lahir (konsisten dgn
                # expiry W1-4 & deadline settlement page), bukan campur/max dgn
                # profil hari ini. Toleransi NOMINAL & fuzzy tetap profil hari
                # ini. Baris non-carried memakai window profil run seperti biasa.
                hi = carried_windows.get(p.id, tol.date_window_days)
            d = p.occurred_at.date() if p.occurred_at else None
            if d is None:
                return
            pchan = getattr(p, "_chan", None)
            if pchan is None:
                pchan = p._chan = _panel_channel(p)
            amt, pos = int(abs(p.money_delta)), p.money_delta > 0
            if tol_amt:
                arr, order = amt_sorted[pos], amt_order[pos]
                lo_i = bisect_left(arr, amt - tol_amt)
                hi_i = bisect_right(arr, amt + tol_amt)
                sel = sorted(
                    (order[j], arr[j]) for j in range(lo_i, hi_i) if arr[j] != amt
                )
                keys = [(a, pos) for _, a in sel]
            else:
                keys = [(amt, pos)]
            for key in keys:
                for b in bidx.get(key, []):
                    if b.id in used or b.id in blocked or b.occurred_at is None:
                        continue
                    if pchan:
                        bchan = getattr(b, "_mchan", None)
                        if bchan is None:
                            bchan = b._mchan = _money_channel(b)
                        # Kunci kanal: gateway berbeda yang sama-sama dikenal.
                        if bchan and bchan != pchan:
                            continue
                    delta = (b.occurred_at.date() - d).days
                    if lo <= delta <= hi:
                        yield b, delta

        # --- pass 1: identitas kuat, assignment global urut skor ---
        pairs = []
        for p in left:
            if p.id in matched:
                continue
            expected = _expected_owner(p)
            for b, delta in kandidat(p):
                s = identity(p, b)
                if s >= tol.fuzzy_threshold:
                    route = _route_ok(expected, owners.get(b.upload_id), b.source_type.key)
                    pairs.append((s, route is True, -delta, p, b))
        pairs.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        for s, _, _, p, b in pairs:
            if p.id in matched or b.id in used:
                continue
            emit(p, b, MatchResult.Bucket.COCOK, s, "amount+date+name")

        # --- pass 2: near-miss identitas kuat (fee kecil & uang H-1) ---
        for p in left:
            if p.id in matched:
                continue
            amt = int(abs(p.money_delta))
            best = None
            # Band near-miss: heuristik fee (max(2500, 1%)) ATAU amount_abs_tol
            # profil — knob yang diisi user harus benar-benar melebarkan band
            # (Default abs_tol=0 → perilaku tak berubah).
            for b, delta in kandidat(p, tol_amt=max(2500, amt // 100, int(tol.amount_abs_tol))):
                s = identity(p, b)
                if s >= tol.fuzzy_threshold and (best is None or s > best[0]):
                    best = (s, b)
            if best:
                s, b = best
                diff = abs(int(abs(b.money_delta)) - amt)
                # Identitas PERSIS (nomor HP/username/nama identik = 100) + selisih
                # kecil khas biaya transfer → cocok; identitas fuzzy tetap ditinjau.
                bucket = (MatchResult.Bucket.COCOK if s >= 100
                          else MatchResult.Bucket.TINJAU)
                emit(p, b, bucket, s, "amount_fee",
                     f"identitas cocok, selisih nominal {diff:,} (indikasi fee)")
                continue
            for b, delta in kandidat(p, lo=-1, hi=-1):
                s = identity(p, b)
                if s >= tol.fuzzy_threshold:
                    emit(p, b, MatchResult.Bucket.TINJAU, s, "date_before",
                         "uang tiba sehari SEBELUM tanggal panel")
                    break

        # --- pass 3: sisa berbasis IDENTITAS — nominal+tanggal cuma pendukung ---
        # Nama mirip pada pita [NAME_REVIEW_FLOOR..threshold) → perlu_tinjau,
        # di-assign GLOBAL urut skor (anti-curi, seperti pass 1). Di bawah floor
        # TIDAK dipasangkan: biarkan menunggu settlement tanggal berikutnya —
        # ini yang mencegah WD "Samsul" nyasar ke mutasi "ARI" (skor 36).
        band_pairs = []
        has_candidate = {}
        for p in left:
            if p.id in matched:
                continue
            expected = _expected_owner(p)
            any_cand = False
            for b, delta in kandidat(p):
                any_cand = True
                s = identity(p, b)
                if s >= NAME_REVIEW_FLOOR:
                    route = _route_ok(expected, owners.get(b.upload_id), b.source_type.key)
                    band_pairs.append((s, route is True, -delta, p, b))
            has_candidate[p.id] = any_cand
        band_pairs.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        for s, _, _, p, b in band_pairs:
            if p.id in matched or b.id in used:
                continue
            wallet = _wallet_label(b.counterparty)
            extra = f" — kanal {wallet}" if wallet else ""
            emit(p, b, MatchResult.Bucket.TINJAU, s, "name_partial",
                 f"nama mirip (score {s:.0f}){extra}")
        for p in left:
            if p.id in matched:
                continue
            detail = NO_MONEY_WAIT_DETAIL if has_candidate.get(p.id) else NO_MONEY_DETAIL
            emit(p, None, MatchResult.Bucket.TIDAK, 0, "no_money", detail)
        return out


class PanelBankMatcher(_MoneyMatcher):
    left_key = "panel"


class BracketBankMatcher(_MoneyMatcher):
    """CLI-only (`manage.py match bracket_bank`) — TIDAK dipakai alur web:
    run_batch hanya menjalankan PANEL_BRACKET + PANEL_BANK."""

    left_key = "bracket"


MATCHERS = {
    MatchRun.Relation.PANEL_BRACKET: PanelBracketMatcher,
    MatchRun.Relation.PANEL_BANK: PanelBankMatcher,
    MatchRun.Relation.BRACKET_BANK: BracketBankMatcher,
}


def run_match(relation, tolerance=None, date_from=None, date_to=None, user=None, toko=None, batch=None, include=None,
              carried=None, retro=None, carried_windows=None):
    """`carried` = dict left_id → MatchResult no_money lama (carry-over harian).
    Baris carried ikut pool relasi UANG agar bisa settle terlambat, tapi tidak
    pernah membuat MatchResult baru di run ini; pasangan yang match dikembalikan
    lewat atribut transien `run.late_pairs` untuk di-flip oleh run_batch.
    `retro` = dict tx_id → ReconBatch asal (baris susulan). Baris susulan ikut
    pool biasa, tapi hasil yang ber-anchor padanya dialihkan ke atribut transien
    `run.retro_results` untuk ditulis ke batch asalnya oleh run_batch.
    `carried_windows` = dict left_id → date_window_days batch ASAL baris carried
    (W6-2): matcher memakai window asal utk baris itu, bukan window profil ini."""
    tolerance = tolerance or ToleranceProfile.objects.get(name="Default")
    matcher = MATCHERS[relation]()
    # Atomic menyeluruh: exception di tengah matcher me-rollback MatchRun yang
    # baru dibuat — jangan meninggalkan run yatim tanpa hasil (dipanggil
    # standalone dari CLI; run_batch punya atomic sendiri, nested aman).
    with db_tx.atomic():
        run = _run_match_inner(matcher, relation, tolerance, date_from, date_to,
                               user, toko, batch, include, carried, retro,
                               carried_windows)
    return run


def _run_match_inner(matcher, relation, tolerance, date_from, date_to,
                     user, toko, batch, include, carried, retro,
                     carried_windows=None):
    run = MatchRun.objects.create(
        relation=relation, tolerance=tolerance, date_from=date_from, date_to=date_to,
        created_by=user, batch=batch,
    )
    left, right = matcher.sides(date_from, date_to, toko, include=include)
    if carried and relation == MatchRun.Relation.PANEL_BRACKET:
        # Kesempatan pairing bracket baris carried sudah lewat di batch asalnya —
        # jangan menghasilkan no_bracket/no_panel dobel di batch baru.
        left = [t for t in left if t.id not in carried]
        right = [t for t in right if t.id not in carried]
    results = matcher.match(run, left, right, carried_windows=carried_windows)
    late_pairs, keep = [], results
    if carried and relation != MatchRun.Relation.PANEL_BRACKET:
        keep = []
        for r in results:
            if r.left_id in carried:
                if r.right_id is not None:  # settle terlambat → flip di batch asal
                    late_pairs.append((carried[r.left_id], r))
                # carried tanpa pasangan → drop (tidak_cocok sudah tercatat di asalnya)
            else:
                keep.append(r)
    retro_results = []
    if retro:
        kept = []
        for r in keep:
            anchor = r.left_id if r.left_id is not None else r.right_id
            if anchor is not None and anchor in retro:
                retro_results.append(r)  # hasil baris susulan → milik batch asalnya
            else:
                kept.append(r)
        keep = kept
    with db_tx.atomic():
        MatchResult.objects.bulk_create(keep, batch_size=2000)
    c = Counter(r.bucket for r in keep)
    n_excluded_left = sum(
        1 for t in left
        if (carried and t.id in carried) or (retro and t.id in retro)
    )
    run.summary = {
        "left": len(left) - n_excluded_left, "right": len(right),
        "cocok": c.get("cocok", 0),
        "perlu_tinjau": c.get("perlu_tinjau", 0),
        "tidak_cocok": c.get("tidak_cocok", 0),
    }
    if carried:
        run.summary["late_settled"] = len(late_pairs)
    if retro:
        run.summary["susulan"] = len(retro_results)
    run.save(update_fields=["summary"])
    run.late_pairs = late_pairs
    run.retro_results = retro_results
    # SEMUA uang yang terpakai pasangan hari ini (termasuk yang hasilnya milik
    # batch lain via flip/susulan) — dipakai run_batch utk B1/B2.
    run.used_right_ids = {r.right_id for r in results if r.right_id}
    return run


def _matched_money(runs):
    """Uang REAL yang benar-benar berpasangan ke baris Panel (bucket cocok + perlu_tinjau).

    Dijumlah dari sisi UANG (right) pada MatchResult PANEL_BANK yang punya left&right.
    Bucket TIDAK dikecualikan: pasangan yang DITOLAK auditor (mark_unmatched)
    bukan uang matched — tanpa ini kartu batch tetap "balanced" padahal
    reviewer menolak pasangannya. DP = money_delta>0, WD = abs(money_delta<0).
    """
    panel_bank = [r.id for r in runs if r.relation == MatchRun.Relation.PANEL_BANK]
    dp = wd = 0.0
    if panel_bank:
        results = MatchResult.objects.filter(
            run_id__in=panel_bank, left__isnull=False, right__isnull=False
        ).exclude(bucket=MatchResult.Bucket.TIDAK).select_related("right")
        for res in results:
            md = float(res.right.money_delta)
            if md > 0:
                dp += md
            elif md < 0:
                wd += -md
    return dp, wd


def _carried_qs(tokos):
    """Filter dasar carry-over "menunggu settlement" untuk banyak toko sekaligus —
    hasil no_money LAMA milik baris kredit yang masih AKTIF.
    Hanya relasi UANG (no_money); hasil no_bracket PANEL_BRACKET tidak ikut.
    Run CLI tanpa batch diabaikan (tak ada batch asal untuk konsumsi/flip)."""
    return MatchResult.objects.filter(
        bucket=MatchResult.Bucket.TIDAK, reason_code="no_money",
        left__isnull=False, left__toko__in=tokos,
        left__consumed_by_batch__isnull=True,
        run__batch__isnull=False,
        run__relation__in=[MatchRun.Relation.PANEL_BANK, MatchRun.Relation.BRACKET_BANK],
    )


def _carried_results(toko):
    """left_id → MatchResult no_money carry-over (lihat `_carried_qs`).
    run__tolerance ikut di-select: expiry dinilai pakai window batch ASAL."""
    # run__batch__tolerance ikut: web/settlement.py membaca home.tolerance
    # (deadline baris = toleransi batch ASAL) — tanpa ini satu query per baris.
    qs = _carried_qs([toko]).select_related(
        "left", "run", "run__batch", "run__tolerance", "run__batch__tolerance"
    ).order_by("id")
    return {r.left_id: r for r in qs}  # id terbesar menang (defensif bila ganda)


def pending_settlement_count(toko):
    """Jumlah baris kredit yang masih AKTIF menunggu settlement (untuk info UI).
    COUNT(DISTINCT left) di SQL — setara len(_carried_results) tapi tanpa
    materialisasi objek (halaman /tokos/ sempat ~1,5 dtk karena ini per toko)."""
    return pending_settlement_counts([toko]).get(toko.id, 0)


def pending_settlement_counts(tokos):
    """toko_id → jumlah menunggu settlement, SATU query agregat untuk semua toko."""
    rows = _carried_qs(tokos).values_list("left__toko").annotate(
        n=Count("left_id", distinct=True)
    )
    return dict(rows)


def _retro_homes(toko, recon_date, date_from, date_to, include, exclude_ids=None):
    """tx_id → ReconBatch asal untuk BARIS SUSULAN: baris aktif dalam lingkup yang
    tanggalnya < recon_date dan tanggal itu sudah punya batch harian sendiri.
    Baris carried (punya hasil no_money lama) dikecualikan — jalurnya flip, bukan
    susulan. Tanggal tanpa batch → bukan susulan (diproses batch berjalan)."""
    qs = _consume_scope(toko, date_from, date_to, include).filter(
        occurred_at__lt=_day_start(recon_date)
    )
    if exclude_ids:
        qs = qs.exclude(id__in=exclude_ids)
    rows = list(qs.values_list("id", "occurred_at"))
    if not rows:
        return {}
    dates = {dt.date() for _, dt in rows if dt}
    homes = {
        b.recon_date: b
        for b in ReconBatch.objects.filter(
            toko=toko, recon_date__in=dates
        ).select_related("tolerance")  # W7-1: window home dibaca per baris susulan
    }
    return {i: homes[dt.date()] for i, dt in rows if dt and dt.date() in homes}


def _add_retro_gross(batch, rows):
    """Tambah total POTRET (panel / money_gross) batch asal dengan baris susulan —
    baris ini belum terhitung saat batch asal dijalankan. Selisih/matched dihitung
    ulang terpisah oleh refresh_batch_summary."""
    s = dict(batch.summary or {})
    for flow in ("dp", "wd"):
        f = dict(s.get(flow) or {})
        f["panel"] = float(f.get("panel") or 0)
        f["money_gross"] = float(f.get("money_gross") or 0)
        s[flow] = f
    for t in rows:
        key = t.source_type.key
        if key == "panel" and t.jenis in ("depo", "wd"):
            flow = "dp" if t.jenis == "depo" else "wd"
            s[flow]["panel"] += float(t.amount)
        elif key in MONEY_SOURCES and t.jenis != "admin":
            md = float(t.money_delta)
            if md > 0:
                s["dp"]["money_gross"] += md
            elif md < 0:
                s["wd"]["money_gross"] += -md
    batch.summary = s
    batch.save(update_fields=["summary"])


def _writeback_retro(batch, retro, retro_results, tolerance, user):
    """Tulis hasil baris susulan ke MatchRun relasi sama di batch ASALnya (buat
    run baru bila relasinya belum pernah jalan di sana) + tambah gross batch asal.
    Return set batch asal yang tersentuh (untuk refresh summary)."""
    homes = {}
    groups = {}
    for r in retro_results:
        anchor = r.left_id if r.left_id is not None else r.right_id
        home = retro[anchor]
        groups.setdefault((home.pk, r.run.relation), (home, []))[1].append(r)
    for (home_pk, relation), (home, rs) in groups.items():
        run = MatchRun.objects.filter(batch=home, relation=relation).order_by("id").first()
        if run is None:
            # W6-2b: run baru di batch HOME memakai toleransi HOME — expiry &
            # deadline settlement page dinilai dari run.tolerance/home.tolerance,
            # jadi toleransi run hari ini akan membuat kontrak window baris
            # susulan menyimpang dari batch asalnya.
            run = MatchRun.objects.create(
                relation=relation,
                tolerance=(home.tolerance if home.tolerance_id else tolerance),
                batch=home, created_by=user,
            )
        note = f"Susulan via run {batch.recon_date}"
        for r in rs:
            r.run = run
            r.reason_detail = f"{r.reason_detail} ({note})" if r.reason_detail else note
        MatchResult.objects.bulk_create(rs, batch_size=2000)
        homes[home_pk] = home
    if retro:
        txs = Transaction.objects.filter(id__in=retro).select_related("source_type")
        per_home = {}
        for t in txs:
            home = retro[t.id]
            per_home.setdefault(home.pk, (home, []))[1].append(t)
        for home_pk, (home, rows) in per_home.items():
            _add_retro_gross(home, rows)
            homes[home_pk] = home
    return set(homes.values())


def _apply_late_settlements(batch, late_pairs):
    """Flip hasil no_money LAMA di batch asalnya: bucket ikut aturan skor normal
    (cocok / perlu_tinjau name_partial), right diisi baris uang, reason asal disimpan
    di reason_detail, ditandai resolved_by_batch=batch (untuk revert saat hapus)."""
    resolved = []
    for prior, new in late_pairs:
        if prior.bucket != MatchResult.Bucket.TIDAK:
            continue  # defensif: hasil sudah dioverride manual
        prior.bucket = new.bucket
        prior.right = new.right
        prior.score = new.score
        prior.reason_detail = (
            f"Settle terlambat oleh run {batch.recon_date} — asal: {prior.reason_code}"
            + (f"; {new.reason_detail}" if new.reason_detail else "")
        )
        prior.reason_code = "late_settlement"
        prior.resolved_by_batch = batch
        prior.save(update_fields=["bucket", "right", "score", "reason_code",
                                  "reason_detail", "resolved_by_batch"])
        resolved.append(prior)
    return resolved


def refresh_batch_summary(batch):
    """Hitung ulang bagian TURUNAN summary batch dari MatchResult tersimpan:
    bucket per run, money_matched/money/selisih, buckets total. Field potret saat
    run (panel, money_gross, warnings, skipped) tidak disentuh. Idempoten — dipakai
    saat hasil batch ini di-flip late settlement maupun di-revert."""
    batch.refresh_from_db(fields=["summary"])  # jangan timpa increment gross susulan
    runs = list(batch.runs.all())
    buckets = {"cocok": 0, "perlu_tinjau": 0, "tidak_cocok": 0}
    for r in runs:
        c = Counter(r.results.values_list("bucket", flat=True))
        s = dict(r.summary or {})
        for k in buckets:
            s[k] = c.get(k, 0)
            buckets[k] += s[k]
        r.summary = s
        r.save(update_fields=["summary"])
    dp_m, wd_m = _matched_money(runs)
    s = dict(batch.summary or {})
    for flow, matched in (("dp", dp_m), ("wd", wd_m)):
        f = dict(s.get(flow) or {})
        f["money_matched"] = matched
        f["money"] = matched  # key lama (backward-compat) = versi MATCHED
        f["selisih"] = float(f.get("panel") or 0) - matched
        s[flow] = f
    s["buckets"] = buckets
    batch.summary = s
    batch.save(update_fields=["summary"])


def _late_settlement_summary(resolved):
    """Ringkasan settle terlambat per arah uang (DP/WD) dari hasil yang di-flip."""
    out = {"dp": {"count": 0, "amount": 0.0}, "wd": {"count": 0, "amount": 0.0}}
    for r in resolved:
        md = float(r.right.money_delta)
        flow = "dp" if md > 0 else "wd"
        out[flow]["count"] += 1
        out[flow]["amount"] += abs(md)
    return out


def revert_late_settlements(batch):
    """Sebelum batch dihapus: batalkan semua efek carry-over yang dilakukan batch ini.
    Flip dikembalikan ke tidak_cocok/no_money (baris kreditnya aktif lagi — kandidat
    settlement berikutnya), baris kadaluarsa diaktifkan lagi, dan summary batch asal
    dihitung ulang. Return jumlah flip yang dibatalkan.
    Catatan: delete programatik (shell/queryset.delete) MELEWATI helper ini — jalur
    resmi penghapusan adalah view hapus batch di web."""
    results = list(
        MatchResult.objects.filter(resolved_by_batch=batch).select_related("run", "run__batch")
    )
    homes = {}
    for r in results:
        r.bucket = MatchResult.Bucket.TIDAK
        r.right = None
        r.score = 0
        r.reason_code = "no_money"
        r.reason_detail = NO_MONEY_DETAIL
        r.resolved_by_batch = None
        r.save(update_fields=["bucket", "right", "score", "reason_code",
                              "reason_detail", "resolved_by_batch"])
        if r.left_id:
            Transaction.objects.filter(pk=r.left_id).update(consumed_by_batch=None)
        if r.run.batch_id:
            homes[r.run.batch_id] = r.run.batch
    # Baris kadaluarsa yang batch ini konsumsi ke batch asal → aktif lagi.
    # Filter consumed_by_batch_id=home membuatnya no-op bila batch asal sudah dihapus.
    for e in ((batch.summary or {}).get("late_settlement") or {}).get("expired", []):
        Transaction.objects.filter(pk=e["tx"], consumed_by_batch_id=e["home"])\
            .update(consumed_by_batch=None)
    for home in homes.values():
        refresh_batch_summary(home)
    return len(results)


def classify_unmatched_money(t, recon_date, window, panel_tickets, operator_names):
    """Kategori uang tanpa pasangan (satu sumber kebenaran — dipakai engine & web):
    a = histori (di luar jangkauan window pasangan), b = gateway ber-ticket yang
    tak dikenal panel, c = pindah dana internal (lawan = rekening operator),
    d = dalam periode tanpa penjelasan → layak diperiksa manusia."""
    d = t.occurred_at.date() if t.occurred_at else None
    if d is None or d < recon_date - timedelta(days=window):
        return "a"
    if t.source_type.key == "gateway" and t.ticket_no and t.ticket_no not in panel_tickets:
        return "b"
    cp = _norm_owner(t.counterparty)
    if len(cp) >= 5:  # nama terlalu pendek rawan nyangkut di nama file
        for name in operator_names:
            if name and fuzz.partial_ratio(cp, name) >= 85:
                return "c"
    return "d"


def _operator_names(toko):
    """Nama pemilik rekening operator — diambil dari nama file upload bank toko.
    DEDUP + terurut: file harian bertahun-tahun memuat nama sama ratusan kali;
    classify_unmatched_money mem-fuzz tiap nama per baris uang — duplikat =
    kerja fuzz berulang tanpa menambah informasi (any-match tak berubah)."""
    from sources.models import Upload  # impor lokal: hindari siklus

    return sorted({
        _norm_owner(n)
        for n in Upload.objects.filter(toko=toko, source_type__key="bank")
        .values_list("original_name", flat=True)
    } - {""})


# Margin histori set ticket panel utk kategori b: uang yang bisa berkategori b
# selalu bertanggal >= recon_date - window (lebih tua = kategori a duluan), dan
# ticket gateway menunjuk transaksi panel yang berdekatan waktunya. Ticket panel
# lebih tua dari window+30 hari tak mungkin relevan; margin 30 hari ekstra
# menoleransi file panel telat/susulan. Tanpa batas ini SEMUA ticket sejarah
# toko dimuat per batch (auto-split 30 tanggal = 30× muat penuh).
_TICKET_HISTORY_MARGIN = 30


def recent_panel_tickets(toko, recon_date, window):
    """Set ticket panel toko utk cek kategori b (classify_unmatched_money),
    dibatasi occurred_at >= recon_date - (window + margin).
    recon_date None (batch legacy) → tanpa batas, perilaku lama."""
    qs = Transaction.objects.filter(
        toko=toko, source_type__key="panel"
    ).exclude(ticket_no="")
    if recon_date:
        lo = recon_date - timedelta(days=window + _TICKET_HISTORY_MARGIN)
        qs = qs.filter(occurred_at__gte=_day_start(lo))
    return set(qs.values_list("ticket_no", flat=True))


def _bracket_overlap_warning(runs):
    """Peringatan bila cocok Panel↔Bracket sangat rendah PADAHAL kedua sisi ada data —
    indikasi file Panel & Bracket beda periode / tidak sepasang (bukan mengubah join)."""
    for r in runs:
        if r.relation != MatchRun.Relation.PANEL_BRACKET:
            continue
        s = r.summary or {}
        left, right = s.get("left", 0), s.get("right", 0)
        cocok = s.get("cocok", 0)
        if left and right:  # kedua sisi punya baris
            # Pakai sisi TERBESAR sebagai penyebut: file beda periode membuat salah
            # satu sisi (mis. bracket setelah filter tanggal) menyusut jadi ~0 baris,
            # sehingga cocok jadi porsi kecil dari sisi yang penuh.
            denom = max(left, right)
            if (cocok / denom) < 0.10:
                return (
                    f"Panel↔Bracket cocok sangat rendah ({cocok} dari {denom}) — "
                    "kemungkinan file Panel & Bracket beda periode/tidak sepasang. "
                    "Cek tanggal file."
                )
    return None


def _panel_bracket_total_warning(toko, date_from, date_to, include):
    """Cross-check AGREGAT Panel vs Bracket per arah (untuk toko tanpa join ticket,
    mis. COR): bila total DP/WD beda > 2% padahal kedua sisi ada, beri peringatan."""
    if not _inc(include, "bracket"):
        return None
    base = _date_filter(_active(_toko_filter(
        Transaction.objects.filter(is_duplicate=False), toko)), date_from, date_to)

    def tot(key, jenis):
        return float(base.filter(source_type__key=key, jenis=jenis)
                     .aggregate(x=Sum("amount"))["x"] or 0)

    for flow, jenis in (("DP", "depo"), ("WD", "wd")):
        p, b = tot("panel", jenis), tot("bracket", jenis)
        if p > 0 and b > 0 and abs(p - b) / max(p, b) > 0.02:
            return (f"Panel↔Bracket {flow} total beda: Panel {p:,.0f} vs "
                    f"Bracket {b:,.0f} (selisih {abs(p - b):,.0f}). Cek kelengkapan file.")
    return None


def _aggregate_batch(toko, date_from, date_to, runs, skipped, include=None, exclude_tx_ids=None):
    tx = _date_filter(_active(_toko_filter(Transaction.objects.filter(is_duplicate=False), toko)), date_from, date_to)
    if exclude_tx_ids:
        # Baris carry-over: nilainya sudah tercatat di total batch ASALnya —
        # jangan menggelembungkan total batch ini.
        tx = tx.exclude(id__in=exclude_tx_ids)

    def total(qs, field):
        return float(qs.aggregate(x=Sum(field))["x"] or 0)

    panel = tx.filter(source_type__key="panel")
    # include: Panel — buang sisi yang tidak dicentang dari total.
    if include is not None:
        if not include.get("panel_dp", True):
            panel = panel.exclude(jenis="depo")
        if not include.get("panel_wd", True):
            panel = panel.exclude(jenis="wd")
    # Baris fee BCA ('admin') dikecualikan dari uang WD (bukan WD nyata).
    # Hanya sumber uang yang dicentang ikut total gross.
    money = tx.filter(source_type__key__in=_included_money_sources(include)).exclude(jenis="admin")
    dp_panel = total(panel.filter(jenis="depo"), "amount")
    dp_gross = total(money.filter(money_delta__gt=0), "money_delta")
    wd_panel = total(panel.filter(jenis="wd"), "amount")
    wd_gross = abs(total(money.filter(money_delta__lt=0), "money_delta"))

    dp_matched, wd_matched = _matched_money(runs)

    buckets = {"cocok": 0, "perlu_tinjau": 0, "tidak_cocok": 0}
    for r in runs:
        for k in buckets:
            buckets[k] += (r.summary or {}).get(k, 0)

    warnings = []
    w = _bracket_overlap_warning(runs)
    if w:
        warnings.append(w)
    w2 = _panel_bracket_total_warning(toko, date_from, date_to, include)
    if w2:
        warnings.append(w2)

    return {
        # money_matched = uang yang berpasangan ke Panel; selisih = panel - matched.
        # 'money'/'selisih' dipertahankan (backward-compat) = versi MATCHED.
        "dp": {
            "panel": dp_panel, "money_gross": dp_gross, "money_matched": dp_matched,
            "money": dp_matched, "selisih": dp_panel - dp_matched,
        },
        "wd": {
            "panel": wd_panel, "money_gross": wd_gross, "money_matched": wd_matched,
            "money": wd_matched, "selisih": wd_panel - wd_matched,
        },
        "buckets": buckets,
        "warnings": warnings,
        "relations": [r.relation for r in runs],
        "skipped": skipped,
    }


def _inc(include, key):
    """Sumber ikut run? include=None → semua ikut (perilaku lama). Selain itu: cek toggle."""
    return include is None or include.get(key, True)


def _consume_scope(toko, date_from, date_to, include):
    """Transaksi AKTIF dalam lingkup toko+tanggal yang HANYA dari sumber yang diikutkan.
    Ini yang akan dikunci ke batch setelah sukses (tidak menyentuh sumber tak dicentang)."""
    qs = _date_filter(_active(_toko_filter(Transaction.objects.filter(is_duplicate=False), toko)), date_from, date_to)
    panel = Q()
    if _inc(include, "panel_dp"):
        panel |= Q(source_type__key="panel", jenis="depo")
    if _inc(include, "panel_wd"):
        panel |= Q(source_type__key="panel", jenis="wd")
    cond = panel
    if _inc(include, "bracket"):
        cond |= Q(source_type__key="bracket")
    money_keys = _included_money_sources(include)
    if money_keys:
        cond |= Q(source_type__key__in=money_keys)
    if not cond:
        return Transaction.objects.none()
    return qs.filter(cond)


@db_tx.atomic
def run_batch(toko, tolerance=None, date_from=None, date_to=None, user=None, include=None,
              recon_date=None, consume_floor=None):
    """Atomic: kegagalan di tengah run me-rollback SEMUANYA (termasuk baris batch),
    sehingga tanggal harian tidak terblokir constraint unik oleh batch yatim."""
    # Serialisasi PER TOKO: dua run konkuren (meski beda tanggal) membaca pool
    # aktif yang sama → MatchResult ganda / summary dobel. select_for_update
    # baris Toko menahan run kedua sampai run pertama commit. (No-op di sqlite
    # dev — efektif di Postgres/production.)
    from sources.models import Toko  # impor lokal: hindari siklus (pola _operator_names)

    if toko is not None and toko.pk is not None:
        toko = Toko.objects.select_for_update().get(pk=toko.pk)
    tolerance = tolerance or ToleranceProfile.objects.get(name="Default")
    if recon_date and ReconBatch.objects.filter(toko=toko, recon_date=recon_date).exists():
        raise ValueError(f"Sudah ada batch untuk {toko} tanggal {recon_date}.")
    comp = check_completeness(toko, date_from, date_to)
    batch = ReconBatch.objects.create(
        toko=toko, tolerance=tolerance, date_from=date_from, date_to=date_to,
        created_by=user, completeness=comp, recon_date=recon_date,
    )
    carried = _carried_results(toko) if recon_date else {}
    # W6-2: window MATCHING baris carried = window batch ASALnya (kontrak T+n
    # yang dijanjikan saat baris lahir; sama dgn basis expiry W1-4 dan deadline
    # web/settlement.py). Tanpa ini carried Longgar di run Ketat menolak uang
    # sah D+2/D+3, dan carried Ketat di run Longgar settle melewati deadline.
    carried_windows = {
        left_id: (r.run.tolerance.date_window_days if r.run.tolerance_id
                  else tolerance.date_window_days)
        for left_id, r in carried.items()
    }
    relations, skipped = [], []
    # PANEL_BRACKET hanya jika bracket ADA, dicentang, DAN ada panel ber-ticket
    # DALAM SCOPE tanggal run (panel tanpa ticket—mis. COR—tak bisa di-join baris
    # demi baris ke bracket). Baris CARRIED dikecualikan: run_batches_auto
    # melebarkan date_from ke baris carried, dan PANEL_BRACKET toh mengecualikan
    # carried dari pencocokan — ticket lama itu cuma akan menghasilkan noise no_panel.
    panel_has_ticket = _date_filter(
        _active(_toko_filter(Transaction.objects.filter(
            source_type__key="panel", is_duplicate=False), toko)),
        date_from, date_to,
    ).exclude(ticket_no="").exclude(id__in=set(carried)).exists()
    if comp["bracket"] and _inc(include, "bracket") and panel_has_ticket:
        relations.append(MatchRun.Relation.PANEL_BRACKET)
    else:
        skipped.append(MatchRun.Relation.PANEL_BRACKET.value)
    # PANEL_BANK jalan bila ada uang (bank/gateway) yang ADA & dicentang, ATAU —
    # jaring senyap — uang DIINGINKAN (dicentang) tapi kosong di scope sementara
    # ada panel DP/WD: matcher menghasilkan no_money per baris panel sehingga
    # deposit/wd tak lenyap senyap (selisih batch selalu punya baris penjelas).
    money_present = (comp["bank"] and _inc(include, "bank")) or (comp["gateway"] and _inc(include, "gateway"))
    money_wanted = _inc(include, "bank") or _inc(include, "gateway")
    panel_present = (comp["panel_dp"] and _inc(include, "panel_dp")) or (comp["panel_wd"] and _inc(include, "panel_wd"))
    if money_present or (money_wanted and panel_present):
        relations.append(MatchRun.Relation.PANEL_BANK)
    else:
        skipped.append(MatchRun.Relation.PANEL_BANK.value)

    retro = (
        _retro_homes(toko, recon_date, date_from, date_to, include, exclude_ids=set(carried))
        if recon_date else {}
    )
    # W7-1: paritas carried utk baris SUSULAN — window per baris = toleransi
    # batch ASALnya (kontrak T+n hari baris itu lahir), bukan profil run hari
    # ini. Masuk ke peta yang sama yang mengalir ke matcher (kandidat), dan
    # dipakai lagi di keputusan menunggu/kadaluarsa di bawah. Id uang ikut
    # masuk peta tapi tak berefek: kandidat() hanya melihat id sisi kiri.
    carried_windows.update({
        tx_id: (home.tolerance.date_window_days if home.tolerance_id
                else tolerance.date_window_days)
        for tx_id, home in retro.items()
    })
    runs = [
        run_match(rel, tolerance, date_from, date_to, user=user, toko=toko, batch=batch, include=include,
                  carried=carried, retro=retro, carried_windows=carried_windows)
        for rel in relations
    ]
    late_pairs = [pair for r in runs for pair in getattr(r, "late_pairs", [])]
    resolved = _apply_late_settlements(batch, late_pairs)
    retro_results = [r for run in runs for r in getattr(run, "retro_results", [])]
    retro_homes = _writeback_retro(batch, retro, retro_results, tolerance, user)
    # SEMUA uang yang terpakai pasangan hari ini (termasuk yang hasilnya milik
    # batch lain via flip/susulan) — dipakai konsumsi (B1/B2 + consume_floor).
    used_rights = set()
    for r_ in runs:
        used_rights |= getattr(r_, "used_right_ids", set())
    # W1-2 — celah lo-widening: date_from bisa dilebarkan run_batches_auto ke
    # tanggal baris carried terawal. Uang TAK BERPASANGAN bertanggal < consume_floor
    # (= tanggal panel batch ini) tidak boleh ikut dikonsumsi tanpa MatchResult —
    # biarkan aktif menunggu panel tanggalnya diupload. Uang di celah yang
    # BERPASANGAN (settle carried / susulan) tetap dikonsumsi seperti biasa.
    consume_floor = _as_date(consume_floor)
    pre_floor_orphans = set()
    if consume_floor:
        pre_floor_orphans = set(
            _consume_scope(toko, date_from, date_to, include)
            .filter(source_type__key__in=_included_money_sources(include),
                    occurred_at__lt=_day_start(consume_floor))
            .exclude(id__in=used_rights)
            .exclude(id__in=set(retro))
            .values_list("id", flat=True)
        )
    summary = _aggregate_batch(toko, date_from, date_to, runs, skipped, include=include,
                               exclude_tx_ids=set(carried) | set(retro) | pre_floor_orphans)
    if recon_date:
        summary["late_settlement"] = _late_settlement_summary(resolved)
        summary["retro"] = {"count": len(retro)}
    # Segarkan summary batch ASAL yang tersentuh flip / baris susulan.
    for home in {b.pk: b for b in [r.run.batch for r in resolved] + list(retro_homes)}.values():
        refresh_batch_summary(home)
    # KONSUMSI-SAAT-SUKSES: langkah TERAKHIR. Bila ada exception di atas, tidak
    # tercapai → transaksi tetap aktif (gagal = tidak dibersihkan). Hanya sumber
    # yang diikutkan yang dikunci; sumber tak dicentang tetap tersedia lain kali.
    if recon_date:
        # Rekonsiliasi harian: baris kredit no_money yang masih dalam window
        # TIDAK dikonsumsi ("menunggu settlement") — mutasinya mungkin baru
        # muncul di file hari berikutnya.
        window = tolerance.date_window_days
        resolved_ids = {r.left_id for r in resolved}
        by_home, expired = {}, []
        # 1) Carried yang settle → konsumsi ke batch ASALnya (baris milik hari itu).
        for r in resolved:
            by_home.setdefault(r.run.batch_id, []).append(r.left_id)
        # 2) Carried tak settle & sudah lewat window → kadaluarsa: konsumsi diam-diam
        #    ke batch asal (tidak_cocok-nya sudah tercatat di sana). Jejak {tx, home}
        #    disimpan agar bisa dipulihkan bila batch ini dihapus.
        #    W1-4: window per baris = toleransi batch ASALnya (prior.run.tolerance)
        #    — profil hari ini (Ketat/Longgar) tidak mengubah deadline baris lama,
        #    konsisten dgn deadline yang ditampilkan web/settlement.py.
        for left_id, prior in carried.items():
            if left_id in resolved_ids:
                continue
            d = prior.left.occurred_at.date() if prior.left.occurred_at else None
            w = (prior.run.tolerance.date_window_days
                 if prior.run.tolerance_id else window)
            if not _can_still_settle(d, recon_date, w):
                by_home.setdefault(prior.run.batch_id, []).append(left_id)
                expired.append({"tx": left_id, "home": prior.run.batch_id})
        # 2b) Baris SUSULAN → konsumsi ke batch tanggal asalnya, KECUALI panel
        #     tanpa pasangan uang yang masih dalam window (tetap aktif menunggu —
        #     hasil no_money-nya sudah tertulis di batch asal, jadi run berikutnya
        #     memperlakukannya sebagai carried biasa).
        retro_waiting = set()
        retro_orphans = {}  # home_pk -> (home, [uang susulan tanpa pasangan])
        if retro:
            retro_matched_money = {
                r.left_id for r in retro_results
                if r.right_id is not None and r.run.relation != MatchRun.Relation.PANEL_BRACKET
            }
            for t in Transaction.objects.filter(id__in=retro).select_related("source_type"):
                d = t.occurred_at.date() if t.occurred_at else None
                # W7-1: deadline menunggu/kadaluarsa baris susulan = window
                # batch ASALnya (paritas carried W1-4), bukan profil hari ini.
                if (
                    t.source_type.key == "panel"
                    and t.id not in retro_matched_money
                    and _can_still_settle(d, recon_date, carried_windows.get(t.id, window))
                ):
                    retro_waiting.add(t.id)
                else:
                    home = retro[t.id]
                    by_home.setdefault(home.pk, []).append(t.id)
                    # W1-5: uang susulan TAK berpasangan — dikonsumsi ke home
                    # tapi dulu tanpa MatchResult → hilang dari antrean tinjau.
                    if (t.source_type.key in MONEY_SOURCES and t.jenis != "admin"
                            and t.id not in used_rights):
                        retro_orphans.setdefault(home.pk, (home, []))[1].append(t)
        for home_id, ids in by_home.items():
            Transaction.objects.filter(id__in=ids).update(consumed_by_batch_id=home_id)
        # W1-5 — hasil no_panel utk uang susulan tanpa pasangan, di run PANEL_BANK
        # batch HOME (pola no_panel B2). Home tanpa run PANEL_BANK: tak ada tempat
        # menaruh hasil → dicatat di summary home sebagai retro_unmatched.
        for home, rows in retro_orphans.values():
            pb_home = MatchRun.objects.filter(
                batch=home, relation=MatchRun.Relation.PANEL_BANK
            ).order_by("id").first()
            if pb_home is None:
                home.refresh_from_db(fields=["summary"])
                s_home = dict(home.summary or {})
                s_home["retro_unmatched"] = s_home.get("retro_unmatched", 0) + len(rows)
                home.summary = s_home
                home.save(update_fields=["summary"])
                continue
            # W6-6: bucket TIDAK — konsisten dgn no_panel klasifikasi b/d (B2),
            # supaya semua baris no_panel berkumpul di tab 'tidak_ada_panel'
            # (bucket TIDAK + left null), tidak terpecah ke tab Tinjau.
            MatchResult.objects.bulk_create([
                MatchResult(
                    run=pb_home, bucket=MatchResult.Bucket.TIDAK, left=None, right=t,
                    score=0, reason_code="no_panel",
                    reason_detail=(
                        f"Uang susulan tanpa catatan panel (via run {recon_date})"
                    ),
                ) for t in rows
            ], batch_size=2000)
            refresh_batch_summary(home)
        summary["late_settlement"]["expired"] = expired
        # 3) Yang masih menunggu settlement tetap AKTIF: carried dalam window yang
        #    belum settle + no_money BARU batch ini yang dalam window + panel
        #    susulan yang masih dalam window.
        # __date__gt=X ⟺ __gte=midnight(X+1) — bentuk sargable (lihat _date_range_q).
        new_carry = MatchResult.objects.filter(
            run__batch=batch, bucket=MatchResult.Bucket.TIDAK, reason_code="no_money",
            left__isnull=False,
            left__occurred_at__gte=_day_start(
                recon_date - timedelta(days=window) + timedelta(days=1)
            ),
        ).values_list("left_id", flat=True)
        expired_ids = {e["tx"] for e in expired}
        still_waiting = (
            (set(carried) - resolved_ids - expired_ids) | set(new_carry) | retro_waiting
        )
        # B1 — carry-over sisi UANG: baris uang bertanggal > recon_date yang tidak
        # menjadi pasangan hasil mana pun hari ini tetap AKTIF (milik run tanggalnya
        # sendiri besok). Uang lintas-hari yang BERPASANGAN tetap dikonsumsi.
        money_keys = _included_money_sources(include)
        cross_money = set(
            _consume_scope(toko, date_from, date_to, include)
            .filter(source_type__key__in=money_keys,
                    occurred_at__gte=_day_start(recon_date + timedelta(days=1)))
            .exclude(id__in=used_rights)
            .values_list("id", flat=True)
        )
        _consume_scope(toko, date_from, date_to, include)\
            .exclude(id__in=still_waiting | cross_money | pre_floor_orphans)\
            .update(consumed_by_batch=batch)

        # B2 — uang tanpa pasangan: klasifikasi a/b/c/d; b & d dicatat sebagai
        # hasil no_panel (bisa ditinjau/di-flag), a & c cukup dihitung.
        pb_run = next(
            (r_ for r_ in runs if r_.relation == MatchRun.Relation.PANEL_BANK), None
        )
        if pb_run is not None:
            panel_ticket_set = recent_panel_tickets(toko, recon_date, window)
            ops = _operator_names(toko)
            stats = {k: {"n": 0, "dp": 0.0, "wd": 0.0} for k in "abcd"}
            new_results = []
            um_qs = (
                Transaction.objects.filter(
                    consumed_by_batch=batch, source_type__key__in=money_keys
                )
                .exclude(jenis="admin").exclude(id__in=used_rights)
                .select_related("source_type")
            )
            for t in um_qs:
                k = classify_unmatched_money(t, recon_date, window, panel_ticket_set, ops)
                st = stats[k]
                st["n"] += 1
                md = float(t.money_delta)
                if md > 0:
                    st["dp"] += md
                else:
                    st["wd"] += -md
                if k in ("b", "d"):
                    new_results.append(MatchResult(
                        run=pb_run, bucket=MatchResult.Bucket.TIDAK, left=None, right=t,
                        score=0, reason_code="no_panel",
                        reason_detail=(
                            "Ticket gateway tak dikenal panel" if k == "b"
                            else "Uang dalam periode tanpa catatan panel"
                        ),
                    ))
            if new_results:
                MatchResult.objects.bulk_create(new_results, batch_size=2000)
                s_ = dict(pb_run.summary or {})
                s_["tidak_cocok"] = s_.get("tidak_cocok", 0) + len(new_results)
                pb_run.summary = s_
                pb_run.save(update_fields=["summary"])
                summary["buckets"]["tidak_cocok"] += len(new_results)
            summary["unmatched_money"] = stats
    else:
        _consume_scope(toko, date_from, date_to, include)\
            .exclude(id__in=pre_floor_orphans).update(consumed_by_batch=batch)
    batch.summary = summary
    batch.save(update_fields=["summary"])
    return batch


def _as_date(v):
    """str ISO / date / None → date | None (verifikasi butuh aritmetika timedelta)."""
    if v is None or v == "":
        return None
    return date.fromisoformat(v) if isinstance(v, str) else v


def _panel_dates(toko, date_from=None, date_to=None, include=None):
    """Tanggal-panel AKTIF terurut MENAIK dalam scope & include — jangkar auto-split.
    Hanya baris panel DP/WD yang diikutkan (hormati include.panel_dp/panel_wd)."""
    qs = Transaction.objects.filter(
        source_type__key="panel", is_duplicate=False, jenis__in=["depo", "wd"]
    )
    if not _inc(include, "panel_dp"):
        qs = qs.exclude(jenis="depo")
    if not _inc(include, "panel_wd"):
        qs = qs.exclude(jenis="wd")
    qs = _date_filter(_active(_toko_filter(qs, toko)), date_from, date_to)
    return sorted({dt.date() for dt in qs.values_list("occurred_at", flat=True) if dt})


def verify_panel_anchor(toko, date_from, date_to, include, window):
    """Verifikasi jangkar tanggal: uang/bracket AKTIF yang tanggalnya berada DALAM
    rentang tanggal panel harus 'tertutup' minimal satu tanggal panel dalam window.
    Return list pelanggaran (kosong = lolos). Basis PER-TANGGAL; admin/fee dikecualikan.

    PENTING: hanya memeriksa tanggal DALAM rentang panel. Uang di LUAR rentang (mis.
    statement bank sebulan penuh sedang panel cuma sebagian tanggal) BUKAN pelanggaran
    — cuma belum direkonsiliasi, dibiarkan menunggu sampai panel tanggalnya diupload.
    Tanpa panel sama sekali → tak ada yang direkon → tak ada pelanggaran.

    'Tertutup' — money (bank/gateway): ∃ panel p dengan p <= m <= p+window (searah
    engine: uang >= panel). Bracket: ∃ panel p dengan |m-p| <= window."""
    pdates = sorted(_panel_dates(toko, date_from, date_to, include))
    if not pdates:
        return []
    pset = set(pdates)
    # Rentang relevan: uang harus >= panel (searah), jadi [panel_awal .. panel_akhir+window];
    # bracket boleh mendahului panel, jadi [panel_awal-window .. panel_akhir+window].
    lo_m, hi_m = pdates[0], pdates[-1] + timedelta(days=window)
    lo_b, hi_b = pdates[0] - timedelta(days=window), pdates[-1] + timedelta(days=window)
    base = _date_filter(
        _active(_toko_filter(Transaction.objects.filter(is_duplicate=False), toko)),
        date_from, date_to,
    )
    violations = []

    def covered_money(m):
        return any(p <= m <= p + timedelta(days=window) for p in pset)

    def covered_bracket(m):
        return any(abs((m - p).days) <= window for p in pset)

    money_keys = _included_money_sources(include)
    if money_keys:
        agg = defaultdict(lambda: [0.0, 0])  # tanggal -> [gross_abs, n]
        rows = (
            base.filter(source_type__key__in=money_keys)
            .exclude(jenis="admin")
            .values_list("occurred_at", "money_delta")
        )
        for dt, md in rows:
            if dt:
                a = agg[dt.date()]
                a[0] += abs(float(md or 0))
                a[1] += 1
        for m, (gross, n) in agg.items():
            if lo_m <= m <= hi_m and not covered_money(m):
                violations.append({"date": m, "source": "uang", "amount_gross": gross, "n": n})

    if _inc(include, "bracket"):
        bdates = defaultdict(int)
        for dt in (
            base.filter(source_type__key="bracket")
            .exclude(ticket_no="")
            .values_list("occurred_at", flat=True)
        ):
            if dt:
                bdates[dt.date()] += 1
        for m, n in bdates.items():
            if lo_b <= m <= hi_b and not covered_bracket(m):
                violations.append({"date": m, "source": "bracket", "amount_gross": 0.0, "n": n})

    return sorted(violations, key=lambda v: (v["date"], v["source"]))


def run_batches_auto(toko, tolerance=None, date_from=None, date_to=None, user=None, include=None):
    """Orkestrator rekonsiliasi otomatis per tanggal: satu ReconBatch per tanggal-panel
    (panel = jangkar). Pre-flight `verify_panel_anchor` memblokir bila ada uang/bracket
    DALAM rentang panel tanpa panel penutup — TAK ada batch dibuat. Loop MENAIK memakai
    carry-over bawaan: `run_batch(date_from=lo, date_to=D, recon_date=D)`.

    `lo` = tanggal panel terawal (atau baris carried yang lebih awal, agar carry-over
    lintas-hari tetap masuk scope) — BUKAN None. Ini penting: statement bank sering
    diekspor sebulan penuh sedang panel cuma sebagian tanggal; uang SEBELUM `lo` tak
    boleh ikut dikonsumsi ke batch pertama (biar tetap menunggu sampai panelnya ada).
    `date_to=D` cegah tanggal masa depan bocor. Tanggal ber-batch dilewati (tidak raise)."""
    tolerance = tolerance or ToleranceProfile.objects.get(name="Default")
    date_from = _as_date(date_from)
    date_to = _as_date(date_to)
    window = tolerance.date_window_days

    panel_dates = _panel_dates(toko, date_from, date_to, include)
    violations = verify_panel_anchor(toko, date_from, date_to, include, window)
    if violations:
        return {
            "ok": False, "batches": [], "dates_processed": [],
            "skipped_existing": [], "violations": violations,
            "errors": [], "panel_dates": panel_dates,
        }

    # Batas bawah scope: tanggal panel terawal, diperlebar ke tanggal baris carried
    # bila lebih awal (carry-over lintas-hari harus tetap masuk scope). Uang di bawah
    # batas ini tidak ikut dikonsumsi — dibiarkan menunggu panel tanggalnya.
    lo = min(panel_dates) if panel_dates else None
    if lo is not None:
        carried_dates = [
            r.left.occurred_at.date()
            for r in _carried_results(toko).values()
            if r.left and r.left.occurred_at
        ]
        if carried_dates:
            lo = min(lo, min(carried_dates))

    # W6-1: floor konsumsi = tanggal panel terawal yang BELUM punya batch,
    # KONSTAN untuk semua batch di loop — dulu consume_floor=d (tanggal batch
    # masing-masing) sehingga uang orphan di hari TANPA panel di antara dua
    # tanggal panel baru (gap-day, mis. panel 27 & 30 + uang nyasar 28)
    # dilindungi selamanya: tak dikonsumsi, tanpa MatchResult, hilang dari
    # semua UI. Dengan floor konstan, gap-day antar jangkar baru dikonsumsi +
    # terklasifikasi no_panel oleh batch pertama yang menjangkaunya. Uang
    # SEBELUM jangkar baru terawal (termasuk celah setelah tanggal yang sudah
    # ber-batch — panelnya mungkin baru diupload besok) tetap menunggu.
    existing_by_date = {
        b.recon_date: b
        for b in ReconBatch.objects.filter(toko=toko, recon_date__in=panel_dates)
    }
    new_dates = [d for d in panel_dates if d not in existing_by_date]
    floor = min(new_dates) if new_dates else None

    batches, skipped_existing, errors = [], [], []
    for d in panel_dates:  # MENAIK — prasyarat kebenaran carry-over
        existing = existing_by_date.get(d)
        if existing:
            skipped_existing.append({"date": d, "batch_id": existing.id})
            continue
        try:
            # consume_floor=floor: uang tak berpasangan di celah [lo..floor) yang
            # panelnya belum diupload TIDAK dikonsumsi (tetap aktif menunggu
            # panel tanggalnya); gap-day >= floor dikonsumsi + terklasifikasi.
            batch = run_batch(
                toko, tolerance, date_from=lo, date_to=d,
                user=user, include=include, recon_date=d, consume_floor=floor,
            )
            batches.append(batch)
        except Exception as e:  # noqa: BLE001
            # STOP di tanggal gagal: tanggal berikutnya bisa mengonsumsi baris
            # milik tanggal ini (home batch-nya tak ada) — jangan lanjut.
            logger.exception("run_batches_auto gagal di %s tanggal %s", toko, d)
            errors.append({"date": d, "message": str(e)})
            break
    return {
        "ok": True, "batches": batches,
        "dates_processed": [b.recon_date for b in batches],
        "skipped_existing": skipped_existing, "violations": [],
        "errors": errors, "panel_dates": panel_dates,
    }
