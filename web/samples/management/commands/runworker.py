from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from geyser_web.pipeline import get_storage
from samples.worker import RunLocalRunner, Worker


class Command(BaseCommand):
    help = ("Local pipeline trigger: run scripts/run_local.py for every new upload in raw_reads/. "
            "Offline stand-in for the S3 -> Lambda -> Step Functions trigger; not used in the cloud.")

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="process what is waiting, then exit")
        parser.add_argument("--interval", type=float, default=2.0, help="seconds between polls")

    def handle(self, *args, once=False, interval=2.0, **options):
        if settings.GEYSER_STORAGE_BACKEND != "local":
            raise CommandError("runworker needs STORAGE_BACKEND=local. In the cloud the S3 upload "
                               "triggers the pipeline Lambda, so no worker is needed.")
        root = settings.GEYSER_LOCAL_STORAGE_DIR
        runner = RunLocalRunner(root, runner=settings.GEYSER_RUNNER, image=settings.GEYSER_IMAGE,
                                reference=settings.GEYSER_REFERENCE_NAME)
        worker = Worker(get_storage(), runner, say=lambda msg: self.stdout.write(msg))
        self.stdout.write(f"[worker] watching {root}/raw_reads (runner={runner.runner}, image={runner.image}, "
                          f"reference={runner.reference})")
        if once:
            worker.recover()
            worker.run_once()
            return
        try:
            worker.serve_forever(interval)
        except KeyboardInterrupt:
            self.stdout.write("[worker] stopped")
