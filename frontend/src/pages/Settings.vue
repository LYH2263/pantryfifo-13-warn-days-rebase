<template>
  <div>
    <h1>设置</h1>
    <label class="muted" for="warn-days">临期预警天数（正整数）</label>
    <div>
      <input
        id="warn-days"
        type="number"
        min="1"
        step="1"
        :value="input"
        @input="onInput"
        style="max-width: 160px"
      />
      <button @click="save" :disabled="busy || input === ''">{{ busy ? '保存中…' : '保存' }}</button>
    </div>
    <p class="muted" v-if="msg" :style="{ color: ok ? '#1f6f78' : '#c45c26' }">{{ msg }}</p>
    <p class="muted">
      改天数只影响顶条「临期」范围：在架批次的余量、状态与到期日均不变；已过期下架的批次不会回到在架。
    </p>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { store } from '../store'

const input = ref('')
const msg = ref('')
const ok = ref(true)
const busy = ref(false)

onMounted(async () => {
  if (store.warnDays == null) await store.loadSettings()
  // 输入框始终以服务端真源初始化
  input.value = String(store.warnDays)
})

function onInput(e) {
  input.value = e.target.value
  msg.value = ''
}

async function save() {
  const raw = input.value.trim()
  // 前端先挡一道：必须是正整数；不通过则输入框与顶条都维持改前
  if (!/^[1-9]\d*$/.test(raw)) {
    ok.value = false
    msg.value = '预警天数必须是正整数'
    input.value = String(store.warnDays)
    return
  }
  const value = Number(raw)
  busy.value = true
  try {
    await store.setWarnDays(value) // 成功后顶条已按新值重算
    ok.value = true
    msg.value = `已保存，顶条已按 ${value} 天重算`
  } catch (e) {
    // 后端拒绝（设置未改）：输入框与顶条都回到改前
    ok.value = false
    msg.value = '保存失败：' + e.message + '，设置与顶条维持改前'
    input.value = String(store.warnDays)
  } finally {
    busy.value = false
  }
}
</script>
