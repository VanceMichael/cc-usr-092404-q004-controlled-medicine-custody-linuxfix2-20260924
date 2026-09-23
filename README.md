# 药房受控药品保管链服务

在店铺身份事实之上，回答合规主管的夜班问题：**某批号受控药品此刻在发出门店、承运人、接收门店还是隔离区，由谁唯一保管，数量是否守恒。**

Flask 负责 HTTP 边界，SQLAlchemy 连接 SQLite（WAL），所有写操作在单事务内完成；默认数据库文件为 `data/pharmacy_identity.sqlite3`，可用 `DATABASE_PATH` 改址。

```bash
python -m pip install -e ".[test]"
python -m alembic upgrade head
pytest
flask --app 'pharmacy_identity:create_app()' run
```

## 领域不变量

1. **计划时刻冻结**：调拨申请创建时冻结门店许可、承运资质、收发货关系的**版本 ID**、批号与数量；冻结后即使身份记录被纠正，申请看到的仍是当时事实。
2. **双人复核**：FIRST/SECOND 两槽必须由两名不同经办人完成 APPROVE；任一槽驳回即取消。
3. **唯一在途保管人**：出库事件把数量原子地从发出店交给承运人；清洁签收前承运人是唯一保管人，两边库存不可能同时持有同一单位。
4. **异常即隔离即调查**：短少、破损、超时、封签不符自动隔离并创建调查（`INV-*`）；好货/破损/短少分别进入接收店、隔离区与 LOSS 账户。
5. **补偿事件而非删除**：取消释放预留；拒收原车退回；交付后退回分“承运人接走 / 送回发出店”两程；调查裁决可放行、退回或确认损失。历史节点只增不改。
6. **真实发生时间**：离线扫码携带 `occurred_at` 入链（与 `recorded_at` 分列）；处置动作时间锚定在被处置事实之后，防止补录导致链路因果倒置。
7. **越节点转人工**：未出库先扫码、封签不符、时间倒挂只把事实入链并冻结自动流转（HTTP 202 + 人工复核单）。
8. **重复不双计**：扫码按 `(device_id, idempotency_key)` 幂等；写接口支持 `Idempotency-Key` 重放；封签由数据库部分唯一索引裁决，双终端并发确认只有一个成功。
9. **纠正只影响未出库**：许可/关系以新版本纠正（旧版本置 `CORRECTED` 保留），未出库申请立即重验，已出库/已完成的只追加风险说明。
10. **可证明**：`/audit/conservation` 重放全部事件证明批号数量守恒（含无负余额），`/audit/hash-chain` 重算 SHA-256 链证明节点未被篡改或删除。

## 数据模型要点

- `parties / actors / licenses / relationships / drugs / batches / quarantine_locations`：身份与版本化资质。
- `transfer_requests / transfer_lines / reviews`：冻结申请、明细与双人复核。
- `custody_events / custody_items`：**追加型**保管事件链，事件内嵌 `movements`（from/to 保管人 + 批号数量），全局 `prev_hash/event_hash` 串联；SQLite 触发器在数据库层禁止 UPDATE/DELETE。
- `investigations / risk_notes / manual_reviews`：调查、风险说明与人工复核。
- `idempotent_requests`：HTTP 幂等响应缓存。

## 主要接口

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /admin/{parties,actors,quarantines,drugs,batches,licenses,relationships}` | 主数据登记 |
| `POST /admin/licenses/{id}/{status,correct}` | 许可暂停/恢复/版本纠正 |
| `POST /admin/relationships/{id}/correct` | 关系版本纠正 |
| `POST /transfers` | 创建申请（冻结许可、资质、批号、数量） |
| `POST /transfers/{n}/reviews` | 双人复核（FIRST/SECOND） |
| `POST /transfers/{n}/dispatch` | 施封出库，承运人成为唯一在途保管人 |
| `POST /transfers/{n}/scans` | 在途/离线扫码，越节点转人工 |
| `POST /transfers/{n}/receive` | 对封签与实收数量签字；异常自动隔离立案 |
| `POST /transfers/{n}/{cancel,reject,return}` | 补偿：取消 / 拒收 / 两程退回 |
| `POST /investigations/{case}/resolve` | 调查裁决：放行 / 退回 / 确认损失 |
| `POST /manual-reviews/{id}/resolve` | 处理人工复核单 |
| `POST /system/timeout-sweep` | 恢复运行后补做超时扫描与立案 |
| `GET /batches/{p}/{b}/location?at=` | 任意时刻批号定位 |
| `GET /transfers/{n}/custody-intervals` | 连续不重叠的保管区间 |
| `GET /audit/{conservation,hash-chain}` | 守恒与哈希链证明 |

请求带 `X-Actor-Id`；角色为 `ADMIN / STORE_STAFF / CARRIER_STAFF / AUDITOR`，门店、承运、审计只返回履职所需字段（`views.py` 投影）。写请求可带 `Idempotency-Key`。

## 开发检查

- 编译检查：`python3 -m compileall -q src migrations`
- 全量测试：`pytest`（覆盖跨午夜、短少/破损/超时、补偿恢复、版本纠正、离线扫码、越节点、幂等、封签并发竞争、角色视图、哈希链与真实迁移升降级）
