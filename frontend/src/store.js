import { ref } from 'vue'
import { api } from './api'

// 顶条预警与 warn_days 的前端唯一真源：设置页改天数、消费页提交、下架后
// 都走这里刷新，保证顶条永远与后端 /alerts 现算结果一致。
export const alerts = ref([])
export const warnDays = ref('')

export const levelText = { expired: '已过期', soon: '临期', fresh: '正常' }

export async function loadSettings() {
  const s = await api('/settings')
  warnDays.value = s.warn_days ?? ''
  return s
}

export async function refreshAlerts() {
  try { alerts.value = await api('/alerts') } catch { alerts.value = [] }
}

// 正整数校验前后端同一口径：非正整数不发请求，顶条与设置维持旧值。
export function parseWarnDays(v) {
  const s = String(v ?? '').trim()
  if (!/^[1-9]\d*$/.test(s)) return null
  return Number(s)
}

export async function saveWarnDays(v) {
  const n = parseWarnDays(v)
  if (n === null) throw new Error('请输入正整数')
  const r = await api('/settings/warn-days', {
    method: 'PUT', body: JSON.stringify({ warn_days: n }),
  })
  warnDays.value = String(r.warn_days)
  await refreshAlerts() // 保存成功后顶条立刻按新天数现算
  return r
}
