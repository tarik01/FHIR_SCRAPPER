import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import './style.css'
import App from './App.vue'
import PatientList from './pages/PatientList.vue'
import PatientDetail from './pages/PatientDetail.vue'
import RunReport from './pages/RunReport.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/patients' },
    { path: '/patients', component: PatientList },
    { path: '/patients/:id', component: PatientDetail, props: true },
    { path: '/report', component: RunReport },
  ],
})

createApp(App).use(router).mount('#app')
