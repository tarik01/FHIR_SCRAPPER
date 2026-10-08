from datetime import UTC, date, datetime
from decimal import Decimal

from django.test import SimpleTestCase

from pipeline.services.mapping import (
    ObservationMapper,
    ObservationRecord,
    PatientMapper,
    PatientRecord,
    Rejected,
    parse_fhir_datetime,
)

BASE = "https://fhir.example.test/baseR4"
TODAY = date(2026, 10, 8)


def observation(**overrides):
    resource = {
        "resourceType": "Observation",
        "id": "o1",
        "status": "final",
        "subject": {"reference": "Patient/p1"},
        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4", "display": "Heart rate"}]},
        "valueQuantity": {"value": Decimal("72"), "unit": "/min"},
        "effectiveDateTime": "2026-01-02T10:00:00-03:00",
    }
    resource.update(overrides)
    return {k: v for k, v in resource.items() if v is not None}


def map_obs(**overrides):
    return ObservationMapper(known_patient_ids={"p1"}, base_url=BASE).map(observation(**overrides))


def map_patient(resource):
    return PatientMapper(today=TODAY).map(resource)


class PatientMappingTests(SimpleTestCase):
    def test_official_name_is_preferred(self):
        record = map_patient({"name": [
            {"use": "usual", "given": ["Bia"], "family": "Souza"},
            {"use": "official", "given": ["Beatriz", "Ana"], "family": "Souza"},
        ]})
        self.assertEqual((record.given_name, record.family_name), ("Beatriz Ana", "Souza"))

    def test_name_without_use_falls_back_to_first(self):
        record = map_patient({"name": [{"given": ["Test"], "family": "First"}, {"family": "Second"}]})
        self.assertEqual(record.family_name, "First")

    def test_text_only_name_is_kept(self):
        record = map_patient({"name": [{"text": "Test Synthetic"}]})
        self.assertEqual((record.given_name, record.family_name), ("Test Synthetic", ""))

    def test_all_identifiers_are_kept(self):
        record = map_patient({"identifier": [
            {"system": "urn:mrn", "value": "M1", "type": {"coding": [{"code": "MR"}]}},
            {"system": "urn:ssn", "value": "000-00-0000"},
            {"system": "urn:empty"},
        ]})
        self.assertEqual([(i.system, i.value, i.type_code) for i in record.identifiers],
                         [("urn:mrn", "M1", "MR"), ("urn:ssn", "000-00-0000", "")])

    def test_partial_birth_date_keeps_its_precision(self):
        record = map_patient({"birthDate": "1980"})
        self.assertEqual((record.birth_date, record.birth_date_precision), ("1980", "year"))

    def test_birth_date_with_time_is_truncated(self):
        record = map_patient({"birthDate": "1967-08-22T00:00:00"})
        self.assertEqual((record.birth_date, record.birth_date_precision), ("1967-08-22", "day"))

    def test_future_birth_date_is_rejected(self):
        self.assertEqual(map_patient({"birthDate": "5032-04-12"}), Rejected("birthDate in the future"))

    def test_missing_gender_stays_null_not_unknown(self):
        self.assertIsNone(map_patient({}).gender)
        self.assertEqual(map_patient({"gender": "unknown"}).gender, "unknown")

    def test_deceased_absence_is_not_alive(self):
        self.assertEqual(map_patient({}).deceased, None)
        record = map_patient({"deceasedDateTime": "2020-05-01"})
        self.assertEqual((record.deceased, record.deceased_at), (True, datetime(2020, 5, 1, tzinfo=UTC)))
        self.assertIsInstance(record, PatientRecord)

    def test_partial_deceased_date_keeps_its_precision(self):
        year_only = map_patient({"deceasedDateTime": "2020"})
        self.assertEqual((year_only.deceased_at, year_only.deceased_precision),
                         (datetime(2020, 1, 1, tzinfo=UTC), "year"))
        self.assertEqual(map_patient({"deceasedDateTime": "2020-05"}).deceased_precision, "month")
        self.assertEqual(map_patient({"deceasedDateTime": "2020-05-01T10:00:00Z"}).deceased_precision, "second")
        self.assertEqual(map_patient({"deceasedBoolean": True}).deceased_precision, "")


class ObservationSubjectTests(SimpleTestCase):
    def test_relative_patient_reference(self):
        self.assertEqual(map_obs().patient_source_id, "p1")

    def test_absolute_reference_to_known_server(self):
        self.assertEqual(map_obs(subject={"reference": f"{BASE}/Patient/p1"}).patient_source_id, "p1")

    def test_location_subject_is_rejected(self):
        self.assertEqual(map_obs(subject={"reference": "Location/42"}),
                         Rejected("subject is not a Patient (Location)"))

    def test_identifier_only_subject_is_rejected(self):
        self.assertEqual(map_obs(subject={"identifier": {"value": "X"}, "display": "Someone"}),
                         Rejected("subject has no reference"))

    def test_foreign_server_is_rejected(self):
        self.assertEqual(map_obs(subject={"reference": "https://other.example/Patient/p1"}),
                         Rejected("subject references a foreign server"))

    def test_unknown_patient_is_rejected(self):
        self.assertEqual(map_obs(subject={"reference": "Patient/missing"}), Rejected("subject patient not loaded"))


