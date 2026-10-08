<script setup>
import { onMounted, ref, watch } from 'vue'
import { api } from '../api'
import { birthDate, fullName, gender } from '../format'

const query = ref('')
const data = ref({ count: 0, results: [] })
const loading = ref(false)
const error = ref('')
let searchTimer

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.patients({ q: query.value.trim() })
  } catch (failure) {
    error.value = failure.message
  } finally {
    loading.value = false
  }
}

watch(query, () => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(load, 300)
})

onMounted(load)
</script>

<template>
  <section>
    <div class="page-header">
      <h1>Patients</h1>
      <span class="muted">{{ data.count.toLocaleString() }} migrated</span>
    </div>

    <input v-model="query" class="search" type="search" placeholder="Search by name or source id" />

    <p v-if="error" class="error">{{ error }}</p>

    <table v-else>
      <thead>
        <tr>
          <th>Name</th>
          <th>Birth date</th>
          <th>Gender</th>
          <th class="numeric">Observations</th>
          <th>Source id</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="patient in data.results" :key="patient.id">
          <td><RouterLink :to="`/patients/${patient.id}`">{{ fullName(patient) }}</RouterLink></td>
          <td class="nowrap">{{ birthDate(patient) }}</td>
          <td class="nowrap" :class="{ muted: !patient.gender }">{{ gender(patient.gender) }}</td>
          <td class="numeric">{{ patient.observation_count }}</td>
          <td class="muted mono">{{ patient.source_id }}</td>
        </tr>
        <tr v-if="!loading && !data.results.length">
          <td colspan="5" class="muted">No patients found.</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
