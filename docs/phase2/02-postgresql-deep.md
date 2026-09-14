# 章2：PostgreSQL 架构深入

> PostgreSQL 是最先进的开源关系型数据库。本章深入 PG 的进程模型、Shared Buffer、WAL、MVCC 实现，并开发一个 C 扩展。
>
> 本章面向新手，从"PostgreSQL 是什么"讲起，配合大量 ASCII 图解和对比表格，逐步建立对生产级数据库内部结构的直觉。每节末尾都有与 miniDB 的对照，帮助你在"玩具数据库"和"工业数据库"之间架起桥梁。

---

## 目录

1. [PostgreSQL 是什么](#1-postgresql-是什么)
2. [进程模型](#2-进程模型)
3. [Shared Memory 架构](#3-shared-memory-架构)
4. [MVCC 实现](#4-mvcc-实现)
5. [VACUUM](#5-vacuum)
6. [WAL 架构](#6-wal-架构)
7. [PG 扩展开发（C）](#7-pg-扩展开发c)
8. [Python 实验详解](#8-python-实验详解)
9. [与 miniDB 的详细对照](#9-与-minidb-的详细对照)
10. [PostgreSQL 优势](#10-postgresql-优势)
11. [习题](#11-习题)

---

## 1. PostgreSQL 是什么

### 1.1 一句话定义

PostgreSQL（简称 PG）是一个**开源的、对象-关系型的**数据库管理系统。它既支持传统关系模型（表、行、SQL），又支持对象特性（自定义类型、继承、函数重载），还内置了 JSON、数组、地理空间等非关系数据类型。

### 1.2 简史

| 年份 | 事件 | 意义 |
|---|---|---|
| 1986 | 加州大学伯克利分校 Michael Stonebraker 启动 Postgres 项目 | "Post-Ingres" 的缩写，继 Ingres 之后的下一代研究 |
| 1995 | 两位 Berkeley 学生 Andrew Yu 和 Jolly Chen 加入 SQL 解释器，改名 PostgreSQL 95 | 第一次支持 SQL 查询语言 |
| 1996 | 项目更名为 PostgreSQL，正式开源 | 社区接管，版本号 6.0 |
| 2005 | 8.0 版本原生支持 Windows | 之前需要 Cygwin |
| 2010 | 9.0 引入流复制（streaming replication） | 主从复制不再依赖 WAL 文件搬运 |
| 2016 | 9.6 引入并行查询 | 大查询可用多核 |
| 2023 | 16 版本发布，逻辑复制增强 | 持续演进 |

> **新手提示**：Postgres 和 PostgreSQL 是同一个东西。官方发音是 "Post-Gres-Q-L"，但社区里大多数人直接叫 "Postgres"。

### 1.3 核心特点

```
┌──────────────────────────────────────────────────┐
│              PostgreSQL 核心特点                  │
├──────────────────────────────────────────────────┤
│ 1. 标准 SQL 合规性极高（SQL:2016 大部分特性）    │
│ 2. ACID 事务，MVCC 并发控制                      │
│ 3. 丰富的数据类型：JSONB、数组、范围、几何、UUID │
│ 4. 可扩展：自定义类型、操作符、函数、索引方法    │
│ 5. 多种索引：B-Tree、Hash、GiST、GIN、BRIN、SP-GiST│
│ 6. 物理流复制 + 逻辑复制                         │
│ 7. 表分区（声明式分区 10+）                      │
│ 8. 点在时间恢复（PITR）                          │
│ 9. 严格的权限模型（角色、模式、行级安全）        │
│ 10. 跨平台，30+ 年活跃社区                       │
└──────────────────────────────────────────────────┘
```

### 1.4 与 SQLite / MySQL 对比

| 特性 | SQLite | MySQL | PostgreSQL |
|---|---|---|---|
| **架构** | 嵌入式，库文件 | C/S，多线程 | C/S，多进程 |
| **并发模型** | 文件锁，串行写 | 行级锁 + InnoDB MVCC | MVCC（多版本） |
| **标准 SQL** | 大部分 | 方言较多 | 最接近标准 |
| **数据类型** | 基本类型 | 丰富 | 最丰富（JSONB、数组、范围、几何） |
| **索引类型** | B-Tree | B-Tree、Hash、Fulltext | B-Tree、Hash、GiST、GIN、BRIN、SP-GiST |
| **扩展性** | 几乎不可扩展 | 插件有限 | C 扩展 API，高度可扩展 |
| **复制** | 无 | 主从（基于 binlog） | 物理流复制 + 逻辑复制 |
| **分区表** | 无 | 有限 | 声明式分区，成熟 |
| **窗口函数** | 有 | 有 | 有，且支持更多选项 |
| **CTE 递归** | 有 | 8.0+ | 有，且支持递归 + 物化 |
| **JSON 支持** | JSON1 扩展 | JSON 类型 | JSONB（二进制，可索引） |
| **适用场景** | 嵌入式、单机、测试 | Web 应用、中小规模 | 复杂查询、大范围 OLTP/OLAP |

> **新手记忆口诀**：
> - SQLite = "一个文件就是一个数据库"
> - MySQL = "快、简单、Web 之王"
> - PostgreSQL = "功能全、标准严、能扩展"

### 1.5 为什么本章要深入 PG？

miniDB 是我们手写的玩具数据库，用来理解原理。PostgreSQL 是生产级数据库，用来对照验证我们的理解。学完本章你应该能回答：

- miniDB 的单进程模型为什么不能直接用于生产？
- miniDB 的 Buffer Pool 和 PG 的 Shared Buffers 有什么本质区别？
- miniDB 的 MVCC 和 PG 的 MVCC 在存储上有什么不同？
- miniDB 的 WAL 和 PG 的 WAL 在恢复时为什么都不需要 undo？

---

## 2. 进程模型

### 2.1 总览

PostgreSQL 采用**多进程**架构（不是多线程）。一个 `postmaster` 主进程负责监听端口，每来一个客户端连接就 `fork` 一个 `backend` 子进程。后台维护任务（刷脏页、写 WAL、清理死元组）由专门的后台进程承担。

```
                    ┌─────────────────────┐
                    │   客户端 1 (psql)   │
                    └──────────┬──────────┘
                               │ TCP 5432
                               ▼
┌──────────────────────────────────────────────────────────┐
│                    postmaster (主进程)                    │
│                    监听 5432 端口                         │
│                    fork() 出 backend                      │
└──────────────────────────────────────────────────────────┘
        │           │           │           │           │
        ▼           ▼           ▼           ▼           ▼
   ┌────────┐ ┌────────┐ ┌─────────┐ ┌──────────┐ ┌──────────┐
   │backend1│ │backend2│ │backend3 │ │checkpoint│ │ bgwriter │
   │(连接1) │ │(连接2) │ │(连接3)  │ │   er     │ │          │
   └────────┘ └────────┘ └─────────┘ └──────────┘ └──────────┘
        │           │           │
        └─────┬─────┴─────┬─────┘
              ▼           ▼
        ┌──────────┐ ┌──────────┐
        │ walwriter│ │autovacuum│
        │          │ │ launcher │
        └──────────┘ └─────┬────┘
                           │ fork
                           ▼
                     ┌──────────────┐
                     │autovacuum    │
                     │worker (per   │
                     │ table)       │
                     └──────────────┘

    另外还有:
      - stats collector (统计收集，PG 14+ 改为共享内存)
      - logical replication launcher (逻辑复制)
      - walreceiver / walsender (流复制)
      - archiver (WAL 归档)
```

### 2.2 每个进程的职责

| 进程 | 数量 | 职责 | 对应 miniDB | 新手类比 |
|---|---|---|---|---|
| **postmaster** | 1 | 监听端口，fork backend，监控子进程存活 | 无（单进程） | 餐厅大堂经理 |
| **backend** | 每连接 1 个 | 执行 SQL，解析/优化/执行 | `main.c` | 服务员，一桌一个 |
| **checkpointer** | 1 | 定期把脏页刷盘，写 checkpoint 记录 | `buffer_pool flush` | 打烊前清场的保洁 |
| **bgwriter** | 1 | 后台轻量刷脏页，减轻 checkpointer 压力 | 无 | 随时收拾桌子的帮工 |
| **walwriter** | 1 | 定期把 WAL buffer 刷入 WAL 文件 | `wal flush` | 实时记账的会计 |
| **autovacuum launcher** | 1 | 监控各表死元组比例，fork worker | 无 | 巡查卫生的监工 |
| **autovacuum worker** | 按需 | 对单个表执行 VACUUM | 无 | 真正打扫的清洁工 |
| **stats collector** | 1 (PG<14) | 收集统计信息（表大小、死元组等） | 无 | 数据分析师 |
| **archiver** | 0/1 | 归档 WAL 文件到指定目录 | 无 | 档案管理员 |
| **walsender** | 按需 | 流复制中发送 WAL 给备库 | 无 | 快递员 |
| **walreceiver** | 按需 | 备库接收 WAL | 无 | 收货员 |

### 2.3 为什么用多进程而不是多线程？

```
多进程 (PG 的选择)              多线程 (MySQL 的选择)
┌────────┐ ┌────────┐          ┌────────────────────┐
│进程1   │ │进程2   │          │进程                 │
│  线程  │ │  线程  │          │ ┌──────┐ ┌──────┐ │
│  独立  │ │  独立  │          │ │线程1 │ │线程2 │ │
│  地址  │ │  地址  │          │ │共享  │ │共享  │ │
│  空间  │ │  空间  │          │ │地址  │ │地址  │ │
└────────┘ └────────┘          │ └──────┘ └──────┘ │
                                └────────────────────┘

优点:                          优点:
  - 一个进程崩溃不影响其他        - 上下文切换更轻
  - 调试简单 (gdb attach)        - 内存占用更少
  - fork 快 (Copy-on-Write)      - 共享数据无需 IPC

缺点:                          缺点:
  - 连接多时内存大               - 一个线程崩溃全挂
  - 进程间通信靠共享内存          - 锁竞争更复杂
```

> **关键区别**：miniDB 是单进程嵌入式，PG 是多进程 C/S 架构。每个连接一个进程（PG 16 也支持线程模式实验性特性）。

### 2.4 查看进程

```bash
# Linux 下查看 PG 进程
ps aux | grep postgres

# 典型输出:
# postgres  1234  postmaster -D /var/lib/postgresql/data
# postgres  1235  postgres: checkpointer
# postgres  1236  postgres: background writer
# postgres  1237  postgres: walwriter
# postgres  1238  postgres: autovacuum launcher
# postgres  1239  postgres: stats collector
# postgres  1240  postgres: postgres postgres 127.0.0.1(5432) idle
```

```sql
-- 查看当前连接的 backend 进程
SELECT pid, usename, datname, client_addr, state, query
FROM pg_stat_activity;
```

### 2.5 连接池为什么重要？

```
没有连接池:                       有连接池 (pgbouncer):
┌────────┐                        ┌────────┐
│  App   │ 1000 并发连接           │  App   │ 1000 并发请求
└───┬────┘                        └───┬────┘
    │ fork 1000 backend               │ 复用 20 连接
    ▼                                 ▼
┌─────────────┐                  ┌──────────────┐
│ 1000 进程   │                  │  pgbouncer   │
│ 每个几十MB  │                  │  20 个连接   │
│ = 几十GB    │                  └──────┬───────┘
└─────────────┘                         │
                                         ▼
                                   ┌─────────────┐
                                   │  20 backend │
                                   │  内存可控   │
                                   └─────────────┘
```

> **新手提示**：生产环境几乎一定会在 PG 前面加 pgbouncer 或 pgcat，因为每个 backend 进程要占几 MB 到几十 MB 内存。

---

## 3. Shared Memory 架构

### 3.1 为什么需要共享内存？

多进程架构下，所有 backend 需要访问同一份缓存数据（数据页、WAL、锁表）。如果每个进程各自缓存，会：

1. 浪费内存（N 份副本）
2. 数据不一致（A 进程改了页，B 进程看不到）

解决方案：操作系统提供一段**共享内存段**，所有 backend 进程映射到自己的地址空间，访问同一块物理内存。

### 3.2 整体架构图

```
┌─────────────────────────────────────────────────────────┐
│                    Shared Memory (PG)                    │
│                                                          │
│  ┌──────────────┐  ┌────────────────┐  ┌────────────┐  │
│  │  Shared      │  │  WAL Buffer    │  │  Lock       │  │
│  │  Buffers     │  │  (XLog)        │  │  Table      │  │
│  │  (8KB × N)   │  │  (默认 64KB~)  │  │  (LWLock)   │  │
│  │              │  │                │  │             │  │
│  │  数据页缓存  │  │  WAL 记录缓存  │  │  轻锁/表锁  │  │
│  └──────────────┘  └────────────────┘  └────────────┘  │
│                                                          │
│  ┌──────────────┐  ┌────────────────┐  ┌────────────┐  │
│  │  Control     │  │  CLOG          │  │  CommitTS  │  │
│  │  File        │  │  (提交状态位图) │  │  (提交时间) │  │
│  └──────────────┘  └────────────────┘  └────────────┘  │
│                                                          │
│  ┌──────────────┐  ┌────────────────┐  ┌────────────┐  │
│  │  Subtrans    │  │  MultiXact     │  │  Predicate  │  │
│  │  (子事务)    │  │  (多锁)        │  │  Lock       │  │
│  └──────────────┘  └────────────────┘  └────────────┘  │
└─────────────────────────────────────────────────────────┘
        ▲                    ▲                    ▲
        │                    │                    │
   所有 backend         checkpointer          walwriter
   共享读写             刷入数据文件          刷入 WAL 文件
```

### 3.3 Shared Buffers 详解

Shared Buffers 是 PG 的**数据页缓存**，相当于 miniDB 的 Buffer Pool，但放在共享内存里。

```
Shared Buffers 内部结构:
┌─────────────────────────────────────────────────┐
│  Buffer Descriptors Array (BufferDesc)          │
│  ┌─────┬─────┬─────┬─────┬─────┬─────┐         │
│  │  0  │  1  │  2  │  3  │ ... │ N-1 │         │
│  └─────┴─────┴─────┴─────┴─────┴─────┘         │
│    每个 desc 记录:                               │
│    - tag (relfilenode, forknum, blocknum)        │
│    - state (dirty, valid, locked)                │
│    - usage_count (用于 clock sweep 淘汰)         │
└─────────────────────────────────────────────────┘
                       │
                       ▼  指向
┌─────────────────────────────────────────────────┐
│  Buffer Pages (连续内存)                        │
│  ┌────────┬────────┬────────┬────────┐         │
│  │ Page 0 │ Page 1 │ Page 2 │  ...   │         │
│  │ (8KB)  │ (8KB)  │ (8KB)  │        │         │
│  └────────┴────────┴────────┴────────┘         │
└─────────────────────────────────────────────────┘

淘汰算法: Clock Sweep (近似 LRU)
  - 每个 buffer 有 usage_count (0~5)
  - 扫描时: usage_count > 0 则减 1 跳过
  -          usage_count == 0 则选中淘汰
```

```sql
-- 查看 shared buffers 配置
SHOW shared_buffers;          -- 默认 128MB
SHOW effective_cache_size;    -- OS 缓存估算
SHOW wal_buffers;             -- WAL buffer 大小

-- 查看 buffer 命中率
SELECT sum(blks_hit) AS hit, sum(blks_read) AS read,
       sum(blks_hit) / (sum(blks_hit) + sum(blks_read)) AS hit_ratio
FROM pg_stat_database;
```

```
命中率解读:
  hit_ratio > 0.99  →  很好，缓存充足
  hit_ratio 0.90~0.99 → 一般，考虑加大 shared_buffers
  hit_ratio < 0.90  →  差，可能需要调优或加内存
```

### 3.4 WAL Buffer

WAL Buffer 是 WAL 记录的写入缓存，比 Shared Buffers 小得多（默认 64KB 或 -1 自动设为 shared_buffers/32）。

```
WAL 写入路径:
  backend 生成 WAL 记录
        │
        ▼
  ┌─────────────┐
  │ WAL Buffer  │  ← 多 backend 并发写入，用 LWLock 保护
  └──────┬──────┘
         │
         ├── walwriter 定期刷 (异步)
         └── commit 时刷 (synchronous_commit=on 则同步 fsync)
                │
                ▼
         ┌─────────────┐
         │ WAL 文件    │  pg_wal/000000010000000000000001
         └─────────────┘
```

### 3.5 Lock Table

PG 的锁分两类：

```
1. 轻锁 (LWLock - Light Weight Lock)
   - 在共享内存的 Lock Table 中
   - 用于保护内部数据结构（如 buffer desc）
   - 短期持有，不阻塞事务

2. 硬锁 (Regular Lock)
   - 表级锁 (ACCESS SHARE, EXCLUSIVE 等 8 种模式)
   - 行级锁 (FOR UPDATE, FOR SHARE)
   - 长期持有，阻塞事务
```

### 3.6 与 miniDB 进程内缓存对比

| 维度 | miniDB Buffer Pool | PG Shared Buffers |
|---|---|---|
| **位置** | 进程堆内存 | 操作系统共享内存段 |
| **可见性** | 单进程内 | 所有 backend 共享 |
| **大小** | 启动时 malloc | 启动时 shmget/mmap |
| **淘汰** | LRU | Clock Sweep (近似 LRU) |
| **页大小** | 4KB | 8KB (编译时可选) |
| **并发保护** | 不需要（单进程） | LWLock + Pin/Unpin |
| **刷脏** | 同进程 flush | checkpointer + bgwriter |

> **对比 miniDB**：miniDB 的 Buffer Pool 在进程内存中，PG 的 Shared Buffers 在共享内存中，所有 backend 进程共享。这是单进程 vs 多进程的本质差异。

---

## 4. MVCC 实现

### 4.1 MVCC 是什么？

MVCC（Multi-Version Concurrency Control，多版本并发控制）的核心思想：**读不阻塞写，写不阻塞读**。每行数据可能有多个版本，每个事务根据自己的"快照"看到合适的版本。

```
传统锁 (2PL):                    MVCC:
  T1: SELECT * FROM t           T1: SELECT * FROM t
  T2: UPDATE t SET ...  阻塞!   T2: UPDATE t SET ...  不阻塞!
  T1: commit                   T1: commit
  T2: 才能继续                  T2: 创建新版本，T1 看旧版本
```

### 4.2 元组头部字段

PostgreSQL 的每一行（元组）都有一个头部，记录事务信息：

```c
/* PostgreSQL HeapTupleHeaderData (简化) */
struct HeapTupleHeaderData {
    HeapTupleFields t_heap;  /* xmin, xmax */
    ItemPointerData t_ctid;  /* 当前元组ID（更新时指向新版本） */
    uint16 t_infomask2;      /* 属性数量 */
    uint16 t_infomask;       /* 标志位（hint bits） */
    uint8  t_hoff;           /* 头部偏移 */
    /* ... 位图和数据 ... */
};

struct HeapTupleFields {
    TransactionId t_xmin;  /* 插入该版本的事务ID */
    TransactionId t_xmax;  /* 删除/更新该版本的事务ID */
    CommandId    t_cmin;   /* 同事务内的命令序号 */
    CommandId    t_cmax;   /* 同事务内的命令序号 */
};
```

### 4.3 字段含义表

| 字段 | 类型 | 含义 | 何时设置 |
|---|---|---|---|
| **t_xmin** | TransactionId (32 bit) | 插入/创建该版本的事务 ID | INSERT 或 UPDATE 创建新版本时 |
| **t_xmax** | TransactionId (32 bit) | 删除/使该版本失效的事务 ID | DELETE 或 UPDATE 标记旧版本时；0 表示未删除 |
| **t_cmin** | CommandId (32 bit) | 该事务内创建该版本的命令序号 | 同上，用于同事务内可见性 |
| **t_cmax** | CommandId (32 bit) | 该事务内删除该版本的命令序号 | 同上 |
| **t_ctid** | ItemPointerData (6 byte) | 当前元组的位置；更新时指向新版本 | UPDATE 时旧版本指向新版本 |
| **t_infomask** | uint16 | 标志位（hint bits、锁信息等） | 提交/回滚时设置 hint bits |

### 4.4 版本链示例

```
初始: INSERT INTO t VALUES (1, 'A');
  页面:
    ┌──────────────────────────────────┐
    │ (1, 'A')  xmin=T1  xmax=0       │  ← 活元组
    └──────────────────────────────────┘

T2: UPDATE t SET val='B' WHERE id=1;
  页面:
    ┌──────────────────────────────────┐
    │ (1, 'A')  xmin=T1  xmax=T2      │  ← 死元组（T2 提交后）
    │          ctid -> 指向下面       │
    │ (1, 'B')  xmin=T2  xmax=0       │  ← 活元组
    └──────────────────────────────────┘

T3: DELETE FROM t WHERE id=1;
  页面:
    ┌──────────────────────────────────┐
    │ (1, 'A')  xmin=T1  xmax=T2      │  ← 死元组
    │ (1, 'B')  xmin=T2  xmax=T3      │  ← 死元组（T3 提交后）
    └──────────────────────────────────┘
  VACUUM 后:
    ┌──────────────────────────────────┐
    │ (空，空间被回收)                 │
    └──────────────────────────────────┘
```

### 4.5 可见性判断规则

事务 T（持有快照 S）能看到元组 V 的条件：

```
┌─────────────────────────────────────────────────────────────┐
│                  可见性判断流程                              │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  1. 检查 V.xmin:                                             │
│     ├─ xmin 未提交 且 xmin != T → 不可见 (创建者没提交)      │
│     ├─ xmin 已中止           → 不可见 (创建者回滚)           │
│     ├─ xmin > S.xmax         → 不可见 (创建于快照之后)       │
│     ├─ xmin 在 S 的活跃列表中 → 不可见 (创建时还在运行)     │
│     └─ xmin 已提交 且在快照中 → 继续 (创建者可见)           │
│                                                              │
│  2. 检查 V.xmax:                                             │
│     ├─ xmax == 0             → 可见 (未被删除)              │
│     ├─ xmax 未提交 且 xmax != T → 可见 (删除者没提交)       │
│     ├─ xmax 已中止           → 可见 (删除者回滚)            │
│     ├─ xmax > S.xmax         → 可见 (删除于快照之后)        │
│     ├─ xmax 在 S 的活跃列表中 → 可见 (删除时还在运行)       │
│     └─ xmax 已提交 且在快照中 → 不可见 (已被删除)           │
│                                                              │
│  3. 同事务内 (xmin == T 或 xmax == T):                       │
│     用 cmin/cmax 判断该命令之前还是之后                     │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 4.6 快照结构

```c
typedef struct SnapshotData {
    SnapshotType snapshot_type;  // MVMB or SNAPSHOT_MVCC
    TransactionId xmin;          // 快照中最小活跃事务
    TransactionId xmax;          // 快照中最大事务 + 1
    TransactionId *xip;          // 活跃事务列表 (xmin~xmax 之间)
    uint32        xcnt;          // xip 数量
    // ...
} SnapshotData;
```

```
快照示例:
  当前事务 T=100
  运行中的事务: 95, 97, 99
  快照 S:
    S.xmin = 95
    S.xmax = 101  (下一个分配的事务号)
    S.xip  = [95, 97, 99]

  判断元组 (xmin=90, xmax=0):
    90 < 95 → 90 在快照之前，查 CLOG 确认已提交 → 可见

  判断元组 (xmin=96, xmax=0):
    95 <= 96 < 101 → 检查 96 是否在 xip 中 → 不在 → 查 CLOG → 已提交 → 可见

  判断元组 (xmin=97, xmax=0):
    97 在 xip 中 → 不可见 (T=97 还在运行)
```

### 4.7 Hint Bits 优化

每次可见性判断都要查 CLOG（提交日志）会太慢。PG 在元组头部设置 **hint bits**，缓存提交状态：

```
Hint Bits:
  HEAP_XMIN_COMMITTED  → xmin 已确认提交，无需查 CLOG
  HEAP_XMIN_INVALID    → xmin 已回滚
  HEAP_XMAX_COMMITTED  → xmax 已确认提交，无需查 CLOG
  HEAP_XMAX_INVALID    → xmax 已回滚

首次检查:
  1. 元组没有 hint bit → 查 CLOG → 设置 hint bit (写回页面，可能产生 WAL)
  2. 之后检查 → 直接看 hint bit，不查 CLOG

  这是一种"惰性优化"，第一次查完就记住结果
```

### 4.8 CLOG（提交日志）

CLOG 记录每个事务的提交状态，用位图存储，非常紧凑：

```
CLOG 结构:
  每个事务 2 bit:
    00 = 正在运行
    01 = 已提交
    10 = 已回滚
    11 = 已提交 (子事务)

  存储: pg_xact/0000 文件，每 8192 字节一个页
  1 亿事务只需 ~25MB (100000000 * 2 bit / 8 = 25MB)
```

### 4.9 Python 实验：观察 MVCC

```python
import psycopg2

conn = psycopg2.connect("dbname=test user=postgres")
conn.autocommit = False

# 创建表并插入
cur = conn.cursor()
cur.execute("CREATE TABLE mvcc_demo (id int, val text)")
cur.execute("INSERT INTO mvcc_demo VALUES (1, 'original')")
conn.commit()

# 查看元组的 xmin/xmax
cur.execute("""
    SELECT xmin, xmax, * FROM mvcc_demo
""")
print("更新前:", cur.fetchone())

# 更新（创建新版本）
cur.execute("UPDATE mvcc_demo SET val = 'updated' WHERE id = 1")
cur.execute("SELECT xmin, xmax, * FROM mvcc_demo")
print("更新后:", cur.fetchall())  # 旧版本 xmax 被标记，新版本出现

conn.commit()
conn.close()
```

预期输出解读：

```
更新前: (740, 0, 1, 'original')
         │    │   │   │
         │    │   │   └── val 字段
         │    │   └── id 字段
         │    └── xmax=0 表示未被删除
         └── xmin=740 表示插入事务 ID

更新后: [(740, 741, 1, 'original'), (741, 0, 1, 'updated')]
         │    │            │    │
         │    │            │    └── 新版本，xmax=0 可见
         │    │            └── 新版本 xmin=741 (更新事务)
         │    └── 旧版本 xmax=741 (被更新事务标记删除)
         └── 旧版本 xmin=740

注意: 普通 SELECT 只返回可见的行，要看旧版本需用页面检视工具
      如 pg_visibility 扩展或 pageinspect
```

> **对比 miniDB**：miniDB 的 MVCC 用 xmin/xmax + 快照数组，PG 用 xmin/xmax + CLOG（提交日志）+ 快照。PG 的 CLOG 是位图，比快照数组更紧凑，且支持事务回滚后快速判断。

---

## 5. VACUUM

### 5.1 死元组问题

MVCC 的代价：UPDATE 和 DELETE 不会立即回收空间，留下**死元组**（dead tuple）。

```
UPDATE mvcc_demo SET val = 'new' WHERE id = 1;

执行后:
  页中存在:
    版本1: (id=1, val='old', xmax=T1)  ← 死元组
    版本2: (id=1, val='new', xmin=T1)  ← 活元组

死元组不会被自动回收 → 需要VACUUM
```

### 5.2 死元组堆积的危害

```
表文件随 UPDATE/DELETE 增长:
  ┌──────────────────────────────────┐
  │ 活元组 │ 死 │ 活 │ 死死 │ 活 │死│
  └──────────────────────────────────┘
        ↑               ↑
        空间浪费         查询要扫描更多行
                         索引膨胀
                         统计信息失真
```

| 危害 | 说明 |
|---|---|
| **空间浪费** | 表文件只增不减（普通 VACUUM 不归还 OS） |
| **扫描变慢** | 顺序扫描要跳过死元组 |
| **索引膨胀** | 索引指向死元组，增大索引体积 |
| **统计失真** | `n_dead_tup` 影响规划器决策 |

### 5.3 VACUUM 流程

```
VACUUM 内部步骤:
  ┌─────────────────────────────────────────────┐
  │ 1. 扫描表，找到死元组                         │
  │    (利用 visibility map 跳过全可见页)        │
  ├─────────────────────────────────────────────┤
  │ 2. 回收死元组占用的空间                       │
  │    (标记为可用，不归还 OS，留给后续 INSERT)   │
  ├─────────────────────────────────────────────┤
  │ 3. 更新可见性映射 (visibility map)           │
  │    (页全可见 → 标记，索引扫描可跳过版本检查)  │
  ├─────────────────────────────────────────────┤
  │ 4. 更新空闲空间映射 (FSM)                    │
  │    (记录每页可用空间，供 INSERT 使用)        │
  ├─────────────────────────────────────────────┤
  │ 5. 更新统计信息 (pg_stat_user_tables)        │
  ├─────────────────────────────────────────────┤
  │ 6. VACUUM FULL: 重写整个表，物理回收空间     │
  │    (会锁表，需要额外空间)                     │
  └─────────────────────────────────────────────┘
```

### 5.4 VACUUM vs VACUUM FULL vs autovacuum

| 命令 | 锁级别 | 归还空间 | 速度 | 生产可用 |
|---|---|---|---|---|
| `VACUUM` | 共享锁（不阻塞读写） | 否（仅内部复用） | 快 | 经常跑 |
| `VACUUM FULL` | 独占锁（阻塞读写） | 是（重写表） | 慢 | 谨慎，停机时 |
| `VACUUM ANALYZE` | 共享锁 | 否 | 快 | 经常跑 |
| `autovacuum` | 共享锁 | 否 | 后台自动 | 默认开 |

```
VACUUM:
  ┌──┬──┬──┬──┬──┬──┐      ┌──┬──┬──┬──┬──┬──┐
  │活│死│活│死│活│死│  →   │活│  │活│  │活│  │
  └──┴──┴──┴──┴──┴──┘      └──┴──┴──┴──┴──┴──┘
  死元组空间保留，供后续 INSERT 复用

VACUUM FULL:
  ┌──┬──┬──┬──┬──┬──┐      ┌──┬──┬──┐
  │活│死│活│死│活│死│  →   │活│活│活│
  └──┴──┴──┴──┴──┴──┘      └──┴──┴──┘
  重写表，物理缩小，但锁表
```

### 5.5 autovacuum 配置

```sql
-- 查看 autovacuum 设置
SHOW autovacuum;                          -- on
SHOW autovacuum_vacuum_threshold;         -- 50
SHOW autovacuum_vacuum_scale_factor;      -- 0.2
SHOW autovacuum_analyze_threshold;        -- 50
SHOW autovacuum_analyze_scale_factor;     -- 0.1
SHOW autovacuum_vacuum_cost_limit;        -- 200
SHOW autovacuum_vacuum_cost_delay;        -- 2ms

-- 意味着: 当死元组 > 50 + 0.2 × 表总行数 时触发 vacuum
-- 当修改行 > 50 + 0.1 × 表总行数 时触发 analyze

-- 查看表的 vacuum 统计
SELECT relname, n_live_tup, n_dead_tup,
       last_autovacuum, autovacuum_count
FROM pg_stat_user_tables;
```

### 5.6 autovacuum 参数详解表

| 参数 | 默认值 | 说明 |
|---|---|---|
| `autovacuum` | on | 是否启用自动清理 |
| `autovacuum_max_workers` | 3 | 同时运行的 worker 数 |
| `autovacuum_naptime` | 1min | launcher 轮询间隔 |
| `autovacuum_vacuum_threshold` | 50 | 触发 vacuum 的死元组基数 |
| `autovacuum_vacuum_scale_factor` | 0.2 | 死元组比例阈值 |
| `autovacuum_analyze_threshold` | 50 | 触发 analyze 的修改基数 |
| `autovacuum_analyze_scale_factor` | 0.1 | 修改比例阈值 |
| `autovacuum_vacuum_cost_limit` | 200 | 单轮 vacuum 工作量上限 |
| `autovacuum_vacuum_cost_delay` | 2ms | 达到上限后休眠时间 |

### 5.7 单表 autovacuum 调优

```sql
-- 对写多表单独设置更激进的 autovacuum
ALTER TABLE busy_table SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);

-- 查看某表当前设置
SELECT reloptions FROM pg_class WHERE relname = 'busy_table';
```

> **对比 miniDB**：miniDB 没有死元组问题（单版本 + 原地更新），PG 的 MVCC 产生死元组需要 VACUUM 回收。这是 MVCC "读不阻塞写"的代价。

---

## 6. WAL 架构

### 6.1 WAL 是什么？

WAL（Write-Ahead Logging，预写式日志）是 PG 保证持久性的核心机制：**修改数据页之前，先把修改记录写入 WAL**。崩溃后重放 WAL 即可恢复。

### 6.2 WAL 写入流程时序图

```
时间 →
backend:    ───┬─────────────修改数据页(Pin, 加 LWLock)───────────┬───
              │                                                 │
              │  生成 WAL 记录                                  │
              ▼                                                 │
WAL Buffer:  ───┬────写入 WAL 记录 (Copy+LWLock)────┬────────────┬───
                 │                                 │            │
                 │  (commit 时)                    │            │
                 ▼                                 │            │
WAL 文件:     ───┬──── fsync (synchronous_commit=on)───┬───────┬───
                    │                                 │        │
                    │  返回客户端 "commit ok"          │        │
                    ▼                                 ▼        ▼
                                          (异步)
checkpointer: ────────────────────────刷脏页到数据文件──────────────
                                          (不阻塞 backend)
```

### 6.3 写入流程详解

```
写入流程:
  1. backend 修改数据页（在 shared buffer 中）
     - Pin 住 buffer，加 exclusive LWLock
     - 修改页面内容，标记 buffer 为 dirty

  2. backend 生成 WAL 记录（在 WAL buffer 中）
     - 构造 XLogRecord (包含资源管理器 ID、变更数据)
     - 写入 WAL buffer (用 WALWriteLock 保护)

  3. backend 将 WAL 记录刷入 WAL 文件（fsync）
     - synchronous_commit=on: commit 时同步 fsync
     - synchronous_commit=off: 由 walwriter 异步刷

  4. checkpointer 异步将脏页刷入数据文件
     - 定期或 WAL 量达 max_wal_size 时触发
     - 写 checkpoint 记录到 WAL
     - 刷所有脏页到数据文件
```

### 6.4 恢复流程

```
恢复流程 (CRASH):
  ┌─────────────────────────────────────────────┐
  │ 1. 读取 control file，找到上次 checkpoint    │
  │    的 WAL 位置 (redo point)                  │
  ├─────────────────────────────────────────────┤
  │ 2. 从 redo point 开始读取 WAL                │
  ├─────────────────────────────────────────────┤
  │ 3. redo: 重放所有已提交但未刷盘的修改         │
  │    - 按顺序逐条应用 WAL 记录                  │
  │    - 重建数据页到崩溃前状态                  │
  ├─────────────────────────────────────────────┤
  │ 4. undo: 不需要（MVCC 天然支持）             │
  │    - 未提交事务的修改直接忽略即可            │
  │    - 因为它们的数据页不会被 redo (WAL 中有   │
  │      记录但 commit 状态为 aborted)          │
  ├─────────────────────────────────────────────┤
  │ 5. 写新 checkpoint，恢复正常运行             │
  └─────────────────────────────────────────────┘
```

### 6.5 为什么不需要 undo？

```
传统数据库 (2PL):
  T1: 写入页 P (旧值 A → 新值 B)
  T1: 未提交，崩溃
  恢复: 需要 undo，把 P 从 B 恢复回 A

PostgreSQL (MVCC):
  T1: INSERT 新元组 (xmin=T1)
  T1: 未提交，崩溃
  恢复: 查 CLOG 发现 T1 未提交 → 元组不可见 → 无需 undo
        (死元组留给 VACUUM 清理)
```

### 6.6 Checkpoint 机制

```
Checkpoint 的作用: 限制恢复时间

没有 checkpoint:
  WAL: [R1][R2][R3]...[R1000000]
  崩溃后要从头重放 100 万条 → 慢

有 checkpoint:
  WAL: [R1][R2][CK][R3][R4][CK][R5]
                        ↑
                   最近 checkpoint
  崩溃后只重放 [R5] → 快

checkpoint 做了什么:
  1. 把所有 shared buffer 中的脏页刷到数据文件
  2. 在 WAL 中写一条 checkpoint 记录
  3. 更新 control file 记录 redo point
```

### 6.7 WAL 配置

```sql
SHOW wal_level;              -- replica (或 minimal, logical)
SHOW max_wal_size;           -- 1GB (两个 WAL 段之间允许的最大量)
SHOW min_wal_size;           -- 80MB
SHOW checkpoint_timeout;     -- 5min
SHOW checkpoint_completion_target;  -- 0.9
SHOW synchronous_commit;     -- on
SHOW wal_sync_method;        -- fdatasync
SHOW full_page_writes;       -- on
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `wal_level` | replica | minimal/replica/logical，影响记录内容 |
| `max_wal_size` | 1GB | checkpoint 间最大 WAL 量 |
| `checkpoint_timeout` | 5min | 两次 checkpoint 最大间隔 |
| `synchronous_commit` | on | on=commit 等 fsync；off=不等（快但可能丢） |
| `full_page_writes` | on | checkpoint 后首次修改整页写 WAL（防部分写） |

### 6.8 与 miniDB WAL 对比

| 维度 | miniDB WAL | PG WAL |
|---|---|---|
| **格式** | 简单文本/二进制 | 二进制 XLogRecord |
| **内容** | redo only | redo + 逻辑解码 |
| **段大小** | 单文件 | 16MB 段文件循环 |
| **恢复** | 重放 | redo + checkpoint |
| **复制** | 无 | 物理流复制 + 逻辑复制 |
| **归档** | 无 | archive_command 归档 |
| **fsync** | 简单 | 多种 wal_sync_method |

> **对比 miniDB**：miniDB 的 WAL 是 redo-only 单文件，PG 的 WAL 支持逻辑复制、分段、归档、PITR，是生产级实现。

---

## 7. PG 扩展开发（C）

### 7.1 为什么 PG 可扩展？

PG 的设计哲学是"内核做最少的事，把扩展能力暴露出去"。你可以用 C 写：

- 自定义函数（如 `text_reverse`）
- 自定义数据类型（如 `complex`）
- 自定义操作符（如 `@@`）
- 自定义索引方法（如 PostGIS 的 GiST）
- 自定义聚合、窗口函数

### 7.2 C 扩展 API 详解

```c
/* phase2/02-postgresql-deep/extensions/mini_utils.c */
#include "postgres.h"    /* PG 核心头文件 */
#include "fmgr.h"        /* 函数管理器 */

PG_MODULE_MAGIC;         /* 模块魔数，版本兼容检查 */

/* 声明一个版本化函数 */
PG_FUNCTION_INFO_V1(text_reverse);

/* 函数实现: text_reverse(text) -> text */
Datum text_reverse(PG_FUNCTION_ARGS) {
    /* PG_GETARG_TEXT_PP: 取第一个参数 (text 类型，打包格式) */
    text *t = PG_GETARG_TEXT_PP(0);

    /* VARSIZE_ANY_EXHDR: 取变长类型数据长度 (减去头部) */
    int len = VARSIZE_ANY_EXHDR(t);

    /* palloc: PG 内存分配 (自动在内存上下文中，事务结束清理) */
    text *result = palloc(VARHDRSZ + len);

    /* VARDATA_ANY: 取数据指针 (跳过头部) */
    char *src = VARDATA_ANY(t);
    char *dst = VARDATA(result);

    /* 反转字符串 */
    for (int i = 0; i < len; i++)
        dst[i] = src[len - 1 - i];

    /* SET_VARSIZE: 设置变长类型的总长度 */
    SET_VARSIZE(result, VARHDRSZ + len);

    /* PG_RETURN_TEXT_P: 返回 text 类型 */
    PG_RETURN_TEXT_P(result);
}
```

### 7.3 关键 API 宏表

| 宏 | 作用 |
|---|---|
| `PG_MODULE_MAGIC` | 模块魔数，防止版本不匹配 |
| `PG_FUNCTION_INFO_V1(func)` | 声明版本 1 函数 |
| `PG_GETARG_TEXT_PP(n)` | 取第 n 个 text 参数（打包格式） |
| `PG_GETARG_INT32(n)` | 取第 n 个 int32 参数 |
| `PG_RETURN_TEXT_P(x)` | 返回 text 类型 |
| `PG_RETURN_INT32(x)` | 返回 int32 类型 |
| `VARSIZE_ANY_EXHDR(t)` | 变长类型数据长度（不含头） |
| `VARDATA_ANY(t)` | 变长类型数据指针 |
| `SET_VARSIZE(t, n)` | 设置变长类型总长度 |
| `palloc(size)` | 在当前内存上下文分配 |
| `pfree(ptr)` | 释放内存 |

### 7.4 编译安装步骤

```bash
# 1. 确保有 PG 开发头文件
pg_config --includedir-server
# 输出: /usr/include/postgresql/15/server

# 2. 编译扩展为共享库
gcc -shared -fPIC -I$(pg_config --includedir-server) \
    -o mini_utils.so mini_utils.c

# 3. 把 .so 放到 PG 的 lib 目录
cp mini_utils.so $(pg_config --pkglibdir)/

# 4. 在 PG 中注册函数
psql -d test -c "
CREATE FUNCTION text_reverse(text) RETURNS text
    AS 'mini_utils' LANGUAGE C;
"

# 5. 测试
psql -d test -c "SELECT text_reverse('hello');"
#  olleh
```

### 7.5 使用 PGXS 构建系统（推荐）

```
# Makefile
MODULES = mini_utils
PG_CONFIG = pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
```

```bash
make && make install
psql -d test -c "CREATE EXTENSION mini_utils;  -- 需要 mini_utils.control 文件"
```

### 7.6 mini_utils.c 代码解读

逐行解读上面的 `text_reverse` 函数：

1. `#include "postgres.h"` — 引入 PG 核心类型定义（`text`、`Datum` 等）
2. `PG_MODULE_MAGIC` — 编译时写入版本信息，加载时检查兼容性
3. `PG_FUNCTION_INFO_V1(text_reverse)` — 声明这是"版本 1"函数调用约定
4. `Datum text_reverse(PG_FUNCTION_ARGS)` — 所有 PG C 函数返回 `Datum`（PG 的通用值类型）
5. `PG_GETARG_TEXT_PP(0)` — 取第 0 个参数，`_PP` 表示支持"打包"格式（节省空间）
6. `palloc` — PG 的内存分配器，在"当前内存上下文"中分配，事务结束时自动清理
7. `VARDATA_ANY` / `SET_VARSIZE` — 操作变长类型的宏，处理头部
8. `PG_RETURN_TEXT_P` — 把 `text *` 包装成 `Datum` 返回

> **新手提示**：PG 的 C 扩展 API 看起来复杂，但核心就三件事：**取参数 → 处理 → 返回结果**。宏帮你处理类型转换和内存管理。

---

## 8. Python 实验详解

### 8.1 pg_internals.py — 观测 PG 内部状态

```python
# phase2/02-postgresql-deep/pg_internals.py
import psycopg2

conn = psycopg2.connect("dbname=postgres user=postgres")
cur = conn.cursor()

# 1. 查看数据库大小和页数
cur.execute("""
    SELECT pg_size_pretty(pg_database_size('postgres')),
           current_setting('block_size')
""")
print("DB size, block size:", cur.fetchone())

# 2. 查看缓冲池命中率
cur.execute("""
    SELECT blks_hit, blks_read,
           round(blks_hit::numeric / nullif(blks_hit + blks_read, 0), 4)
    FROM pg_stat_database WHERE datname = 'postgres'
""")
print("Buffer hit ratio:", cur.fetchone())

# 3. 查看 WAL 位置
cur.execute("SELECT pg_current_wal_lsn(), pg_walfile_name(pg_current_wal_lsn())")
print("Current WAL:", cur.fetchone())

# 4. 查看死元组
cur.execute("""
    SELECT relname, n_live_tup, n_dead_tup,
           round(n_dead_tup::numeric / nullif(n_live_tup, 0), 4)
    FROM pg_stat_user_tables
    WHERE n_dead_tup > 0
    ORDER BY n_dead_tup DESC
""")
print("Dead tuples:", cur.fetchall())
```

**用法**：

```bash
python pg_internals.py
```

**输出解读**：

```
DB size, block size: ('8 MB', '8192')
  → 数据库当前 8MB，页大小 8192 字节 (8KB)

Buffer hit ratio: (Decimal('1234'), Decimal('56'), Decimal('0.9566'))
  → 命中 1234 次，读盘 56 次，命中率 95.66%
  → 95% 以上算正常，低于 90% 考虑加大 shared_buffers

Current WAL: ('0/1A2B3C4D', '000000010000000000000001')
  → 当前 WAL LSN (Log Sequence Number)
  → 对应 WAL 文件名 000000010000000000000001

Dead tuples: [('mvcc_demo', 100, 50, Decimal('0.5000'))]
  → mvcc_demo 表有 100 活元组、50 死元组
  → 死/活 = 50%，该 VACUUM 了
```

### 8.2 mvcc_experiment.py — MVCC 版本链实验

```python
# phase2/02-postgresql-deep/mvcc_experiment.py
import psycopg2
import time

conn = psycopg2.connect("dbname=test user=postgres")
conn.autocommit = True
cur = conn.cursor()

# 准备
cur.execute("DROP TABLE IF EXISTS mvcc_exp")
cur.execute("CREATE TABLE mvcc_exp (id int, val text)")

# 实验 1: INSERT 后看 xmin
cur.execute("INSERT INTO mvcc_exp VALUES (1, 'v1')")
cur.execute("SELECT xmin, xmax, id, val FROM mvcc_exp")
print("INSERT 后:", cur.fetchone())

# 实验 2: UPDATE 后看版本链
cur.execute("UPDATE mvcc_exp SET val='v2' WHERE id=1")
cur.execute("SELECT xmin, xmax, id, val FROM mvcc_exp")
print("UPDATE 后 (可见行):", cur.fetchone())

# 实验 3: 用 pageinspect 看页面内所有版本
cur.execute("""
    SELECT t_xmin, t_xmax, t_ctid, t_data
    FROM heap_page_items(get_raw_page('mvcc_exp', 0))
""")
print("页面内所有版本:")
for row in cur.fetchall():
    print("  ", row)

# 实验 4: 开两个事务，演示快照隔离
conn1 = psycopg2.connect("dbname=test user=postgres")
conn2 = psycopg2.connect("dbname=test user=postgres")
conn1.autocommit = False
conn2.autocommit = False

cur1 = conn1.cursor()
cur2 = conn2.cursor()

# T2 修改但未提交
cur2.execute("UPDATE mvcc_exp SET val='uncommitted' WHERE id=1")

# T1 应该看到旧值 (v2)
cur1.execute("SELECT val FROM mvcc_exp WHERE id=1")
print("T1 看到的值 (T2 未提交):", cur1.fetchone())

conn2.rollback()
conn1.commit()
```

**输出解读**：

```
INSERT 后: (740, 0, 1, 'v1')
  → xmin=740 (插入事务), xmax=0 (未删除)

UPDATE 后 (可见行): (741, 0, 1, 'v2')
  → xmin=741 (更新事务), xmax=0 (新版本可见)
  → 旧版本 (xmin=740, xmax=741) 不出现在普通 SELECT 中

页面内所有版本:
   (740, 741, (0,2), b'...')  ← 旧版本，被 741 删除
   (741, 0,   (0,2), b'...')  ← 新版本，活
  → pageinspect 能看到页面里所有版本，包括死元组

T1 看到的值 (T2 未提交): ('v2',)
  → 快照隔离：T1 不看 T2 未提交的修改
```

### 8.3 vacuum_demo.py — VACUUM 效果演示

```python
# phase2/02-postgresql-deep/vacuum_demo.py
import psycopg2

conn = psycopg2.connect("dbname=test user=postgres")
conn.autocommit = True
cur = conn.cursor()

cur.execute("DROP TABLE IF EXISTS vacuum_demo")
cur.execute("CREATE TABLE vacuum_demo (id int, val text)")

# 插入 10000 行
cur.execute("INSERT INTO vacuum_demo SELECT i, 'v' FROM generate_series(1, 10000) i")

# 查看表大小
cur.execute("SELECT pg_size_pretty(pg_table_size('vacuum_demo'))")
print("初始大小:", cur.fetchone())

# 更新所有行（产生 10000 死元组）
cur.execute("UPDATE vacuum_demo SET val = 'updated'")

cur.execute("""
    SELECT n_live_tup, n_dead_tup
    FROM pg_stat_user_tables WHERE relname = 'vacuum_demo'
""")
print("UPDATE 后:", cur.fetchone())

cur.execute("SELECT pg_size_pretty(pg_table_size('vacuum_demo'))")
print("UPDATE 后大小:", cur.fetchone())

# 手动 VACUUM
cur.execute("VACUUM vacuum_demo")

cur.execute("""
    SELECT n_live_tup, n_dead_tup
    FROM pg_stat_user_tables WHERE relname = 'vacuum_demo'
""")
print("VACUUM 后:", cur.fetchone())

cur.execute("SELECT pg_size_pretty(pg_table_size('vacuum_demo'))")
print("VACUUM 后大小:", cur.fetchone())

# VACUUM FULL 物理回收
cur.execute("VACUUM FULL vacuum_demo")
cur.execute("SELECT pg_size_pretty(pg_table_size('vacuum_demo'))")
print("VACUUM FULL 后大小:", cur.fetchone())
```

**输出解读**：

```
初始大小: ('640 kB',)
UPDATE 后: (10000, 10000)        ← 10000 活 + 10000 死
UPDATE 后大小: ('1280 kB',)      ← 翻倍（死元组占空间）
VACUUM 后: (10000, 0)            ← 死元组清零
VACUUM 后大小: ('1280 kB',)      ← 没变！普通 VACUUM 不归还 OS
VACUUM FULL 后大小: ('640 kB',)  ← 缩小，物理回收
```

> **关键观察**：普通 `VACUUM` 清理死元组但不缩小文件；`VACUUM FULL` 缩小文件但锁表。生产环境靠 autovacuum 维护，极少用 VACUUM FULL。

---

## 9. 与 miniDB 的详细对照

### 9.1 总览表

| 组件 | miniDB | PostgreSQL | 设计差异原因 |
|---|---|---|---|
| **进程模型** | 单进程 | 多进程 | PG 需要并发连接 |
| **缓冲池** | 进程内 | 共享内存 | 多进程共享数据 |
| **锁** | 行级 2PL | 表级+行级 | PG 用表锁+SI 锁 |
| **MVCC** | 快照数组 | CLOG+快照 | PG 优化存储 |
| **死元组** | 无 | 需要 VACUUM | MVCC 代价 |
| **WAL** | redo-only | redo+逻辑 | PG 支持逻辑复制 |
| **索引** | B+Tree | B-Tree, Hash, GiST, GIN, BRIN | PG 多种访问方法 |
| **扩展** | 无 | C 扩展 API | PG 可扩展性 |

### 9.2 进程模型对照

| 维度 | miniDB | PostgreSQL |
|---|---|---|
| 架构 | 单进程嵌入式 | 多进程 C/S |
| 连接 | 函数调用 | TCP 5432 |
| 并发 | 串行 | 多 backend 并发 |
| 隔离 | 不需要 | 共享内存 + LWLock |
| 崩溃影响 | 整个进程 | 单 backend 崩溃不影响其他 |

### 9.3 缓存对照

| 维度 | miniDB Buffer Pool | PG Shared Buffers |
|---|---|---|
| 位置 | 进程堆内存 | OS 共享内存段 |
| 大小配置 | 启动参数 | `shared_buffers` |
| 淘汰算法 | LRU | Clock Sweep |
| 页大小 | 4KB | 8KB |
| 并发保护 | 无 | Pin + LWLock |
| 刷脏 | 同进程 | checkpointer + bgwriter |
| 命中率查询 | 内部变量 | `pg_stat_database.blks_hit/read` |

### 9.4 锁对照

| 维度 | miniDB | PostgreSQL |
|---|---|---|
| 表锁 | 无 | 8 种模式 (ACCESS SHARE..ACCESS EXCLUSIVE) |
| 行锁 | 2PL (FOR UPDATE) | 存储在元组头 infomask |
| 死锁检测 | 超时 | 等待图周期检测 |
| 软锁 | 无 | LWLock (保护内部结构) |
| 谓词锁 | 无 | SSI (可串行化快照隔离) |

### 9.5 MVCC 对照

| 维度 | miniDB | PostgreSQL |
|---|---|---|
| 版本存储 | xmin/xmax 字段 | 元组头 t_heap |
| 提交状态 | 快照数组 | CLOG 位图 |
| 快照 | 活跃事务数组 | xmin/xmax + xip 数组 |
| 可见性判断 | 查快照数组 | 查 CLOG + hint bits |
| 死元组清理 | 无（原地更新） | VACUUM |
| 回滚 | 标记 xmax | CLOG 标记 aborted |

### 9.6 WAL 对照

| 维度 | miniDB | PostgreSQL |
|---|---|---|
| 格式 | 简单记录 | XLogRecord 二进制 |
| 段管理 | 单文件 | 16MB 段循环 |
| checkpoint | 简单 | 完整机制 |
| 恢复 | 重放 | redo + 崩溃恢复 |
| 复制 | 无 | 物理 + 逻辑 |
| 归档 | 无 | archive_command |
| PITR | 无 | 支持 |

### 9.7 索引对照

| 维度 | miniDB | PostgreSQL |
|---|---|---|
| B-Tree | 有 | 有 (默认) |
| Hash | 无 | 有 |
| GiST | 无 | 有 (平衡树，地理/全文) |
| GIN | 无 | 有 (倒排，数组/全文) |
| BRIN | 无 | 有 (块范围，时序数据) |
| SP-GiST | 无 | 有 (空间分区) |
| 并发创建 | 无 | 有 (CONCURRENTLY) |
| 部分索引 | 无 | 有 (WHERE 条件) |
| 表达式索引 | 无 | 有 (f(x)) |

### 9.8 扩展对照

| 维度 | miniDB | PostgreSQL |
|---|---|---|
| 自定义函数 | 无 | C / PL/pgSQL / PL/Python 等 |
| 自定义类型 | 无 | CREATE TYPE |
| 自定义操作符 | 无 | CREATE OPERATOR |
| 自定义索引 | 无 | Index Access Method |
| 自定义聚合 | 无 | CREATE AGGREGATE |
| 扩展包管理 | 无 | CREATE EXTENSION |

---

## 10. PostgreSQL 优势

### 10.1 扩展性

PG 被称为"数据库界的乐高"。你可以用 C 写扩展，几乎能做任何事：

```
扩展能力层级:
  ┌─────────────────────────────────────────┐
  │ 自定义索引方法 (如 PostGIS, pgvector)   │  ← 最强
  ├─────────────────────────────────────────┤
  │ 自定义数据类型 + 操作符                 │
  ├─────────────────────────────────────────┤
  │ 自定义函数 (C / PL/pgSQL / PL/Python)   │
  ├─────────────────────────────────────────┤
  │ 自定义聚合、窗口函数                    │
  ├─────────────────────────────────────────┤
  │ 触发器、规则、事件                      │
  └─────────────────────────────────────────┘
```

知名扩展：PostGIS（地理）、pgvector（向量）、TimescaleDB（时序）、Citus（分布式）、pg_partman（分区管理）。

### 10.2 标准 SQL 合规

PG 对 SQL 标准的支持是开源库中最完整的：

| 特性 | 支持 |
|---|---|
| 窗口函数 | ✓ (RANK, LAG, LEAD, NTILE...) |
| CTE (WITH) | ✓ (递归 + 物化) |
| MERGE | ✓ (15+) |
| FETCH FIRST | ✓ |
| SAVEPOINT | ✓ |
| 行级安全 (RLS) | ✓ |
| 谓词锁 (SERIALIZABLE) | ✓ (真正的可串行化) |
| 数组类型 | ✓ |
| 范围类型 | ✓ |
| 域类型 | ✓ |

### 10.3 社区生态

```
PostgreSQL 社区:
  - 30+ 年历史，最老牌开源数据库之一
  - 每年一个大版本 (9.x → 10 → 11 → ... → 16)
  - 核心团队 + 贡献者模式，质量极高
  - 邮件列表驱动 (不是 GitHub PR)
  - 商业支持: EDB, 2ndQuadrant, Crunchy Data, AWS, GCP
  - 云服务: AWS RDS/Aurora, GCP Cloud SQL, Azure, Supabase
```

### 10.4 JSON 支持

```sql
-- JSONB: 二进制 JSON，可索引，高效
CREATE TABLE events (id serial, data jsonb);
INSERT INTO events (data) VALUES
    ('{"type": "login", "user": "alice", "ip": "1.2.3.4"}'),
    ('{"type": "purchase", "user": "bob", "amount": 99.9}');

-- 查询 JSON 字段
SELECT data->>'user' AS user_name
FROM events
WHERE data->>'type' = 'login';

-- JSON 索引
CREATE INDEX ON events USING gin (data jsonb_path_ops);

-- JSON 路径查询 (PG 12+)
SELECT * FROM events WHERE data @> '{"user": "alice"}';
```

### 10.5 分区表

```sql
-- 声明式分区 (PG 10+)
CREATE TABLE measurements (
    id serial,
    measured_at timestamp,
    value numeric
) PARTITION BY RANGE (measured_at);

-- 按月分区
CREATE TABLE measurements_2024_01 PARTITION OF measurements
    FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');
CREATE TABLE measurements_2024_02 PARTITION OF measurements
    FOR VALUES FROM ('2024-02-01') TO ('2024-03-01');

-- 查询自动只扫描相关分区 (分区裁剪)
SELECT * FROM measurements
WHERE measured_at BETWEEN '2024-01-15' AND '2024-01-20';
```

### 10.6 其他优势速览

| 优势 | 说明 |
|---|---|
| **点在时间恢复 (PITR)** | 恢复到任意时间点 |
| **逻辑复制** | 订阅特定表，灵活 |
| **并行查询** | 9.6+ 大查询多核 |
| **JIT 编译** | 11+ LLVM 加速表达式 |
| **连接池** | pgbouncer 成熟方案 |
| **监控** | pg_stat_* 视图丰富 |
| **安全** | RLS、SCRAM、SSL、LDAP |

---

## 文件清单

| 文件 | 内容 |
|---|---|
| `pg_internals.py` | 观测 PG 内部状态 |
| `mvcc_experiment.py` | MVCC 版本链实验 |
| `vacuum_demo.py` | VACUUM 效果演示 |
| `extensions/mini_utils.c` | PG C 扩展 |

---

## 11. 习题

1. **MVCC 观察**：用 Python 连接 PG，创建表并执行 INSERT/UPDATE/DELETE，每次用 `SELECT xmin, xmax, * FROM t` 观察变化。解释为什么 UPDATE 后旧行的 xmax 不为 0。

2. **死元组与 VACUUM**：批量 INSERT 10000 行后 DELETE 一半，观察 `pg_stat_user_tables.n_dead_tup` 变化。手动执行 `VACUUM` 和 `VACUUM FULL`，对比表大小变化，解释为什么普通 VACUUM 不缩小文件。

3. **C 扩展开发**：编译安装 `mini_utils.c` 中的 `text_reverse` 函数，然后仿照它写一个 `word_count(text)` 函数，返回文本中单词数量。提示：用 `VARSIZE_ANY_EXHDR` 取长度，遍历统计空格。

4. **可见性判断**：对比 PG 和 miniDB 的 MVCC 可见性判断逻辑。画出 PG 中事务 T (快照 S) 判断元组 V 可见的完整流程图，标注 CLOG 查询和 hint bits 优化的位置。

5. **进程模型**：启动 PG，用 `ps aux | grep postgres` 列出所有进程，识别每个进程的角色。然后开两个 psql 连接，观察 backend 进程数量变化。思考：为什么 PG 不像 MySQL 那样用多线程？

6. **WAL 恢复**：在 PG 中执行若干写操作，用 `pg_current_wal_lsn()` 观察 LSN 变化。然后强制 `pg_ctl stop -m immediate`（模拟崩溃）再启动，观察日志中的恢复过程。解释为什么 PG 恢复不需要 undo。

7. **autovacuum 调优**：创建一张表，设置 `autovacuum_vacuum_scale_factor = 0.05`，持续 UPDATE 制造死元组，观察 autovacuum 何时触发。对比默认 0.2 的表，记录触发频率差异。

8. **JSON 与分区**：用 JSONB 存储一组带时间戳的事件，建 GIN 索引。然后按月做声明式分区，测试分区裁剪（`EXPLAIN` 观察只扫描相关分区）。思考：miniDB 要支持这两个特性分别需要哪些改动？

---

> **本章小结**：PostgreSQL 用多进程 + 共享内存支撑并发，用 MVCC + CLOG 实现"读不阻塞写"，用 VACUUM 回收死元组，用 WAL + checkpoint 保证持久性，用 C 扩展 API 提供极致可扩展性。miniDB 实现了这些机制的简化版本，对照学习能帮你理解"生产级数据库为什么这么设计"。
>
> 下一章我们将深入索引内部结构，对比 miniDB 的 B+Tree 和 PG 的多种索引方法。
