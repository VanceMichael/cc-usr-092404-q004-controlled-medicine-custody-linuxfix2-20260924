# 药房店铺身份服务

服务用于管理药房店铺身份事实、关系版本与处置记录。Flask 负责 HTTP 边界，SQLAlchemy 只连接 SQLite；默认数据库文件为 `data/pharmacy_identity.sqlite3`，可使用 `DATABASE_PATH` 改址。

```bash
python -m pip install -e ".[test]"
python -m alembic upgrade head
pytest
flask --app 'pharmacy_identity:create_app()' run
```

代码按应用、数据库基础设施、迁移和测试分开。迁移历史由 Alembic 管理，容器启动时先升级数据库，再启动多进程 HTTP 服务。

## 开发检查

- 编译检查：`python3 -m compileall -q src`
