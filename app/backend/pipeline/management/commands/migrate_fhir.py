import asyncio
import json

from django.core.management.base import BaseCommand, CommandError

from pipeline.services.extract import BulkExportExtractor, ExtractError
from pipeline.services.fhir_client import FhirError
from pipeline.services.transform import TransformEngine, TransformError
from pipeline.services.validate import ValidationEngine, ValidationError


class Command(BaseCommand):
    help = "FHIR migration pipeline: extract | transform | validate"

    def add_arguments(self, parser):
        parser.add_argument("step", choices=["extract", "transform", "validate"])
        parser.add_argument("--workers", type=int, help="extract: parallel downloads (default: EXPORT_WORKERS)")
        parser.add_argument("--rebuild", action="store_true", help="transform: clear the internal model first")
        parser.add_argument("--sample-rate", type=float, default=0.01, help="validate: share re-mapped and compared")
        parser.add_argument("--seed", type=int, help="validate: random seed for a reproducible sample")

    def handle(self, *args, **options):
        step = options["step"]
        try:
            if step == "validate":
                return self.validate(options)
            summary = self.extract(options) if step == "extract" else self.transform(options)
        except (ExtractError, FhirError, TransformError, ValidationError) as error:
            raise CommandError(str(error))
        self.stdout.write(json.dumps(summary, indent=2, default=str))

    def extract(self, options):
        return asyncio.run(BulkExportExtractor(workers=options["workers"]).run())

    def transform(self, options):
        return TransformEngine().run(rebuild=options["rebuild"])

    def validate(self, options):
        report = ValidationEngine(sample_rate=options["sample_rate"], seed=options["seed"]).run()
        for check in report.checks:
            mark = "PASS" if check["passed"] else "FAIL"
            detail = f"  ({check['detail']})" if check["detail"] else ""
            self.stdout.write(f"[{mark}] {check['name']}: expected {check['expected']}, got {check['actual']}{detail}")
        for mismatch in report.sample["mismatches"][:10]:
            self.stdout.write(f"       mismatch {mismatch['resource_type']}/{mismatch['source_id']} "
                              f"{mismatch['field']}: expected {mismatch['expected']!r}, got {mismatch['actual']!r}")
        self.stdout.write(f"\nReport {report.pk}: {'PASSED' if report.passed else 'FAILED'} "
                          f"(sample seed {report.sample['seed']})")
        self.stdout.write("Quarantined records must be signed off by the data owner before acceptance.")
        if not report.passed:
            raise CommandError("validation failed: investigate, fix and rebuild from the raw store")
