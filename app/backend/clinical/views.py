from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.http import JsonResponse
from django.views import View

from clinical.models import Observation, Patient
from clinical.serializers import observation_item, patient_detail, patient_summary, run_report
from pipeline.models import RunReport

HIDDEN_BY_DEFAULT = "entered-in-error"
UNNAMED_LAST = Case(
    When(family_name="", given_name="", then=Value(1)), default=Value(0), output_field=IntegerField()
)


def not_found(message):
    return JsonResponse({"error": message}, status=404)


class HealthView(View):
    async def get(self, request):
        return JsonResponse({"status": "ok"})


class PatientListView(View):
    async def get(self, request):
        patients = (
            self.search(request.GET.get("q", ""))
            .annotate(observation_count=Count("observations"))
            .order_by(UNNAMED_LAST, "family_name", "given_name", "source_id")
        )
        results = [patient_summary(patient) async for patient in patients]
        return JsonResponse({"count": len(results), "results": results})

    def search(self, query):
        patients = Patient.objects.all()
        for term in query.split():
            patients = patients.filter(
                Q(family_name__icontains=term) | Q(given_name__icontains=term) | Q(source_id__iexact=term)
            )
        return patients


class PatientDetailView(View):
    async def get(self, request, patient_id):
        patient = await (
            Patient.objects.filter(pk=patient_id).annotate(observation_count=Count("observations")).afirst()
        )
        if patient is None:
            return not_found("patient not found")
        identifiers = [item async for item in patient.identifiers.order_by("system", "value")]
        return JsonResponse(patient_detail(patient, identifiers))


class PatientObservationsView(View):
    async def get(self, request, patient_id):
        if not await Patient.objects.filter(pk=patient_id).aexists():
            return not_found("patient not found")
        observations = Observation.objects.filter(patient_id=patient_id)
        hidden = await observations.filter(status=HIDDEN_BY_DEFAULT).acount()
        if request.GET.get("include_errors") != "1":
            observations = observations.exclude(status=HIDDEN_BY_DEFAULT)
        results = [observation_item(o) async for o in observations.order_by("-effective_start", "code")]
        return JsonResponse({"count": len(results), "results": results, "entered_in_error": hidden})


class UnknownEndpointView(View):
    async def get(self, request):
        return not_found("unknown endpoint")


class LatestRunReportView(View):
    async def get(self, request):
        report = await RunReport.objects.select_related("job").order_by("-created_at").afirst()
        if report is None:
            return not_found("no validation report yet: run `migrate_fhir validate`")
        return JsonResponse(run_report(report))
