# Pantryfifo · 冰箱临期先吃

分批入库 → FEFO 扣减 → 过期下架。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5300 |
| API | 10300 |

0-1：`shopping_list` / `recipe_suggest` / `temp_zone`。

## 预警天数（warn_days）

- `PUT /api/settings/warn-days` 仅接受正整数；非法返回 400，设置与顶条维持改前值。
- 改天数只让顶条 `GET /api/alerts` 现算 soon/expired；全层批次 status、余量、入库时的到期日字符串均不变。
- 已过期下架（`/api/expire-sweep`，履历见 `GET /api/shelf-events`）的批不会因改天数回到在架。
- 消费走「预演 `/api/consume/preview` → 确认 `/api/consume`」：提交回传预演快照，预演后设置或批次漂移则整单 409，不做部分扣减。
