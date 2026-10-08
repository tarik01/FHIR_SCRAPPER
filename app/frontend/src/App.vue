<template>
  <header class="topbar">
    <strong>FHIR Migration</strong>
    <nav>
      <RouterLink to="/patients">Patients</RouterLink>
      <RouterLink to="/report">Run report</RouterLink>
    </nav>
    <span class="badge">Synthetic data</span>
  </header>
  <main class="content">
    <RouterView />
  </main>
</template>
