import os
import re

from django.core.management.base import BaseCommand, CommandError

from sources.services import PARSERS, ingest


def detect_flow(path):
    """Tebak arah file dari TOKEN utuh nama file (dipisah non-alfanumerik).

    Substring pernah salah tebak: "CROWDFUND" mengandung "wd" -> file DP
    dianggap WD. Prioritas wd > dp dipertahankan (perilaku lama)."""
    name = os.path.basename(path).lower()
    toks = re.split(r"[^a-z0-9]+", name)
    if "wd" in toks:
        return "wd"
    if "dp" in toks:
        return "dp"
    return ""


class Command(BaseCommand):
    help = "Parse & ingest satu file sumber menjadi Transaction kanonik."

    def add_arguments(self, parser):
        parser.add_argument("parser_key", help=f"salah satu: {', '.join(PARSERS)}")
        parser.add_argument("file_path")
        parser.add_argument("--flow", default=None, help="dp/wd (default: deteksi dari nama file)")
        parser.add_argument("--recon-date", default=None)

    def handle(self, *args, **opts):
        flow = opts["flow"] if opts["flow"] is not None else detect_flow(opts["file_path"])
        try:
            up, created, dup = ingest(
                opts["parser_key"],
                opts["file_path"],
                recon_date=opts["recon_date"],
                flow=flow,
            )
        except (ValueError, FileNotFoundError) as e:
            raise CommandError(str(e))
        self.stdout.write(
            self.style.SUCCESS(
                f"OK [{opts['parser_key']}/{flow or '-'}] {up.original_name}: "
                f"{created} dibuat, {dup} duplikat (Upload #{up.pk})"
            )
        )
