<template>
  <div>
    <div class="alert-bar" v-if="store.alerts.length">
      临期预警：{{ store.alerts.map(a => a.name + '(' + (a.level === 'expired' ? '已过期' : '临期' + (a.days_left ?? '') + '天') + ')').join(' · ') }}
    </div>
    <div class="alert-bar" v-else>临期预警带：暂无紧急批次</div>
    <div class="wrap">
      <nav class="layer-tabs">
        <router-link to="/">全层</router-link>
        <router-link to="/layer/upper">上层</router-link>
        <router-link to="/layer/mid">中层</router-link>
        <router-link to="/layer/lower">下层</router-link>
        <router-link to="/inbound">入库</router-link>
        <router-link to="/consume">消费</router-link>
        <router-link to="/settings">设置</router-link>
      </nav>
      <router-view />
    </div>
  </div>
</template>
<script setup>
import { onMounted } from 'vue'
import { store } from './store'
onMounted(async () => {
  try { await store.loadSettings() } catch {}
  try { await store.refreshAlerts() } catch { store.alerts = [] }
})
</script>
