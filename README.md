# 药房店铺身份服务

服务用于管理药房店铺身份事实、关系版本与处置记录。Flask 负责 HTTP 边界，SQLAlchemy 只连接 SQLite；默认数据库文件为 `data/pharmacy_identity.sqlite3`，可使用 `DATABASE_PATH` 改址。

```bash
python -m pip install -e ".[test]"
python -m alembic upgrade head
pytest
flask --app 'pharmacy_identity:create_app()' run
```

代码按应用、数据库基础设施、迁移和测试分开。迁移历史由 Alembic 管理，容器启动时先升级数据库，再启动多进程 HTTP 服务。

## 受控药品调拨保管链路

夜班交接要回答的核心问题是"每个批号此刻在谁手里、有多少"。系统不保存可变的库存列，全部结论由只追加事件表 `custody_events` 按真实发生时间重放得到（`chain.py` 纯函数）。

- **计划冻结**：建单时冻结门店许可证、承运资质、批号与数量快照（`frozen_snapshot`），并冻结可用库存；许可暂停/资质失效的建单与出库均被拒绝。
- **双人复核**：申请人不得自审，两位不同复核人各一条 `request_reviews`，复核未齐禁止出库。
- **唯一在途保管人**：出库是一条 `store → carrier` 的 `released` 移交事件（部分唯一索引保证每单仅一条），此后承运人是唯一在途保管人，收发两边不可能同时持有同一批号。
- **封签与签收**：接收方先对封签签字（`seal_confirmations` 唯一约束，两终端抢确认只有一条落库，另一终端拿到原结果），再对实收数量签字；短少/破损/超时直接进隔离区并创建 `investigations`。
- **补偿事件**：取消（未出库）、拒收（在途）、退回（交接后）、解除隔离都写 `compensated` 事件；短少凭调查结论写 `written_off` 出链。
- **历史不可改**：`custody_events` 上有 `BEFORE UPDATE/DELETE` 触发器，纠正只能写新事件；许可/关系后来被纠正时只重验未出库申请（`license_revalidated`），已完成交接只追加 `risk_note`。
- **离线与恢复**：设备离线扫描按 `occurred_at` 真实时刻入链，`recorded_at` 另存落库时刻；越过封签签署节点的扫描转人工复核；同一幂等键重复扫码返回原结果。恢复后 `/system/recover` 补做超时扫描（事件时刻为 `expected_by`）与待办。
- **角色视图**：`?role=store|carrier|audit` 分别只返回履职字段，完整事件链与 `/audit` 仅审计角色可取。

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admin/stores`、`/admin/carriers`、`/admin/staff`、`/admin/batches`、`/admin/genesis` | 主体登记与期初库存 |
| POST | `/admin/stores/<id>/license`、`/admin/carriers/<id>/qualification` | 许可/资质变更 |
| POST | `/admin/revalidate` | 纠正后重验（未出库重验，已交接追加风险说明） |
| POST | `/requests` | 建单并冻结快照与库存 |
| POST | `/requests/<no>/reviews` | 双人复核（approve/reject） |
| POST | `/requests/<no>/release` | 出库（唯一在途保管人开始） |
| POST | `/requests/<no>/seal` | 封签签字（单次，重复返回原结果） |
| POST | `/requests/<no>/receive` | 实收录入，异常自动隔离立案 |
| POST | `/requests/<no>/cancel`、`/reject`、`/return` | 补偿事件 |
| POST | `/scans/offline` | 离线扫描（真实发生时刻，越序转人工） |
| POST | `/system/recover` | 恢复后补做超时扫描与待办 |
| POST | `/investigations/<id>/resolve` | write_off / release_goods / close |
| GET | `/batches/<no>/location?at=` | 夜班交接：批号此刻（或任意时刻）保管归属 |
| GET | `/requests/<no>?role=` | 分角色视图 |
| GET | `/requests/<no>/chain?role=audit` | 完整事件链 |
| GET | `/audit?role=audit&at=` | 唯一保管归属、批号数量守恒、未结调查证明 |

## 开发检查

- 编译检查：`python3 -m compileall -q src`
- 全量测试：`pytest`（25 个用例，含封签双终端竞态、跨午夜运输、只追加触发器、恢复补做）
