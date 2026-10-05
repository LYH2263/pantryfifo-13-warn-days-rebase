<template>
  <div>
    <h1>设置</h1>
    <label class="muted" for="warn-days">临期预警天数（正整数）</label>
    <div>
      <input id="warn-days" type="number" min="1" step="1" v-model="input" style="width:120px" />
      <button @click="save">保存</button>
    </div>
    <p class="muted" v-if="msg" :style="{ color: ok ? 'var(--teal)' : 'var(--alert)' }">{{ msg }}</p>
    <p class="muted">
      改天数只影响顶条现算的临期/过期归类；全层批次余量、状态与入库时的到期日字符串不变。
      已过期下架的批不会因改天数回到在架。
    </p>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { saveWarnDays, warnDays, parseWarnDays, loadSettings } from '../store'
const input = ref('')
const msg = ref('')
const ok = ref(false)
onMounted(async () => { await loadSettings().catch(() => {}); input.value = warnDays.value || '' })
async function save() {
  // 前端先按与后端一致的口径拦截；非法时设置与顶条都维持改前
  if (parseWarnDays(input.value) === null) {
    ok.value = false
    msg.value = '请输入正整数，设置未改动'
    input.value = warnDays.value
    return
  }
  try {
    await saveWarnDays(input.value)
    ok.value = true
    msg.value = '已保存，顶条已按新天数现算'
  } catch (e) {
    // 400 等失败：store 未更新，顶条仍是旧值
    ok.value = false
    msg.value = '保存失败：' + e.message
    input.value = warnDays.value
  }
}
</script>
