import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      redirect: '/investigations',
    },
    {
      path: '/investigations',
      name: 'investigations',
      component: () => import('@/views/InvestigationWorkbench.vue'),
    },
  ],
})

export default router
