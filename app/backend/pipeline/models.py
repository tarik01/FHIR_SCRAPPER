from django.db import models


class ExportJob(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress"
        COMPLETED = "completed"
        FAILED = "failed"
        EXPIRED = "expired"

    job_id = models.CharField(max_length=128, primary_key=True)
    source_base_url = models.URLField()
    request_url = models.TextField()
    poll_url = models.TextField(blank=True)
    since = models.CharField(max_length=64, null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.IN_PROGRESS)
    transaction_time = models.CharField(max_length=64, null=True, blank=True)
    expected_counts = models.JSONField(null=True, blank=True)
    manifest_json = models.JSONField(null=True, blank=True)
    kicked_off_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    def __str__(self):
        return f"{self.job_id} ({self.status})"


class ExportFile(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        DONE = "done"
        FAILED = "failed"

    job = models.ForeignKey(ExportJob, on_delete=models.CASCADE, related_name="files")
    resource_type = models.CharField(max_length=32)
    url = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    http_status = models.PositiveIntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    line_count = models.PositiveIntegerField(null=True, blank=True)
    inserted_count = models.PositiveIntegerField(null=True, blank=True)
    duplicate_count = models.PositiveIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    downloaded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "url"], name="unique_file_per_job")]

    def __str__(self):
        return f"{self.resource_type} file {self.pk} ({self.status})"


class RawResource(models.Model):
    resource_type = models.CharField(max_length=32)
    source_id = models.CharField(max_length=128)
    version_id = models.CharField(max_length=128)
    last_updated = models.CharField(max_length=64, blank=True)
    payload = models.TextField()
    payload_sha256 = models.CharField(
        max_length=64,
        help_text="SHA-256 of the exact NDJSON line; detects changed content and serves as version_id "
        "when the source has no meta.versionId.",
    )
    file = models.ForeignKey(ExportFile, on_delete=models.PROTECT, related_name="resources")
    line_no = models.PositiveIntegerField(
        help_text="1-based line number inside the NDJSON file; with file, traces the row to its exact source line.",
    )
    extracted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["resource_type", "source_id", "version_id"], name="unique_resource_version"
            )
        ]
        indexes = [models.Index(fields=["resource_type", "source_id"])]

    def __str__(self):
        return f"{self.resource_type}/{self.source_id} v{self.version_id}"


class Quarantine(models.Model):
    raw_resource = models.ForeignKey(RawResource, on_delete=models.SET_NULL, null=True)
    resource_type = models.CharField(max_length=32)
    source_id = models.CharField(max_length=128)
    reason = models.CharField(max_length=255)
    mapping_version = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["resource_type", "source_id"], name="unique_quarantined_resource")
        ]

    def __str__(self):
        return f"{self.resource_type}/{self.source_id}: {self.reason}"


class RunReport(models.Model):
    job = models.ForeignKey(ExportJob, on_delete=models.SET_NULL, null=True, related_name="reports")
    created_at = models.DateTimeField(auto_now_add=True)
    passed = models.BooleanField()
    checks = models.JSONField(help_text="One entry per validation check: name, expected, actual, passed, detail.")
    totals = models.JSONField(help_text="Counts per type, quarantine reasons and duplicate candidates.")
    sample = models.JSONField(help_text="Sample size, seed and field-level mismatches found.")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Run report {self.pk} ({'passed' if self.passed else 'failed'})"
