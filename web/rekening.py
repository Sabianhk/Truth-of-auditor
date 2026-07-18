"""Rincian Rekening — breakdown sisi UANG (bank/gateway) per rekening operator.

Kembaran Breakdown Bracket untuk mutasi bank nyata. Baris per rekening
(`source_label_full`, mis. "BCA a/n HENDI"): Deposit / Withdraw / Biaya Admin /
Net / Trx / Saldo Awal / Saldo Akhir / Selisih Kontrol. Saldo memakai
`balance_after` (saldo berjalan statement) via metode rantai-saldo yang sama
dengan breakdown FR (`_saldo_batas`) — kebal acak urutan. Sumber tanpa saldo
(gateway QRIS, BCA PDF) → saldo & selisih "—".
"""
from collections import Counter
from decimal import Decimal

from transactions.models import Transaction
from web.breakdown import _saldo_batas

NOL = Decimal("0")
MONEY_KEYS = ("bank", "gateway")


def rekening_breakdown(toko, tanggal):
    """{"accounts": [per rekening], "total": agregat, "count": jumlah baris}."""
    rows = (
        Transaction.objects.filter(
            toko=toko, source_type__key__in=MONEY_KEYS, occurred_at__date=tanggal
        )
        .select_related("source_type", "upload", "account", "upload__account")
        .order_by("occurred_at", "id")
    )

    # Kunci group = (label, rekening efektif) — BUKAN label saja: dua rekening
    # BCA pemilik sama punya label identik, digabung = satu rantai saldo palsu
    # (saldo awal/akhir/kontrol salah). Rekening efektif: account transaksi >
    # account upload > tanpa rekening (None).
    per = {}  # (label, account_id) → {"items": [...], "is_gateway": bool, ...}
    count = 0
    for t in rows:
        count += 1
        label = t.source_label_full
        acc = t.account if t.account_id else (
            t.upload.account if t.upload_id and t.upload.account_id else None
        )
        slot = per.setdefault((label, acc.pk if acc else None),
                              {"items": [], "is_gateway": False,
                               "label": label, "acc": acc})
        if t.source_type.key == "gateway":
            slot["is_gateway"] = True
        slug = "admin" if t.jenis == "admin" else ("deposit" if t.money_delta > 0 else "withdrawal")
        # bentuk tuple sama dgn breakdown FR agar _saldo_batas bisa dipakai ulang:
        # (jam, id, delta, balance, slug) — query sudah urut (occurred_at, id).
        slot["items"].append((t.occurred_at, t.id, t.money_delta or NOL, t.balance_after, slug))

    # Dua rekening berbagi label → bedakan tampilan dengan suffix nomor
    # rekening (label tunggal tetap polos, tampilan lama tak berubah).
    n_label = Counter(s["label"] for s in per.values())
    accounts = []
    for slot in per.values():
        label = slot["label"]
        acc = slot["acc"]
        if n_label[label] > 1 and acc is not None and acc.account_no:
            label = f"{label} — {acc.account_no}"
        items = slot["items"]
        deposit = withdraw = admin = mutasi = NOL
        trx = 0
        for _jam, _pk, delta, _bal, slug in items:
            mutasi += delta
            if slug == "admin":
                admin += delta
            elif delta > 0:
                deposit += delta
                trx += 1
            elif delta < 0:
                withdraw += delta
                trx += 1
        withdraw = abs(withdraw)
        saldo_awal, saldo_akhir = _saldo_batas(items)
        selisih = None
        if saldo_awal is not None and saldo_akhir is not None:
            selisih = saldo_akhir - (saldo_awal + mutasi)
        accounts.append({
            "label": label, "is_gateway": slot["is_gateway"],
            "deposit": deposit, "withdraw": withdraw, "admin": admin,
            "net": deposit - withdraw, "trx": trx, "mutasi": mutasi,
            "saldo_awal": saldo_awal, "saldo_akhir": saldo_akhir, "selisih": selisih,
        })

    accounts.sort(key=lambda a: (a["is_gateway"], a["label"]))

    total = {
        "deposit": NOL, "withdraw": NOL, "admin": NOL, "net": NOL, "trx": 0,
        "mutasi": NOL, "saldo_awal": None, "saldo_akhir": None, "selisih": None,
    }
    for a in accounts:
        for k in ("deposit", "withdraw", "admin", "net", "trx", "mutasi"):
            total[k] += a[k]
        for k in ("saldo_awal", "saldo_akhir", "selisih"):
            if a[k] is not None:
                total[k] = (total[k] or NOL) + a[k]
    return {"accounts": accounts, "total": total, "count": count}
