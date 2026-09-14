# miniDB — 从零造一个关系型数据库

> 教学性项目：用 C 语言从字节级开始造一个迷你 RDBMS，再深入 SQLite/PostgreSQL 实战。
> 最终产出：可运行数据库 + 文档站（GitHub Pages）。

---

## 项目定位

- **阶段1**：用 C 语言从字节级开始，造出一个支持 SQL、事务、崩溃恢复的迷你 RDBMS（11 章，可运行可压测）
- **阶段2**：基于阶段1的源码级理解，深入 SQLite/PostgreSQL 实战，讲透后端开发中数据库的"怎么用、怎么调、怎么排错"（8 章，含可复现实验）
- **文档站**：MkDocs Material + Doxygen，部署 GitHub Pages，每章配图解 + 代码 + 习题

**核心原则：两阶段都按完整项目标准设计，不因分阶段而缩减任一阶段的深度与工作量。**

---

## 阶段1：从零造数据库（C 语言实现迷你 RDBMS）

### 章节大纲

| 章 | 主题 | 学习目标 | 关键产出 |
|---|---|---|---|
| 0 | 项目基础设施 | CMake、单元测试、CI、代码规范 | 可构建可测试的空壳 |
| 1 | 存储基础：从字节到页 | 为什么以"页"为IO单位；slotted page；序列化 | `pager` `page` `file_manager` |
| 2 | Buffer Pool | LRU/Clock/LRU-K 对比；并发缓存；脏页刷写 | `buffer_pool` `replacer` |
| 3 | B+Tree 索引 | BST→BTree→B+Tree；分裂/合并；范围扫描；crabbing | `btree` `node` |
| 4 | 堆表存储 | 表/索引分离；Tuple布局；NULL bitmap；TID | `heap` `tuple` `table` |
| 5 | WAL 与崩溃恢复 | ARIES；redo/undo；checkpoint；恢复流程 | `wal` `log_record` `recovery` |
| 6 | 事务与并发控制 | ACID；2PL；死锁检测；MVCC；快照隔离 | `transaction` `lock_manager` `mvcc` |
| 7 | SQL 解析 | 词法/语法分析（手写递归下降）；AST；语义检查 | `lexer` `parser` `ast` |
| 8 | 查询优化 | 关系代数；启发式+代价优化；Join顺序与算法选择 | `optimizer` `cost_model` `statistics` |
| 9 | 执行引擎 | Volcano模型；各算子；表达式求值 | `executor` `operators/*` |
| 10 | 网络协议与CLI | TCP server；协议设计；连接管理；CLI客户端 | `server` `protocol` `client` |
| 11 | 集成与压测 | 全模块串联；sysbench-like压测；对比SQLite | benchmark报告 |

### 模块依赖图

```
        [1 pager/page] ──┐
                        ├─→ [2 buffer_pool] ──┐
                        │                     ├─→ [3 btree] ──┐
                        │                     │              ├─→ [4 heap] ──┐
                        │                     │              │              │
                        └─────────────────────┴──────────────┴──────────────┤
                                                                              │
        [5 wal] ──→ [6 txn/mvcc] ────────────────────────────────────────────┤
                                                                              │
        [7 lexer/parser] ──→ [8 optimizer] ──→ [9 executor] ─────────────────┤
                                                                              │
        [10 server/protocol] ←────────────────────────────────────────────────┘
                                  ↓
                            [11 集成压测]
```

### 里程碑

- **M1**（章1-2）：能读写页、有 Buffer Pool，跑通单元测试
- **M2**（章3-4）：能建 B+Tree 索引和堆表，支持插入/点查/范围查
- **M3**（章5-6）：有 WAL 和 MVCC 事务，拔电源后能崩溃恢复
- **M4**（章7-9）：能执行 `SELECT/INSERT/UPDATE/DELETE` 带 WHERE 和 JOIN
- **M5**（章10-11）：TCP 可访问，能跑压测，与 SQLite 对比性能

---

## 阶段2：实际数据库如何使用（SQLite + PostgreSQL）

