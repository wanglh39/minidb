# 章1：SQLite 深入

> SQLite 是世界上部署最广的数据库——每台手机、每个浏览器都内置。本章从源码级理解 SQLite 架构，与阶段1的 miniDB 逐层对照。

## SQLite 架构总览

```
┌─────────────────────────────────────────────┐
│              SQL Interface                   │
│   (sqlite3_exec, sqlite3_prepare, ...)       │
├─────────────────────────────────────────────┤
│              Tokenizer                       │
│              Parser                          │
│              Code Generator (字节码)          │
├─────────────────────────────────────────────┤
│              Virtual Machine                 │
│           (字节码执行引擎)                    │
├─────────────────────────────────────────────┤
│              B-Tree Layer                    │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐ │
│   │  Pager   │  │  WAL     │  │  Cache   │ │
│   └──────────┘  └──────────┘  └──────────┘ │
├─────────────────────────────────────────────┤
│              OS Interface                    │
│   (文件 I/O, 内存分配, 互斥锁)               │
├─────────────────────────────────────────────┤
│              VFS (Virtual File System)       │
└─────────────────────────────────────────────┘
```

## 与 miniDB 逐层对照

| 层 | miniDB | SQLite | 对照要点 |
|---|---|---|---|
| **SQL 接口** | `parser_parse()` 返回 AST | `sqlite3_prepare()` 返回字节码 | miniDB 直接执行 AST，SQLite 编译为字节码 |
| **词法/语法** | 手写递归下降 | 手写递归下降（LALR-ish） | 两者都手写，无 flex/bison |
| **执行** | Volcano 迭代器 | 字节码虚拟机 | SQLite 用栈式 VM，更紧凑 |
| **B-Tree** | 固定阶 B+Tree | 变长 B-Tree（非 B+Tree） | SQLite 用 B-Tree 而非 B+Tree |
| **页管理** | Slotted Page 4096B | 变长页 512~65536B | SQLite 页大小可配置 |
| **缓存** | LRU/Clock Buffer Pool | mmap 或自己的 page cache | SQLite 默认用 OS mmap |
| **WAL** | 单文件追加 | WAL mode (frame-based) | SQLite WAL 有 checkpoint 机制 |
| **事务** | 2PL + MVCC | WAL + 文件锁 | SQLite 用数据库级锁，非行级 |

## SQLite 存储格式

### 文件头

```
偏移  大小  含义
0     16    魔数: "SQLite format 3\000"
16    2     页大小 (power of 2, 512~65536)
18    1     文件格式写版本
19    1     文件格式读版本
24    4     文件变更计数器
28    4     数据库大小（页数）
...
```

### B-Tree 页

SQLite 的 B-Tree 页有四种类型：

| 类型 | 含义 | 对应 miniDB |
|---|---|---|
| 2 | 内部索引页 | btree internal node |
| 5 | 内部表页 | btree internal node |
| 10 | 叶子索引页 | btree leaf node |
| 13 | 叶子表页 | btree leaf node + heap |

### Record 格式

```
┌─────────┬──────────┬─────────┬─────────┐
│ header  │ col type │ col val │ col val │ ...
│ size    │  codes   │   1     │   2     │
└─────────┴──────────┴─────────┴─────────┘
```

列类型代码（serial type）：

| 代码 | 含义 |
|---|---|
| 0 | NULL |
| 1 | 1-byte int |
| 2 | 2-byte int |
| 3 | 3-byte int |
| 4 | 4-byte int |
| 5 | 6-byte int |
| 6 | 8-byte int |
| 7 | IEEE 754 float |
| 8 | integer 0 (no payload) |
| 9 | integer 1 (no payload) |
| N≥12, even | BLOB of (N-12)/2 bytes |
| N≥13, odd | TEXT of (N-13)/2 bytes |

> **对比 miniDB**：miniDB 用固定类型（INT32/INT64/FLOAT），SQLite 用变长编码，更省空间。

## SQLite WAL 模式

```
主数据库文件 (db.sqlite)
WAL 文件 (db.sqlite-wal)
共享内存文件 (db.sqlite-shm)

写入流程:
  1. 修改的页写入 WAL 文件（追加）
  2. 更新 WAL header 的 mxFrame
  3. 读取时先查 WAL，再查主数据库

Checkpoint:
  1. 将 WAL 中的页写回主数据库
  2. 重置 WAL 文件
  3. 可选: PASSIVE / FULL / RESTART
```

> **对比 miniDB**：miniDB 的 WAL 是 redo-only 日志，SQLite 的 WAL 包含完整页镜像。

## SQLite 执行模型：字节码 VM

```sql
SELECT name FROM users WHERE id = 42;
```

编译为字节码（简化）：

```
Instruction        Arg1    Arg2    Comment
─────────────────────────────────────────────
OpenRead           0       1       打开表 users (rootpage=1)
Integer            42      1       将 42 存入寄存器 R1
NotExists          0       1       如果 R1 对应行不存在, 跳转
Column             0       1       读取 name 列到寄存器
ResultRow          0       1       返回结果行
Close              0               关闭表
Halt
```

> **对比 miniDB**：miniDB 用 Volcano 迭代器（open/next/close），SQLite 用栈式字节码 VM。字节码更紧凑，但调试更难。

