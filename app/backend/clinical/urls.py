from django.urls import path, re_path

from clinical import views

urlpatterns = [
    path("health", views.HealthView.as_view()),
    path("patients", views.PatientListView.as_view()),
    path("patients/<uuid:patient_id>", views.PatientDetailView.as_view()),
    path("patients/<uuid:patient_id>/observations", views.PatientObservationsView.as_view()),
    path("runs/latest", views.LatestRunReportView.as_view()),
    re_path(r"^.*$", views.UnknownEndpointView.as_view()),
]