| 章 | 主题 | 学习目标 | 关键产出 |
|---|---|---|---|
| 1 | SQLite 深入 | 架构对照；源码级理解 Pager/BTree/WAL；C 扩展 | miniDB vs SQLite 对照表 + C 扩展示例 |
| 2 | PostgreSQL 架构深入 | backend/shared buffer/WAL；MVCC实现；VACUUM；扩展开发 | 一个 PG 扩展（C） |
| 3 | 索引策略实战 | 类型选择；复合索引；覆盖索引；失效场景 | TPC-H 数据集对比实验 |
| 4 | 事务与隔离级别实战 | 四级隔离；脏读/不可重复读/幻读演示；写偏斜；死锁排查 | Python 并发复现脚本 |
| 5 | 连接池与后端集成 | PgBouncer原理；ORM的N+1；事务边界 | Python 后端示例（psycopg/asyncpg） |
| 6 | 查询性能调优 | EXPLAIN详解；统计信息；慢查询；Join优化 | 真实慢查询调优案例 |
| 7 | 高可用与扩展 | 流复制/逻辑复制；读写分离；分库分表（Citus） | 部署脚本 + 架构图 |
| 8 | 数据库运维 | 备份/PITR；监控；容量规划；安全加固 | runbook |

---

## 目录结构

```
database/
├── README.md
├── BLUEPRINT.md                   # 本蓝图
├── LICENSE
├── .gitignore
├── CMakeLists.txt
├── docs/                          # 文档站
│   ├── mkdocs.yml
│   ├── index.md
│   ├── phase1/                    # 阶段1每章一文档
│   ├── phase2/                    # 阶段2每章一文档
│   ├── assets/                    # 图表
│   └── Doxyfile                   # C API 文档配置
├── phase1/                        # 从零造数据库
│   ├── CMakeLists.txt
│   ├── src/
│   │   ├── core/                  # pager, page, buffer_pool, replacer
│   │   ├── storage/               # btree, heap, tuple, table
│   │   ├── txn/                   # wal, transaction, mvcc, lock_manager
│   │   ├── sql/                   # lexer, parser, ast
│   │   ├── optimizer/             # optimizer, cost_model, statistics
│   │   ├── executor/              # executor, operators/*
│   │   ├── server/                # server, protocol, client
│   │   └── main.c
│   ├── tests/                     # 镜像 src 结构
│   ├── benchmarks/
│   └── examples/
├── phase2/                        # 实际数据库使用
│   ├── 01-sqlite-deep/
│   ├── 02-postgresql-deep/
│   │   └── extensions/            # 写一个 PG 扩展
│   ├── 03-index-strategy/
│   │   ├── data/                  # TPC-H
│   │   └── queries/
│   ├── 04-transaction/
│   │   └── concurrency_demos/     # Python 脚本复现并发问题
│   ├── 05-connection-pool/
│   │   └── backend_example/       # Python 后端示例
│   ├── 06-query-tuning/
│   ├── 07-ha-scaling/
│   ├── 08-ops/
│   └── scripts/
└── .github/workflows/
    ├── ci.yml                     # C 测试 + 文档构建
    └── docs.yml                   # 部署文档站到 GitHub Pages
```

---

## 技术选型

| 项 | 选择 | 理由 |
|---|---|---|
| 语言 | C99 | 贴近 SQLite/PostgreSQL；学到内存/指针/字节布局 |
| 构建 | CMake | 跨平台，C 项目标准 |
| 测试 | Unity | 极简 C 单元测试框架 |
| SQL parser | 手写递归下降 | 教学优先清晰，不依赖 flex/bison 黑盒 |
| 文档站 | MkDocs Material + Doxygen | Markdown 写教程 + C API 自动生成 |
| CI | GitHub Actions | 测试 + 文档自动部署 |
| 阶段2脚本 | Python（uv 管理） | 并发演示和后端示例 |

---

## 工作方式

1. **每章独立分支**：`git checkout -b ch01-storage`，完成后合并 main，便于回溯
2. **每章配套文档**：写代码同时写 `docs/phase1/01-storage.md`，含原理图解 + 设计决策 + 习题
3. **每章配套测试**：测试镜像 src 结构
4. **不缩减原则**：阶段1每章都做完整实现（非玩具版），阶段2每章都有可复现实验