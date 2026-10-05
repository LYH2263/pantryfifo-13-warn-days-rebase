import { reactive } from 'vue'
import { api } from './api'

// 全应用唯一的顶条/设置状态：设置页改 warn_days 成功后，
// 顶条在这里立刻按新值重拉，任何页面都不持有各自的副本。
export const store = reactive({
  alerts: [],
  warnDays: null,

  async refreshAlerts() {
    // 顶条永远向后端现算要结果；本地不做 soon/expired 推断
    this.alerts = await api('/alerts')
  },

  async loadSettings() {
    const s = await api('/settings')
    this.warnDays = parseInt(s.warn_days, 10)
    return s
  },

  async setWarnDays(value) {
    // 失败（非正整数等 4xx）直接抛出：调用方回退输入框，this.warnDays 与顶条都保持改前
    const s = await api('/settings', {
      method: 'POST',
      body: JSON.stringify({ warn_days: value }),
    })
    this.warnDays = parseInt(s.warn_days, 10)
    await this.refreshAlerts()
  },
})
