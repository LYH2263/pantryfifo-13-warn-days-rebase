<template>
  <div>
    <h1>按临期消费</h1>
    <select v-model.number="item_id" :disabled="!!plan"><option v-for="i in items" :value="i.id">{{ i.name }}</option></select>
    <input type="number" v-model.number="qty" :disabled="!!plan" />
    <button v-if="!plan" @click="preview">FEFO 预演</button>

    <div v-if="plan" style="margin: 10px 0">
      <p class="muted">
        预演口径：warn_days={{ plan.warn_days }} · {{ plan.today }}。
        下列批次的临期/过期标注与顶条同一套规则。
      </p>
      <div v-for="d in plan.deductions" :key="d.lot_id" class="lot">
        批次 #{{ d.lot_id }} 扣 {{ d.take }} · {{ d.expiry || '无到期日' }}
        <span v-if="d.level === 'expired'" style="color:#c45c26">（已过期）</span>
        <span v-else-if="d.level === 'soon'" style="color:#c45c26">（临期 {{ d.days_left }} 天）</span>
      </div>
      <div style="margin-top:10px">
        <button @click="commit" :disabled="busy">{{ busy ? '提交中…' : '确认提交' }}</button>
        <button @click="cancel" style="background:#5a7a7e;margin-left:8px">取消重选</button>
      </div>
    </div>

    <pre v-if="result">{{ result }}</pre>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { store } from '../store'
const items = ref([])
const item_id = ref(1)
const qty = ref(1)
const plan = ref(null)
const result = ref('')
const busy = ref(false)
onMounted(async () => { items.value = await api('/items'); if (items.value[0]) item_id.value = items.value[0].id })

async function preview() {
  result.value = ''
  try {
    // 预演资格按后端当前 warn_days 现算，前端不自己挑 soon 批
    plan.value = await api('/consume/preview', {
      method: 'POST',
      body: JSON.stringify({ item_id: item_id.value, qty: qty.value }),
    })
  } catch (e) { result.value = '预演失败：' + e.message }
}

async function commit() {
  busy.value = true
  try {
    const r = await api('/consume/commit', {
      method: 'POST',
      body: JSON.stringify({ item_id: item_id.value, plan: plan.value }),
    })
    result.value = JSON.stringify(r, null, 2)
    plan.value = null
    // 扣减后顶条按新余量现算，与全层保持一致
    await store.refreshAlerts()
  } catch (e) {
    // 设置/日期/批次在预演后变过：预演资格、顶条、全层不能各过一套 —— 整单已被后端拒绝。
    // 旧预演作废，必须用新口径重新预演。
    result.value = '提交被拒绝：' + e.message + '。预演已过期，请重新预演。'
    plan.value = null
    await store.refreshAlerts()
  } finally {
    busy.value = false
  }
}

function cancel() { plan.value = null; result.value = '' }
</script>
