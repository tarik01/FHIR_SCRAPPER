<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { birthDate, componentValue, deceased, effectiveDate, fullName, gender, observationValue } from '../format'

const props = defineProps({ id: { type: String, required: true } })

const patient = ref(null)
const observations = ref([])
const total = ref(0)
const hiddenErrors = ref(0)
const showErrors = ref(false)
const loading = ref(false)
const error = ref('')

async function loadObservations() {
  loading.value = true
  try {
    const body = await api.observations(props.id, { include_errors: showErrors.value ? 1 : '' })
    observations.value = body.results
    total.value = body.count
    hiddenErrors.value = body.entered_in_error
  } catch (failure) {
    error.value = failure.message
  } finally {
    loading.value = false
  }
}

function toggleErrors() {
  showErrors.value = !showErrors.value
  loadObservations()
}

onMounted(async () => {
  try {
    patient.value = await api.patient(props.id)
    await loadObservations()
  } catch (failure) {
    error.value = failure.message
  }
})
</script>

<template>
  <section>
    <RouterLink to="/patients">← Patients</RouterLink>
    <p v-if="error" class="error">{{ error }}</p>

    <template v-if="patient">
      <div class="page-header">
        <h1>{{ fullName(patient) }}</h1>
        <span class="muted mono">source id {{ patient.source_id }}</span>
      </div>

      <dl class="facts">
        <div><dt>Birth date</dt><dd>{{ birthDate(patient) }}</dd></div>
        <div><dt>Gender</dt><dd>{{ gender(patient.gender) }}</dd></div>
        <div><dt>Deceased</dt><dd>{{ deceased(patient) }}</dd></div>
        <div><dt>Observations</dt><dd>{{ patient.observation_count }}</dd></div>
      </dl>

      <h2>Identifiers</h2>
      <table v-if="patient.identifiers.length">
        <thead><tr><th>System</th><th>Value</th><th>Type</th></tr></thead>
        <tbody>
          <tr v-for="item in patient.identifiers" :key="`${item.system}|${item.value}`">
            <td class="mono">{{ item.system || '—' }}</td>
            <td>{{ item.value }}</td>
            <td>{{ item.type_code || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">No identifiers recorded.</p>

      <div class="section-header">
        <h2>Observations <span class="muted small">({{ total }})</span></h2>
        <button v-if="hiddenErrors" class="link" @click="toggleErrors">
          {{ showErrors ? 'Hide' : 'Show' }} {{ hiddenErrors }} entered in error
        </button>
      </div>

      <table v-if="observations.length">
        <thead><tr><th>Date</th><th>Observation</th><th>Value</th><th>Status</th></tr></thead>
        <tbody>
          <tr v-for="observation in observations" :key="observation.id"
              :class="{ struck: observation.status === 'entered-in-error' }">
            <td class="nowrap">{{ effectiveDate(observation) }}</td>
            <td>
              {{ observation.label }}
              <span class="muted mono small">{{ observation.code }}</span>
            </td>
            <td>
              <span :class="{ muted: observation.value === null }">{{ observationValue(observation) }}</span>
              <ul v-if="observation.components.length" class="components">
                <li v-for="component in observation.components" :key="component.code + component.display">
                  {{ component.display || component.code }}: <strong>{{ componentValue(component) }}</strong>
                </li>
              </ul>
            </td>
            <td>
              <span class="status" :class="observation.status">{{ observation.status || 'unknown' }}</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else-if="!loading" class="muted">No observations.</p>
    </template>
  </section>
</template>
