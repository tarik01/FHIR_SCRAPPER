import uuid

from django.db import models


class Patient(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_system = models.CharField(max_length=255)
    source_id = models.CharField(max_length=128)
    source_version = models.CharField(max_length=128)
    source_last_updated = models.DateTimeField(null=True)
    given_name = models.CharField(max_length=255, blank=True)
    family_name = models.CharField(max_length=255, blank=True)
    birth_date = models.CharField(max_length=10, blank=True)
    birth_date_precision = models.CharField(max_length=5, blank=True)
    gender = models.CharField(max_length=16, null=True)
    deceased = models.BooleanField(null=True)
    deceased_at = models.DateTimeField(null=True)
    mapping_version = models.CharField(max_length=16)
    migrated_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source_system", "source_id"], name="unique_patient_source")
        ]
        indexes = [models.Index(fields=["family_name", "given_name"])]

    def __str__(self):
        return f"Patient {self.source_id}"


class PatientIdentifier(models.Model):
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="identifiers")
    system = models.TextField(blank=True)
    value = models.CharField(max_length=255)
    type_code = models.CharField(max_length=64, blank=True)

    class Meta:
        indexes = [models.Index(fields=["system", "value"])]


class Observation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_system = models.CharField(max_length=255)
    source_id = models.CharField(max_length=128)
    source_version = models.CharField(max_length=128)
    source_last_updated = models.DateTimeField(null=True)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="observations")
    status = models.CharField(max_length=32, null=True)
    category = models.CharField(max_length=64, null=True)
    code_system = models.TextField(blank=True)
    code = models.CharField(max_length=128, blank=True)
    code_display = models.TextField(blank=True)
    code_text = models.TextField(blank=True)
    value_type = models.CharField(max_length=32)
    value_num = models.DecimalField(
        max_digits=30,
        decimal_places=10,
        null=True,
        help_text="Numeric copy for sorting and filtering; SQLite keeps ~15 significant digits.",
    )
    value_unit = models.CharField(max_length=64, blank=True)
    value_text = models.TextField(
        blank=True,
        help_text="Value exactly as received (numbers keep full precision); use this for display.",
    )
    data_absent_reason = models.CharField(max_length=64, blank=True)
    effective_start = models.DateTimeField(null=True)
    effective_end = models.DateTimeField(null=True)
    effective_precision = models.CharField(max_length=8, blank=True)
    components_json = models.JSONField(null=True)
    mapping_version = models.CharField(max_length=16)
    migrated_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source_system", "source_id"], name="unique_observation_source")
        ]
        indexes = [models.Index(fields=["patient", "effective_start"])]

    def __str__(self):
        return f"Observation {self.source_id}"


class PatientDuplicateCandidate(models.Model):
    class Status(models.TextChoices):
        OPEN = "open"
        CONFIRMED = "confirmed"
        REJECTED = "rejected"

    patient_a = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="+")
    patient_b = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="+")
    match_reason = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["patient_a", "patient_b", "match_reason"], name="unique_duplicate_pair"
            )
        ]
