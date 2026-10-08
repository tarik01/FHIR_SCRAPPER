def patient_summary(patient):
    return {
        "id": str(patient.id),
        "source_id": patient.source_id,
        "given_name": patient.given_name,
        "family_name": patient.family_name,
        "birth_date": patient.birth_date,
        "birth_date_precision": patient.birth_date_precision,
        "gender": patient.gender,
        "deceased": patient.deceased,
        "observation_count": getattr(patient, "observation_count", None),
    }


def patient_detail(patient, identifiers):
    return {
        **patient_summary(patient),
        "deceased_at": patient.deceased_at,
        "source_version": patient.source_version,
        "source_last_updated": patient.source_last_updated,
        "identifiers": [
            {"system": item.system, "value": item.value, "type_code": item.type_code} for item in identifiers
        ],
    }


def observation_item(observation):
    return {
        "id": str(observation.id),
        "source_id": observation.source_id,
        "status": observation.status,
        "category": observation.category,
        "code_system": observation.code_system,
        "code": observation.code,
        "label": observation.code_display or observation.code_text or observation.code,
        "value_type": observation.value_type,
        "value": observation.value_text or None,
        "unit": observation.value_unit,
        "data_absent_reason": observation.data_absent_reason,
        "effective_start": observation.effective_start,
        "effective_end": observation.effective_end,
        "effective_precision": observation.effective_precision,
        "components": observation.components_json or [],
    }


def run_report(report):
    job = report.job
    return {
        "id": report.pk,
        "created_at": report.created_at,
        "passed": report.passed,
        "checks": report.checks,
        "totals": report.totals,
        "sample": report.sample,
        "job": None if job is None else {
            "job_id": job.job_id,
            "transaction_time": job.transaction_time,
            "completed_at": job.completed_at,
        },
    }
