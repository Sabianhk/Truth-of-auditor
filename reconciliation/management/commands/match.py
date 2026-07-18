from django.core.management.base import BaseCommand, CommandError

from reconciliation.engine import MATCHERS, run_match
from reconciliation.models import MatchRun, ToleranceProfile
from sources.models import Toko


class Command(BaseCommand):
    help = (
        "Jalankan pencocokan untuk satu relasi (panel_bracket / panel_bank). "
        "Carry-over & late settlement hanya berlaku pada batch harian dari web "
        "(run_batch dengan recon_date), bukan perintah ini."
    )

    def add_arguments(self, parser):
        parser.add_argument("relation", choices=[r.value for r in MatchRun.Relation])
        parser.add_argument("--from", dest="dfrom", default=None, help="YYYY-MM-DD (occurred_at)")
        parser.add_argument("--to", dest="dto", default=None, help="YYYY-MM-DD (occurred_at)")
        parser.add_argument("--tolerance", default="Default")
        parser.add_argument(
            "--toko", required=True,
            help="Nama/key/ID Toko (WAJIB — tanpa scope toko pencocokan lintas toko)",
        )

    def handle(self, *args, **o):
        if o["relation"] not in MATCHERS:
            raise CommandError(
                f"Relasi '{o['relation']}' belum didukung. Tersedia: "
                + ", ".join(str(k) for k in MATCHERS)
            )
        try:
            tol = ToleranceProfile.objects.get(name=o["tolerance"])
        except ToleranceProfile.DoesNotExist:
            raise CommandError(f"ToleranceProfile '{o['tolerance']}' tidak ada")
        ident = str(o["toko"]).strip()
        toko = (
            Toko.objects.filter(name__iexact=ident).first()
            or Toko.objects.filter(key__iexact=ident).first()
            or (Toko.objects.filter(pk=int(ident)).first() if ident.isdigit() else None)
        )
        if toko is None:
            raise CommandError(f"Toko '{ident}' tidak ditemukan (nama/key/ID)")
        run = run_match(o["relation"], tol, o["dfrom"], o["dto"], toko=toko)
        s = run.summary
        self.stdout.write(
            self.style.SUCCESS(
                f"MatchRun #{run.pk} [{o['relation']}]: cocok={s['cocok']} "
                f"perlu_tinjau={s['perlu_tinjau']} tidak_cocok={s['tidak_cocok']} "
                f"(left={s['left']} right={s['right']})"
            )
        )
