# 章2：PostgreSQL 架构深入

> PostgreSQL 是最先进的开源关系型数据库。本章深入 PG 的进程模型、Shared Buffer、WAL、MVCC 实现，并开发一个 C 扩展。

## 进程模型

```
postmaster (主进程)
  ├── backend (客户端1连接)
  ├── backend (客户端2连接)
  ├── checkpointer
  ├── background writer
  ├── walwriter
  ├── autovacuum launcher
  │     └── autovacuum worker
  ├── stats collector
  └── logical replication
```

| 进程 | 职责 | 对应 miniDB |
|---|---|---|
| **postmaster** | 监听端口，fork backend | 无（单进程） |
| **backend** | 执行 SQL | main.c |
| **checkpointer** | 定期刷脏页 | buffer_pool flush |
| **bgwriter** | 后台刷脏页 | buffer_pool flush |
| **walwriter** | 定期刷 WAL | wal flush |
| **autovacuum** | 自动清理死元组 | 无 |

> **关键区别**：miniDB 是单进程嵌入式，PG 是多进程 C/S 架构。每个连接一个进程（PG 16 也支持线程模式）。

## Shared Memory 架构

```
┌─────────────────────────────────────────┐
│            Shared Memory                │
│  ┌──────────────┐  ┌────────────────┐  │
│  │ Shared       │  │ WAL Buffer     │  │
│  │ Buffers      │  │ (XLog)         │  │
│  │ (8KB × N)    │  │                │  │
│  └──────────────┘  └────────────────┘  │
│  ┌──────────────┐  ┌────────────────┐  │
│  │ Lock Table   │  │ Control File   │  │
│  └──────────────┘  └────────────────┘  │
└─────────────────────────────────────────┘
         ▲                    ▲
         │                    │
    所有 backend          checkpointer
    共享访问              刷入磁盘
```

### Shared Buffers

```sql
-- 查看 shared buffers 配置
SHOW shared_buffers;          -- 默认 128MB
SHOW effective_cache_size;    -- OS 缓存估算

-- 查看 buffer 命中率
SELECT sum(blks_hit) AS hit, sum(blks_read) AS read,
       sum(blks_hit) / (sum(blks_hit) + sum(blks_read)) AS hit_ratio
FROM pg_stat_database;
```

> **对比 miniDB**：miniDB 的 Buffer Pool 在进程内存中，PG 的 Shared Buffers 在共享内存中，所有 backend 进程共享。

## MVCC 实现

### 元组头部

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

### 可见性判断

```
事务 T（快照 S）看到元组 V 的条件：

1. V.xmin 已提交 且 V.xmin 在 S 中（创建者已提交且在快照中）
2. V.xmax == 0 或 V.xmax 未提交 或 V.xmax 不在 S 中（未被删除）

Hint Bits 优化:
  HEAP_XMIN_COMMITTED  → xmin 已确认提交，无需查 CLOG
  HEAP_XMAX_COMMITTED  → xmax 已确认提交，无需查 CLOG
  HEAP_XMIN_INVALID    → xmin 已回滚
```

> **对比 miniDB**：miniDB 的 MVCC 用 xmin/xmax + 快照数组，PG 用 xmin/xmax + CLOG（提交日志）+ 快照。PG 的 CLOG 是位图，比快照数组更紧凑。

### Python 实验：观察 MVCC

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

## VACUUM

### 为什么需要 VACUUM？

```
UPDATE mvcc_demo SET val = 'new' WHERE id = 1;

执行后:
  页中存在:
    版本1: (id=1, val='old', xmax=T1)  ← 死元组
    版本2: (id=1, val='new', xmin=T1)  ← 活元组

死元组不会被自动回收 → 需要VACUUM
```

### VACUUM 流程

```
1. 扫描表，找到死元组
2. 回收死元组占用的空间（标记为可用）
3. 更新可见性映射（visibility map）
4. 更新空闲空间映射（FSM）
5. （VACUUM FULL）重写整个表，物理回收空间
```

### autovacuum 配置

```sql
-- 查看 autovacuum 设置
SHOW autovacuum;                          -- on
SHOW autovacuum_vacuum_threshold;         -- 50
SHOW autovacuum_vacuum_scale_factor;      -- 0.2

-- 意味着: 当死元组 > 50 + 0.2 × 表总行数 时触发 vacuum

-- 查看表的 vacuum 统计
SELECT relname, n_live_tup, n_dead_tup,
       last_autovacuum, autovacuum_count
FROM pg_stat_user_tables;
```

> **对比 miniDB**：miniDB 没有死元组问题（单版本 + 原地更新），PG 的 MVCC 产生死元组需要 VACUUM 回收。

## WAL 架构

```
写入流程:
  1. backend 修改数据页（在 shared buffer 中）
  2. backend 生成 WAL 记录（在 WAL buffer 中）
  3. backend 将 WAL 记录刷入 WAL 文件（fsync）
  4. checkpointer 异步将脏页刷入数据文件

恢复流程 (CRASH):
  1. 读取 WAL 文件
  2. redo: 重放所有已提交但未刷盘的修改
  3. undo: 不需要（MVCC 天然支持）
```

### WAL 配置

```sql
SHOW wal_level;              -- replica (或 minimal, logical)
SHOW max_wal_size;           -- 1GB
SHOW checkpoint_timeout;     -- 5min
SHOW synchronous_commit;     -- on
```

## PG 扩展开发（C）

```c
/* phase2/02-postgresql-deep/extensions/mini_utils.c */
#include "postgres.h"
#include "fmgr.h"

PG_MODULE_MAGIC;

/* 自定义函数: text_reverse(text) -> text */
PG_FUNCTION_INFO_V1(text_reverse);
Datum text_reverse(PG_FUNCTION_ARGS) {
    text *t = PG_GETARG_TEXT_PP(0);
    int len = VARSIZE_ANY_EXHDR(t);
    text *result = palloc(VARHDRSZ + len);

    char *src = VARDATA_ANY(t);
    char *dst = VARDATA(result);
    for (int i = 0; i < len; i++)
        dst[i] = src[len - 1 - i];

    SET_VARSIZE(result, VARHDRSZ + len);
    PG_RETURN_TEXT_P(result);
}
```

安装和使用：

```bash
# 编译扩展
gcc -shared -fPIC -I$(pg_config --includedir-server) \
    -o mini_utils.so mini_utils.c

# 在 PG 中加载
CREATE FUNCTION text_reverse(text) RETURNS text
    AS '/path/to/mini_utils' LANGUAGE C;

SELECT text_reverse('hello');  -- 'olleh'
```

## Python 实验：PG 内部观测

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

## 与 miniDB 的详细对照

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

## 文件清单

| 文件 | 内容 |
|---|---|
| `pg_internals.py` | 观测 PG 内部状态 |
| `mvcc_experiment.py` | MVCC 版本链实验 |
| `vacuum_demo.py` | VACUUM 效果演示 |
| `extensions/mini_utils.c` | PG C 扩展 |

## 习题

1. 用 Python 观察 UPDATE 前后的 xmin/xmax 变化
2. 批量 INSERT 后 DELETE，观察 n_dead_tup 变化，手动 VACUUM
3. 编译安装 C 扩展，添加 `word_count(text)` 函数
4. 对比 PG 和 miniDB 的 MVCC 可见性判断逻辑