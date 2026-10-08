<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const report = ref(null)
const error = ref('')

onMounted(async () => {
  try {
    report.value = await api.latestReport()
  } catch (failure) {
    error.value = failure.message
  }
})
</script>

<template>
  <section>
    <div class="page-header">
      <h1>Run report</h1>
      <span v-if="report" class="verdict" :class="report.passed ? 'ok' : 'bad'">
        {{ report.passed ? 'Passed' : 'Failed' }}
      </span>
    </div>
    <p v-if="error" class="error">{{ error }}</p>

    <template v-if="report">
      <p class="muted">
        Report #{{ report.id }} · {{ new Date(report.created_at).toLocaleString() }}
        <template v-if="report.job"> · export cut-off {{ report.job.transaction_time }}</template>
      </p>

      <h2>Validation checks</h2>
      <table>
        <thead><tr><th></th><th>Check</th><th class="numeric">Expected</th><th class="numeric">Actual</th><th>Detail</th></tr></thead>
        <tbody>
          <tr v-for="check in report.checks" :key="check.name">
            <td><span class="mark" :class="check.passed ? 'ok' : 'bad'">{{ check.passed ? '✓' : '✕' }}</span></td>
            <td class="mono">{{ check.name }}</td>
            <td class="numeric">{{ check.expected }}</td>
            <td class="numeric">{{ check.actual }}</td>
            <td class="muted">{{ check.detail }}</td>
          </tr>
        </tbody>
      </table>
      <p class="muted small">
        Sample: {{ report.sample.checked.Patient + report.sample.checked.Observation }} records re-mapped from the raw
        store (seed {{ report.sample.seed }}).
      </p>

      <h2>Quarantine</h2>
      <p class="muted">Kept aside with a reason, never deleted. Must be signed off by the data owner.</p>
      <table>
        <thead><tr><th>Type</th><th>Reason</th><th class="numeric">Records</th></tr></thead>
        <tbody>
          <tr v-for="row in report.totals.quarantine_reasons" :key="row.resource_type + row.reason">
            <td>{{ row.resource_type }}</td>
            <td>{{ row.reason }}</td>
            <td class="numeric">{{ row.count.toLocaleString() }}</td>
          </tr>
        </tbody>
      </table>

      <h2>Potential duplicate patients</h2>
      <p class="muted">Informational only: nothing is merged automatically, since a wrong merge is a clinical risk.</p>
      <table>
        <thead><tr><th>Match reason</th><th class="numeric">Pairs</th></tr></thead>
        <tbody>
          <tr v-for="(count, reason) in report.totals.duplicate_candidates" :key="reason">
            <td>{{ reason }}</td>
            <td class="numeric">{{ count.toLocaleString() }}</td>
          </tr>
        </tbody>
      </table>
    </template>
  </section>
</template>
