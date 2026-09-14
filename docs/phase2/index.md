# 阶段2：实际数据库如何使用

> 基于阶段1的源码级理解，深入 SQLite/PostgreSQL 实战，讲透后端开发中数据库的"怎么用、怎么调、怎么排错"。

## 学习目标

1. **源码级对照**：把阶段1造的 miniDB 与 SQLite/PostgreSQL 真实实现对照
2. **实战技能**：索引策略、事务隔离、连接池、性能调优、高可用
3. **后端集成**：Python 后端如何正确使用数据库

## 章节大纲

| 章 | 主题 | 关键产出 | 状态 |
|---|---|---|---|
| [1](01-sqlite-deep.md) | SQLite 深入 | miniDB vs SQLite 对照表 + C 扩展示例 | ✅ |
| [2](02-postgresql-deep.md) | PostgreSQL 架构深入 | PG 扩展（C）+ MVCC 实验 | ✅ |
| [3](03-index-strategy.md) | 索引策略实战 | TPC-H 数据集对比实验 | ✅ |
| [4](04-transaction.md) | 事务与隔离级别实战 | Python 并发复现脚本 | ✅ |
| [5](05-connection-pool.md) | 连接池与后端集成 | Python 后端示例 | ✅ |
| [6](06-query-tuning.md) | 查询性能调优 | 慢查询调优案例 | ✅ |
| [7](07-ha-scaling.md) | 高可用与扩展 | 部署脚本 + 架构图 | ✅ |
| [8](08-ops.md) | 数据库运维 | runbook + 监控脚本 | ✅ |

## 与阶段1的对照

| 概念 | 阶段1（miniDB） | 阶段2（真实数据库） |
|---|---|---|
| 存储 | Slotted Page 4096B | SQLite B-Tree / PG Heap+Index |
| 缓存 | LRU/Clock Buffer Pool | SQLite mmap / PG Shared Buffers |
| 索引 | B+Tree (固定阶) | SQLite B-Tree / PG B-Tree, Hash, GiST |
| WAL | 单文件追加 | SQLite WAL mode / PG WAL + Checkpoint |
| 事务 | 2PL + MVCC | SQLite WAL / PG MVCC + Snapshot |
| SQL | SELECT/INSERT/DELETE/CREATE 子集 | 完整 SQL + 扩展 |
| 优化 | 启发式 + 简单代价 | SQLite 基于代价 / PG 基于代价+遗传 |
| 执行 | Volcano 迭代器 | SQLite 字节码 / PG Volcano+向量化 |

## 环境准备

```bash
# Python 依赖
pip install psycopg2-binary asyncpg sqlite3 sqlalchemy aiosqlite

# PostgreSQL（本地或 Docker）
docker run --name pg -e POSTGRES_PASSWORD=secret -p 5432:5432 -d postgres:16

# SQLite（Python 内置）
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"
```
