<template>
  <div>
    <h1>按临期消费</h1>
    <select v-model.number="item_id"><option v-for="i in items" :value="i.id">{{ i.name }}</option></select>
    <input type="number" v-model.number="qty" />
    <button @click="preview">消费预演</button>

    <div v-if="plan" style="margin-top:12px">
      <h3>预演（按当前 warn_days={{ plan.warn_days }} 现算）</h3>
      <p v-if="!plan.ok" class="muted" style="color:var(--alert)">
        {{ plan.reason === 'short' ? '余量不足，还差 ' + plan.short : '数量非法' }}
      </p>
      <table v-else style="border-collapse:collapse">
        <tr v-for="d in plan.deductions" :key="d.lot_id">
          <td class="lot">批次 #{{ d.lot_id }} · 到期 {{ d.expiry }} · 扣 {{ d.take }} ·
            <b :style="{ color: d.level === 'expired' ? 'var(--alert)' : 'inherit' }">{{ levelText[d.level] }}</b>
          </td>
        </tr>
      </table>
      <template v-if="plan.ok">
        <button style="margin-top:8px" @click="commit">确认提交</button>
        <button style="margin-left:8px;background:#6b8a8d" @click="plan = null">取消</button>
      </template>
      <p v-if="err" class="muted" style="color:var(--alert)">{{ err }}</p>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { refreshAlerts, levelText } from '../store'
const items = ref([])
const item_id = ref(1)
const qty = ref(1)
const plan = ref(null)
const err = ref('')
onMounted(async () => { items.value = await api('/items'); if (items.value[0]) item_id.value = items.value[0].id })

async function preview() {
  err.value = ''
  try {
    // 预演只做展示与快照，不扣任何余量；资格以提交时服务端重算为准
    plan.value = await api('/consume/preview', {
      method: 'POST', body: JSON.stringify({ item_id: item_id.value, qty: qty.value }),
    })
  } catch (e) { plan.value = null; err.value = e.message }
}

async function commit() {
  err.value = ''
  try {
    await api('/consume', {
      method: 'POST',
      body: JSON.stringify({
        item_id: item_id.value, qty: qty.value,
        warn_days: plan.value.warn_days, snapshot: plan.value.snapshot.lots,
      }),
    })
    plan.value = null
    await refreshAlerts() // 顶条立刻反映扣减后的真实在架批
  } catch (e) {
    // 409：预演后 warn_days 改过、批被下架/消费等 → 整单未落库，要求重新预演
    plan.value = null
    err.value = '提交被拒绝（设置或批次已变化，未做任何扣减）：' + e.message + '，请重新预演'
    await refreshAlerts()
  }
}
</script>