class ObservationValueTests(SimpleTestCase):
    def test_quantity(self):
        record = map_obs()
        self.assertEqual((record.value_type, record.value_num, record.value_unit), ("Quantity", Decimal("72"), "/min"))

    def test_full_decimal_precision_is_kept_as_text(self):
        record = map_obs(valueQuantity={"value": Decimal("0.41177063024789895"), "unit": "score"})
        self.assertEqual(record.value_text, "0.41177063024789895")

    def test_codeable_concept(self):
        record = map_obs(valueQuantity=None, valueCodeableConcept={"coding": [{"code": "N", "display": "Negative"}]})
        self.assertEqual((record.value_type, record.value_text), ("CodeableConcept", "Negative"))

    def test_string_boolean_integer(self):
        self.assertEqual(map_obs(valueQuantity=None, valueString="trace").value_text, "trace")
        self.assertEqual(map_obs(valueQuantity=None, valueBoolean=False).value_text, "false")
        self.assertEqual(map_obs(valueQuantity=None, valueInteger=3).value_num, Decimal(3))

    def test_unsupported_value_type_is_rejected(self):
        result = map_obs(valueQuantity=None, valueRange={"low": {"value": 1}})
        self.assertEqual(result, Rejected("unsupported value type: valueRange"))

    def test_no_value_with_components_is_accepted(self):
        record = map_obs(valueQuantity=None, component=[
            {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6", "display": "Systolic"}]},
             "valueQuantity": {"value": Decimal("120"), "unit": "mmHg"}},
        ])
        self.assertEqual(record.value_type, "none")
        self.assertEqual(record.components_json,
                         [{"code": "8480-6", "display": "Systolic", "value_type": "Quantity", "value": "120", "unit": "mmHg"}])

    def test_no_value_with_absent_reason_is_accepted(self):
        record = map_obs(valueQuantity=None, dataAbsentReason={"coding": [{"code": "asked-declined"}]})
        self.assertEqual(record.data_absent_reason, "asked-declined")

    def test_no_value_at_all_is_rejected(self):
        self.assertEqual(map_obs(valueQuantity=None),
                         Rejected("no value, components, members or data absent reason"))


class ObservationCodeAndDateTests(SimpleTestCase):
    def test_loinc_is_preferred(self):
        record = map_obs(code={"coding": [{"system": "urn:local", "code": "HR"},
                                          {"system": "http://loinc.org", "code": "8867-4"}]})
        self.assertEqual((record.code_system, record.code), ("http://loinc.org", "8867-4"))

    def test_text_only_code_is_kept(self):
        record = map_obs(code={"text": "Model confidence"})
        self.assertEqual((record.code, record.code_text), ("", "Model confidence"))

    def test_missing_code_is_rejected(self):
        self.assertEqual(map_obs(code={}), Rejected("observation has no code"))

    def test_datetime_is_converted_to_utc(self):
        self.assertEqual(map_obs().effective_start, datetime(2026, 1, 2, 13, 0, tzinfo=UTC))

    def test_date_only_keeps_day_precision(self):
        record = map_obs(effectiveDateTime="2020-05-01")
        self.assertEqual((record.effective_start, record.effective_precision),
                         (datetime(2020, 5, 1, tzinfo=UTC), "day"))

    def test_period_has_start_and_end(self):
        record = map_obs(effectiveDateTime=None,
                         effectivePeriod={"start": "2020-05-01T10:00:00Z", "end": "2020-05-01T11:00:00Z"})
        self.assertEqual(record.effective_end - record.effective_start, datetime(2020, 1, 1, 1) - datetime(2020, 1, 1))

    def test_seven_fraction_digits_are_parsed(self):
        parsed, _ = parse_fhir_datetime("2024-03-01T10:00:00.1234567+00:00")
        self.assertEqual(parsed.microsecond, 123456)

    def test_datetime_without_timezone_is_quarantined_not_assumed_utc(self):
        self.assertEqual(map_obs(effectiveDateTime="2026-09-29T09:00:00"),
                         Rejected("effective date without timezone"))
        self.assertEqual(map_patient({"deceasedDateTime": "2020-05-01T10:00:00"}),
                         Rejected("deceasedDateTime without timezone"))

    def test_entered_in_error_status_is_kept(self):
        record = map_obs(status="entered-in-error")
        self.assertIsInstance(record, ObservationRecord)
        self.assertEqual(record.status, "entered-in-error")
