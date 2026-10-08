const NOT_RECORDED = 'Not recorded'
const DIMENSIONLESS = '1'

function withUnit(value, unit) {
  return unit && unit !== DIMENSIONLESS ? `${value} ${unit}` : value
}

export function fullName(patient) {
  return [patient.given_name, patient.family_name].filter(Boolean).join(' ') || 'Unnamed patient'
}

export function birthDate(patient) {
  if (!patient.birth_date) return NOT_RECORDED
  const labels = { year: 'year only', month: 'month only' }
  const note = labels[patient.birth_date_precision]
  return note ? `${patient.birth_date} (${note})` : patient.birth_date
}

export function gender(value) {
  return value ? value[0].toUpperCase() + value.slice(1) : NOT_RECORDED
}

export function deceased(patient) {
  if (patient.deceased === null || patient.deceased === undefined) return NOT_RECORDED
  if (!patient.deceased) return 'No'
  if (!patient.deceased_at) return 'Yes'
  return `Yes (${patient.deceased_at.slice(0, { year: 4, month: 7 }[patient.deceased_precision] ?? 10)})`
}

export function effectiveDate(observation) {
  const start = observation.effective_start
  if (!start) return NOT_RECORDED
  const date = observation.effective_precision === 'second'
    ? new Date(start).toLocaleString()
    : start.slice(0, { year: 4, month: 7 }[observation.effective_precision] ?? 10)
  return observation.effective_end ? `${date} → ${new Date(observation.effective_end).toLocaleString()}` : date
}

export function observationValue(observation) {
  if (observation.value !== null && observation.value !== undefined) {
    return withUnit(observation.value, observation.unit)
  }
  if (observation.data_absent_reason) return `No value (${observation.data_absent_reason})`
  if (observation.components.length) return ''
  return 'No value'
}

export function componentValue(component) {
  if (component.value === null || component.value === undefined) return 'No value'
  return withUnit(component.value, component.unit)
}