## Python 实验：探索 SQLite 内部

```python
# phase2/01-sqlite-deep/sqlite_internals.py
import sqlite3
import struct

# 1. 查看数据库文件头
with open('test.db', 'rb') as f:
    header = f.read(100)
    magic = header[0:16]
    page_size = struct.unpack('>H', header[16:18])[0]
    print(f"Magic: {magic}")
    print(f"Page size: {page_size}")

# 2. 查看页类型
with open('test.db', 'rb') as f:
    f.seek(page_size)  # 跳到第2页
    page = f.read(page_size)
    page_type = page[0]
    types = {2: '内部索引', 5: '内部表', 10: '叶子索引', 13: '叶子表'}
    print(f"Page 2 type: {types.get(page_type, '未知')}")

# 3. SQLite PRAGMA 查询
conn = sqlite3.connect('test.db')
print(conn.execute("PRAGMA page_size").fetchone())
print(conn.execute("PRAGMA page_count").fetchone())
print(conn.execute("PRAGMA journal_mode").fetchone())
print(conn.execute("PRAGMA wal_checkpoint").fetchone())
```

## Python 实验：WAL 模式对比

```python
# phase2/01-sqlite-deep/wal_experiment.py
import sqlite3, time, os

# Rollback journal 模式
conn1 = sqlite3.connect(':memory:')
conn1.execute("PRAGMA journal_mode=DELETE")

# WAL 模式
conn2 = sqlite3.connect('wal_test.db')
conn2.execute("PRAGMA journal_mode=WAL")

# 插入 10000 行对比
for conn, name in [(conn1, 'DELETE'), (conn2, 'WAL')]:
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, val TEXT)")
    t0 = time.time()
    for i in range(10000):
        conn.execute("INSERT INTO t VALUES(?, ?)", (i, f"val_{i}"))
    conn.commit()
    print(f"{name}: {time.time()-t0:.3f}s")
```

## C 扩展示例：自定义 SQL 函数

```c
/* phase2/01-sqlite-deep/sqlite_extension.c */
#include <sqlite3ext.h>
SQLITE_EXTENSION_INIT1

/* 自定义函数: sha256_hex(text) -> hex string */
static void sha256_hex(sqlite3_context *ctx, int argc, sqlite3_value **argv) {
    const unsigned char *text = sqlite3_value_text(argv[0]);
    int len = sqlite3_value_bytes(argv[0]);
    /* ... 计算 SHA256 ... */
    sqlite3_result_text(ctx, result, -1, SQLITE_TRANSIENT);
}

int sqlite3_extension_init(sqlite3 *db, char **err, const char *api) {
    SQLITE_EXTENSION_INIT2(api);
    return sqlite3_create_function(db, "sha256_hex", 1,
                                   SQLITE_UTF8, NULL,
                                   sha256_hex, NULL, NULL);
}
```

加载和使用：

```python
import sqlite3
conn = sqlite3.connect('test.db')
conn.enable_load_extension(True)
conn.load_extension('./sqlite_extension')
print(conn.execute("SELECT sha256_hex('hello')").fetchone())
```

## SQLite 并发模型

| 模式 | 并发度 | 说明 |
|---|---|---|
| **DELETE** (默认) | 低 | 写时全库锁，读时共享锁 |
| **WAL** | 中 | 读不阻塞写，写不阻塞读（但只有一个写者） |
| **MEMORY** | 无 | 全内存，无并发 |

```python
# WAL 模式下的并发读写
import sqlite3, threading

def writer():
    conn = sqlite3.connect('concurrent.db', isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    for i in range(100):
        conn.execute("INSERT INTO t VALUES(?, ?)", (i, f"w{i}"))
        time.sleep(0.001)

def reader():
    conn = sqlite3.connect('concurrent.db', isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    for _ in range(100):
        rows = conn.execute("SELECT count(*) FROM t").fetchone()
        time.sleep(0.001)

# 读写并行执行（WAL 模式下不互相阻塞）
```

> **对比 miniDB**：miniDB 用 2PL 行级锁 + MVCC，SQLite 用库级锁。SQLite 并发度低但实现简单。

## SQLite 限制

| 限制 | 值 | 说明 |
|---|---|---|
| 最大数据库 | 281 TB | 理论值 |
| 单行最大 | 1 GB | BLOB |
| 最大列数 | 32767 | 编译时可调 |
| JOIN 深度 | 64 | 嵌套子查询 |
| 并发写入 | 1 | 即使 WAL 也只支持单写 |

## 文件清单

| 文件 | 内容 |
|---|---|
| `sqlite_internals.py` | 探索 SQLite 文件格式和页结构 |
| `wal_experiment.py` | WAL vs Rollback journal 性能对比 |
| `sqlite_vs_minidb.py` | miniDB 与 SQLite 功能性能对照 |
| `sqlite_extension.c` | C 扩展：自定义 SQL 函数 |

## 习题

1. 用 Python 读取 SQLite 文件头，提取页大小和数据库大小
2. 开启 WAL 模式，用两个线程同时读写，观察是否阻塞
3. 编译 C 扩展，添加 `regex_match(pattern, text)` 函数
4. 对比 miniDB 和 SQLite 插入 10000 行的性能差异