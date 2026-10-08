import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

LOINC = "http://loinc.org"
GENDERS = {"male", "female", "other", "unknown"}
VALUE_TYPES = {
    "valueQuantity": "Quantity",
    "valueCodeableConcept": "CodeableConcept",
    "valueString": "String",
    "valueBoolean": "Boolean",
    "valueInteger": "Integer",
}
PARTIAL_DATE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")
EXTRA_FRACTION_DIGITS = re.compile(r"(\.\d{6})\d+")


class Reject(Exception):
    pass


@dataclass(frozen=True)
class Rejected:
    reason: str


@dataclass(frozen=True)
class Identifier:
    system: str
    value: str
    type_code: str


@dataclass(frozen=True)
class PatientRecord:
    given_name: str
    family_name: str
    birth_date: str
    birth_date_precision: str
    gender: str | None
    deceased: bool | None
    deceased_at: datetime | None
    identifiers: tuple[Identifier, ...]


@dataclass(frozen=True)
class Value:
    value_type: str
    value_num: Decimal | None = None
    value_unit: str = ""
    value_text: str = ""


@dataclass(frozen=True)
class ObservationRecord:
    patient_source_id: str
    status: str | None
    category: str | None
    code_system: str
    code: str
    code_display: str
    code_text: str
    value_type: str
    value_num: Decimal | None
    value_unit: str
    value_text: str
    data_absent_reason: str
    effective_start: datetime | None
    effective_end: datetime | None
    effective_precision: str
    components_json: list | None


def date_precision(month, day):
    return "day" if day else "month" if month else "year"


def parse_fhir_datetime(value):
    match = PARTIAL_DATE.match(value)
    if match:
        year, month, day = match.groups()
        return datetime(int(year), int(month or 1), int(day or 1), tzinfo=UTC), date_precision(month, day)
    parsed = datetime.fromisoformat(EXTRA_FRACTION_DIGITS.sub(r"\1", value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC), "second"


def first_code(concept):
    codings = (concept or {}).get("coding") or []
    return codings[0].get("code", "") if codings else ""


def choose_coding(concept):
    codings = (concept or {}).get("coding") or []
    return next((c for c in codings if c.get("system") == LOINC), codings[0] if codings else {})


def to_decimal(number):
    if number is None:
        return None
    try:
        return Decimal(str(number))
    except InvalidOperation:
        raise Reject("invalid numeric value")


def exact_text(number):
    return "" if number is None else str(number)


def read_value(element):
    key = next((k for k in element if k.startswith("value")), None)
    if key is None:
        return Value("none")
    if key not in VALUE_TYPES:
        raise Reject(f"unsupported value type: {key}")
    raw = element[key]
    match key:
        case "valueQuantity":
            number = to_decimal(raw.get("value"))
            return Value("Quantity", value_num=number, value_text=exact_text(number),
                         value_unit=raw.get("unit") or raw.get("code") or "")
        case "valueInteger":
            number = to_decimal(raw)
            return Value("Integer", value_num=number, value_text=exact_text(number))
        case "valueBoolean":
            return Value("Boolean", value_text="true" if raw else "false")
        case "valueString":
            return Value("String", value_text=raw)
        case "valueCodeableConcept":
            coding = choose_coding(raw)
            return Value("CodeableConcept",
                         value_text=raw.get("text") or coding.get("display") or coding.get("code", ""))


class PatientMapper:
    def __init__(self, today):
        self.today = today

    def map(self, resource):
        try:
            given_name, family_name = self.name(resource.get("name"))
            birth_date, birth_date_precision = self.birth_date(resource.get("birthDate"))
            deceased, deceased_at = self.deceased(resource)
        except Reject as rejection:
            return Rejected(str(rejection))
        return PatientRecord(
            given_name=given_name,
            family_name=family_name,
            birth_date=birth_date,
            birth_date_precision=birth_date_precision,
            gender=self.gender(resource.get("gender")),
            deceased=deceased,
            deceased_at=deceased_at,
            identifiers=self.identifiers(resource.get("identifier")),
        )

    def name(self, names):
        if not names:
            return "", ""
        name = next((n for n in names if n.get("use") == "official"), names[0])
        given = " ".join(name.get("given") or [])
        family = name.get("family") or ""
        if not given and not family:
            return name.get("text", ""), ""
        return given, family

    def birth_date(self, value):
        if not value:
            return "", ""
        date_part = value.split("T", 1)[0]
        match = PARTIAL_DATE.match(date_part)
        if not match:
            raise Reject("invalid birthDate")
        year, month, day = match.groups()
        try:
            earliest_possible = date(int(year), int(month or 1), int(day or 1))
        except ValueError:
            raise Reject("invalid birthDate")
        if earliest_possible > self.today:
            raise Reject("birthDate in the future")
        return date_part, date_precision(month, day)

    def deceased(self, resource):
        if "deceasedDateTime" in resource:
            try:
                return True, parse_fhir_datetime(resource["deceasedDateTime"])[0]
            except ValueError:
                raise Reject("invalid deceasedDateTime")
        if "deceasedBoolean" in resource:
            return bool(resource["deceasedBoolean"]), None
        return None, None

    def gender(self, value):
        return value if value in GENDERS else None

    def identifiers(self, items):
        return tuple(
            Identifier(item.get("system", ""), str(item["value"]), first_code(item.get("type")))
            for item in items or []
            if item.get("value")
        )


class ObservationMapper:
    def __init__(self, known_patient_ids, base_url):
        self.known_patient_ids = known_patient_ids
        self.base_url = base_url

    def map(self, resource):
        try:
            patient_id = self.patient_id(resource.get("subject"))
            code = self.code(resource.get("code") or {})
            value = read_value(resource)
            components = self.components(resource.get("component"))
            absent_reason = resource.get("dataAbsentReason")
            self.ensure_has_result(value, components, resource.get("hasMember"), absent_reason)
            effective_start, effective_end, effective_precision = self.effective(resource)
        except Reject as rejection:
            return Rejected(str(rejection))
        return ObservationRecord(
            patient_source_id=patient_id,
            status=resource.get("status"),
            category=self.category(resource.get("category")),
            **code,
            value_type=value.value_type,
            value_num=value.value_num,
            value_unit=value.value_unit,
            value_text=value.value_text,
            data_absent_reason=first_code(absent_reason) or (absent_reason or {}).get("text", ""),
            effective_start=effective_start,
            effective_end=effective_end,
            effective_precision=effective_precision,
            components_json=components or None,
        )

    def patient_id(self, subject):
        if not subject:
            raise Reject("missing subject")
        reference = subject.get("reference")
        if not reference:
            raise Reject("subject has no reference")
        if "://" in reference:
            if not reference.startswith(f"{self.base_url}/"):
                raise Reject("subject references a foreign server")
            reference = reference[len(self.base_url) + 1:]
        resource_type, _, rest = reference.partition("/")
        if resource_type != "Patient":
            raise Reject(f"subject is not a Patient ({resource_type})")
        patient_id = rest.split("/", 1)[0]
        if not patient_id:
            raise Reject("subject has no reference")
        if patient_id not in self.known_patient_ids:
            raise Reject("subject patient not loaded")
        return patient_id

    def code(self, concept):
        coding = choose_coding(concept)
        if not coding.get("code") and not concept.get("text"):
            raise Reject("observation has no code")
        return {
            "code_system": coding.get("system", ""),
            "code": coding.get("code", ""),
            "code_display": coding.get("display", ""),
            "code_text": concept.get("text", ""),
        }

    def category(self, categories):
        return next((first_code(c) for c in categories or [] if first_code(c)), None)

    def components(self, components):
        return [self.component(c) for c in components or []]

    def component(self, component):
        coding = choose_coding(component.get("code"))
        value = read_value(component)
        return {
            "code": coding.get("code", ""),
            "display": coding.get("display") or (component.get("code") or {}).get("text", ""),
            "value_type": value.value_type,
            "value": value.value_text or None,
            "unit": value.value_unit,
        }

    def ensure_has_result(self, value, components, members, absent_reason):
        if value.value_type == "none" and not (components or members or absent_reason):
            raise Reject("no value, components, members or data absent reason")

    def effective(self, resource):
        try:
            if "effectiveDateTime" in resource:
                start, precision = parse_fhir_datetime(resource["effectiveDateTime"])
                return start, None, precision
            if "effectiveInstant" in resource:
                start, precision = parse_fhir_datetime(resource["effectiveInstant"])
                return start, None, precision
            if "effectivePeriod" in resource:
                period = resource["effectivePeriod"]
                start, precision = parse_fhir_datetime(period["start"]) if period.get("start") else (None, "")
                end = parse_fhir_datetime(period["end"])[0] if period.get("end") else None
                return start, end, precision
        except ValueError:
            raise Reject("invalid effective date")
        return None, None, ""
