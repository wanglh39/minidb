# 章5：WAL 与崩溃恢复

> **核心问题**：断电后数据库怎么恢复？WAL（预写日志，Write-Ahead Log）是答案。
> 所有修改先写日志再改数据，崩溃后重做日志即可恢复。
>
> **本章你将学到**：
> - 为什么数据库不会因为断电丢数据
> - WAL 的"先写日志"原则到底为什么安全
> - LSN、Redo、Checkpoint、ARIES 算法等核心概念
> - miniDB 的 `wal.c` 和 `recovery.c` 每一行代码的含义
> - 如何亲手模拟一次"崩溃 + 恢复"的完整过程

---

## 目录

1. [崩溃恢复问题：为什么需要恢复](#1-崩溃恢复问题为什么需要恢复)
2. [WAL 原理：先写日志，再改数据](#2-wal-原理先写日志再改数据)
3. [日志记录类型：BEGIN/COMMIT/UPDATE/ABORT/CHECKPOINT](#3-日志记录类型begincommitupdateabortcheckpoint)
4. [LSN 概念：日志序号](#4-lsn-概念日志序号)
5. [Redo 流程：崩溃后怎么恢复](#5-redo-流程崩溃后怎么恢复)
6. [Checkpoint：缩短恢复时间](#6-checkpoint缩短恢复时间)
7. [ARIES 算法：工业级恢复标准](#7-aries-算法工业级恢复标准)
8. [fsync 的重要性： fflush ≠ fsync](#8-fsync-的重要性fflush--fsync)
9. [代码逐行解读](#9-代码逐行解读)
10. [实战演示：模拟崩溃和恢复](#10-实战演示模拟崩溃和恢复)
11. [与真实数据库对比](#11-与真实数据库对比)
12. [习题](#12-习题)

---

## 1. 崩溃恢复问题：为什么需要恢复

### 1.1 一个让人失眠的问题

假设你正在运营一个银行数据库，某笔交易刚刚执行：

```
转账：Alice → Bob  100 元
  1. Alice 余额：1000 → 900
  2. Bob   余额：500 → 600
  3. 事务提交成功 ✓
  4. 💥 就在这一瞬间，机房断电
```

机器重新启动后，数据库文件里 Alice 和 Bob 的余额到底是多少？

| 可能的结果 | 是否正确 | 说明 |
|---|---|---|
| Alice=900, Bob=600 | ✓ 正确 | 事务完整生效 |
| Alice=1000, Bob=500 | ✓ 可接受 | 事务整体回滚，钱没丢 |
| Alice=900, Bob=500 | ✗ 灾难 | 钱凭空消失 100 元！|
| Alice=1000, Bob=600 | ✗ 灾难 | 钱凭空多出 100 元！|

数据库最不能容忍的就是后两种情况——**数据不一致**。宁可整笔回滚，也不能让数据"半新半旧"。

### 1.2 崩溃的两种场景

#### 场景一：进程崩溃（kill -9、段错误、OOM）

```
┌──────────────────────────────────────────────┐
│              用户进程（数据库）              │
│  ┌──────────────────────────────────────┐   │
│  │  内存中的 Buffer Pool（脏页）        │   │
│  │  page0: Alice=900  ← 已改但没刷盘    │   │
│  │  page1: Bob=600    ← 已改但没刷盘    │   │
│  └──────────────────────────────────────┘   │
│                      │                       │
│                      │ write()               │
│                      ▼                       │
│  ┌──────────────────────────────────────┐   │
│  │  OS 内核 Page Cache（可能已写入）    │   │
│  └──────────────────────────────────────┘   │
└──────────────────────────────────────────────┘
                      │
                      │ fsync / 自然刷盘
                      ▼
              ┌───────────────┐
              │   磁盘文件     │
              │  Alice=1000   │  ← 还没更新
              │  Bob=500      │  ← 还没更新
              └───────────────┘

💥 kill -9 进程
   → 内存中的脏页全部丢失
   → 磁盘上还是旧数据
   → 重启后看到 Alice=1000, Bob=500（事务整体没生效，可接受）
```

进程崩溃相对**温和**：OS 内核 Page Cache 还在，重启后 OS 会把已 `write()` 的数据刷到磁盘。但如果进程还没来得及 `write()`，那修改就丢了。

#### 场景二：断电 / 内核 panic / 硬件故障

```
┌──────────────────────────────────────────────┐
│              用户进程（数据库）              │
│  ┌──────────────────────────────────────┐   │
│  │  内存中的 Buffer Pool（脏页）        │   │
│  │  page0: Alice=900                    │   │
│  │  page1: Bob=600                      │   │
│  └──────────────────────────────────────┘   │
└──────────────────────────────────────────────┘
                      │ write()
                      ▼
┌──────────────────────────────────────────────┐
│  OS 内核 Page Cache                         │
│  Alice=900  ← write() 已写入，但还没 fsync  │
│  Bob=600                                  │
└──────────────────────────────────────────────┘
                      │
                      │ ❌ 还没 fsync，没到磁盘
                      ▼
              ┌───────────────┐
              │   磁盘文件     │
              │  Alice=1000   │  ← 旧数据
              │  Bob=500      │  ← 旧数据
              └───────────────┘

💥 断电！
   → 内存 + OS Page Cache 全部丢失
   → 磁盘上只剩旧数据 Alice=1000, Bob=500
   → 重启后事务"消失"了
```

断电是**最严酷**的崩溃场景：所有易失性存储（内存、OS Cache）全部清零，只有真正写到磁盘的数据才能幸存。

### 1.3 为什么"直接改数据文件"不行？

新手直觉：**事务提交时，直接把新数据写到数据文件，然后 fsync，不就安全了？**

听起来对，但有致命问题：

```
事务 T1 修改了 3 个页：page0, page5, page12

写盘顺序（即使 fsync 每个页）：
  1. write page0  → fsync ✓
  2. write page5  → fsync ✓
  3. 💥 写 page12 之前断电

结果：page0 和 page5 是新数据，page12 是旧数据
     → 数据不一致！事务"做了一半"
```

**问题本质**：一个事务可能修改多个页，无法保证"要么全部刷盘成功，要么全部不刷盘"——磁盘写是**非原子**的（对一个扇区原子，对多个页不原子）。

### 1.4 WAL 的解决思路

WAL 的核心思想：**把"要做什么"先记到日志里，日志是顺序追加的，可以保证原子；然后再慢慢改数据文件。**

```
事务 T1 修改 page0, page5, page12：

步骤 1：写 WAL 日志（顺序追加，一次 fsync）
  WAL: [BEGIN T1][UPDATE page0 ...][UPDATE page5 ...][UPDATE page12 ...][COMMIT T1]
  fsync(WAL) ✓  ← 日志完整落盘

步骤 2：修改 Buffer Pool 中的 page0, page5, page12（内存）
  → 可以延迟刷盘，不着急

步骤 3：后台慢慢把脏页刷到数据文件
  → 刷一半断电也没关系，重启时按 WAL 重做即可
```

**为什么安全**？

- 日志已完整落盘 → 崩溃后能读到完整的"操作记录"
- 数据页没刷完没关系 → 重启时按日志重做（redo）即可
- 日志是**顺序追加**，一次 fsync 就能保证整个事务原子持久化

这就是 WAL 的精髓。下面我们展开讲。

---

## 2. WAL 原理：先写日志，再改数据

### 2.1 "Write-Ahead"的"ahead"到底是什么意思

Write-Ahead Logging，直译"提前写日志"。**提前**是相对于谁提前？

> **ahead 的含义**：在把数据页的修改写到磁盘**之前**，必须先把对应的日志写到磁盘。

注意是"数据页写到磁盘之前"，不是"修改 Buffer Pool 之前"。Buffer Pool 在内存里，可以先改；但脏页刷盘之前，日志必须先落盘。

```
合法顺序：
  1. 写 WAL 日志            （内存）
  2. fsync WAL             （日志落盘）  ← 关键！
  3. 修改 Buffer Pool 脏页  （内存）
  4. 把脏页刷到数据文件      （磁盘）      ← 此时日志早已落盘

非法顺序（会丢数据）：
  1. 修改 Buffer Pool 脏页  （内存）
  2. 把脏页刷到数据文件      （磁盘）      ← 日志还没落盘！
  3. 写 WAL 日志
  4. fsync WAL
  💥 若步骤 2 之后、步骤 4 之前断电：
     数据文件已是新数据，但日志没有记录 → 无法 redo → 无法恢复
```

### 2.2 为什么"先写日志"就安全了

核心论证：

```
定理：若 WAL 规则被遵守（脏页刷盘前日志已落盘），则崩溃后总能恢复到一致状态。

证明思路：
  设事务 T 已提交（COMMIT 记录已落盘）。
  - T 的所有 UPDATE 记录都在 COMMIT 之前写入 WAL
  - 顺序追加 + fsync 保证：COMMIT 落盘 → 之前所有记录都落盘
  - 所以 T 的所有修改都能从 WAL 读到 → 可以 redo
  → T 的修改不会丢失

  设事务 T 未提交（COMMIT 记录未落盘）。
  - T 的 UPDATE 记录可能部分落盘
  - 但 redo 时只重做已提交事务 → T 的修改被忽略
  → T 的修改不会部分生效
  → 一致性保持
```

### 2.3 WAL 写入的时序图

```
时间轴 ──────────────────────────────────────────────────────►

事务 T1 (修改 page0):
  │
  ├─ wal_begin(T1) ──────────────┐
  │   WAL: [BEGIN T1]             │
  │   fflush ✓                    │
  │                               │
  ├─ wal_update(T1, page0, ...) ──┤
  │   WAL: [BEGIN T1][UPDATE ...] │
  │   fflush ✓                    │
  │                               │
  ├─ 修改 Buffer Pool page0       │  ← 内存操作，不刷盘
  │   page0.data = new_data       │
  │                               │
  ├─ wal_commit(T1) ──────────────┤
  │   WAL: [BEGIN T1][UPDATE ...][COMMIT T1]
  │   fflush ✓                    │  ← 日志完整落盘
  │                               │
  │   返回"提交成功"给用户         │
  │                               │
  │      ... 时间流逝 ...         │
  │                               │
  ├─ 后台刷盘线程把 page0 写到数据文件
  │   write(page0); fsync(data_file)
  │                               │
  ▼
```

注意：用户收到"提交成功"时，数据页可能还没刷盘，但**日志已经完整落盘**。这就是 WAL 提供的保证。

### 2.4 WAL 与 Buffer Pool 的协作

```
┌─────────────────────────────────────────────────────────────┐
│                    数据库进程                               │
│                                                             │
│  ┌──────────────┐         ┌──────────────────────────┐     │
│  │   WAL 文件   │         │     Buffer Pool          │     │
│  │  (顺序追加)  │         │  ┌─────┬──────┬───────┐  │     │
│  │ ┌──────────┐ │         │  │page │ data │ dirty │  │     │
│  │ │BEGIN T1  │ │         │  ├─────┼──────┼───────┤  │     │
│  │ │UPDATE ...│ │ ◄────── │  │  0  │ new  │  1   │  │     │
│  │ │COMMIT T1 │ │  redo   │  │  5  │ new  │  1   │  │     │
│  │ │BEGIN T2  │ │         │  │ 12  │ old  │  0   │  │     │
│  │ │UPDATE ...│ │         │  └─────┴──────┴───────┘  │     │
│  │ └──────────┘ │         │       (内存中)            │     │
│  └──────────────┘         └──────────────────────────┘     │
│         │                          │                        │
│         │ fsync                    │ write + fsync          │
│         ▼                          ▼                        │
│   ┌──────────┐              ┌──────────────┐               │
│   │ 磁盘 WAL │              │ 磁盘数据文件 │               │
│   └──────────┘              └──────────────┘               │
└─────────────────────────────────────────────────────────────┘

规则：数据页刷盘前，对应 WAL 记录必须已落盘
```

---

## 3. 日志记录类型：BEGIN/COMMIT/UPDATE/ABORT/CHECKPOINT

### 3.1 五种记录类型总览

miniDB 的 WAL 文件是一串变长记录，每条记录属于以下五种类型之一：

| 类型 | 数值 | 含义 | 携带数据 | 触发时机 |
|---|---|---|---|---|
| `LOG_BEGIN` | 0 | 事务开始 | 无 | `wal_begin(txn_id)` |
| `LOG_COMMIT` | 1 | 事务提交 | 无 | `wal_commit(txn_id)` |
| `LOG_UPDATE` | 2 | 数据修改 | page_id, offset, length, old_data, new_data | `wal_update(...)` |
| `LOG_ABORT` | 3 | 事务中止 | 无 | `wal_abort(txn_id)` |
| `LOG_CHECKPOINT` | 4 | 检查点 | max_lsn | `wal_checkpoint(max_lsn)` |

### 3.2 记录的统一磁盘格式

所有记录共享一个定长头部，后接变长的类型相关数据：

```
┌──────────┬──────────┬──────────┬──────────┬──────────────────────┐
│ rec_len  │   lsn    │  txn_id  │   type   │  type-specific data  │
│  4 字节  │  8 字节  │  4 字节  │  1 字节  │      （变长）        │
└──────────┴──────────┴──────────┴──────────┴──────────────────────┘
  大端u32    大端u64    大端u32    单字节

字段说明：
  rec_len  : 后面所有数据的总长度（lsn + txn_id + type + extra）
             用于读取时知道要读多少字节
  lsn      : 本条记录的日志序号（= 本记录在文件中的起始偏移量）
  txn_id   : 发出本操作的事务 ID
  type     : 记录类型（0~4）
  extra    : 类型相关数据，见下文
```

**为什么头部要存 `rec_len`？**

因为记录是变长的（UPDATE 带 old_data/new_data，长度不固定），读取时必须先知道"这条记录总共多长"，才能一次性读完整。`rec_len` 就是这个用途。

### 3.3 各类型记录的 extra 字段

#### BEGIN / COMMIT / ABORT

```
┌──────────┬──────────┬──────────┬──────────┐
│ rec_len  │   lsn    │  txn_id  │   type   │
│  4B      │  8B      │  4B      │  1B      │
└──────────┴──────────┴──────────┴──────────┘
extra = 0 字节，rec_len = 8 + 4 + 1 + 0 = 13
```

这三种记录不携带额外数据，只标记事务生命周期的一个节点。

#### UPDATE

```
┌──────────┬──────────┬──────────┬──────────┬──────────┬──────────┬──────────┬──────────┬──────────┐
│ rec_len  │   lsn    │  txn_id  │   type   │ page_id  │  offset  │  length  │ old_data │ new_data │
│  4B      │  8B      │  4B      │  1B      │  4B      │  2B      │  2B      │  len B   │  len B   │
└──────────┴──────────┴──────────┴──────────┴──────────┴──────────┴──────────┴──────────┴──────────┘
extra = 4 + 2 + 2 + len + len = 8 + 2*len
rec_len = 8 + 4 + 1 + 8 + 2*len = 21 + 2*len
```

UPDATE 记录字段含义：

| 字段 | 大小 | 含义 |
|---|---|---|
| `page_id` | 4B | 被修改的页编号 |
| `offset` | 2B | 页内偏移量（修改发生在页的哪个位置）|
| `length` | 2B | 修改的字节长度 |
| `old_data` | len B | 修改前的旧数据（用于 undo，章6）|
| `new_data` | len B | 修改后的新数据（用于 redo，本章）|

**为什么同时存 old_data 和 new_data？**

- `new_data` 用于 **redo**：崩溃后重做修改，把新数据写回页。
- `old_data` 用于 **undo**：回滚未提交事务时，把旧数据写回页（章6 实现）。
- miniDB 本章只实现 redo，但 old_data 已经预留，方便章6 扩展。

#### CHECKPOINT

```
┌──────────┬──────────┬──────────┬──────────┬──────────┐
│ rec_len  │   lsn    │  txn_id  │   type   │ max_lsn  │
│  4B      │  8B      │  4B      │  1B      │  8B      │
└──────────┴──────────┴──────────┴──────────┴──────────┘
extra = 8 字节，rec_len = 8 + 4 + 1 + 8 = 21
txn_id 固定为 0（检查点不属于任何事务）
```

`max_lsn` 记录检查点时刻 WAL 的最大 LSN，用于恢复时确定从哪里开始重做。

### 3.4 一个完整事务的 WAL 记录序列

```
事务 T5 修改 page3 的 offset=64 处 4 字节：旧值 [0,0,0,0] → 新值 [1,2,3,4]

WAL 文件内容（每行一条记录）：

偏移   记录
0      [rec_len=13][lsn=0] [txn_id=5][BEGIN]
13     [rec_len=29][lsn=13][txn_id=5][UPDATE][page_id=3][offset=64][length=4][old=00 00 00 00][new=01 02 03 04]
42     [rec_len=13][lsn=42][txn_id=5][COMMIT]

文件总大小：55 字节
```

### 3.5 记录类型在源码中的定义

`wal.h` 第 13-19 行：

```c
typedef enum {
    LOG_BEGIN = 0,
    LOG_COMMIT = 1,
    LOG_UPDATE = 2,
    LOG_ABORT = 3,
    LOG_CHECKPOINT = 4,
} log_type_t;
```

用枚举而不是宏，类型安全；数值从 0 连续，方便数组索引。

---

## 4. LSN 概念：日志序号

### 4.1 什么是 LSN

**LSN（Log Sequence Number，日志序号）** 是给每条 WAL 记录分配的唯一编号，用于标识记录的时间顺序。

miniDB 的做法非常简单：**用记录在 WAL 文件中的字节偏移量作为 LSN**。

```
WAL 文件：
偏移:  0    13    42    55    68
       │     │     │     │     │
       ▼     ▼     ▼     ▼     ▼
       [BEGIN][UPDATE][COMMIT][BEGIN][UPDATE]
LSN:   0     13    42    55    68
```

### 4.2 为什么用文件偏移量

| 方案 | 优点 | 缺点 |
|---|---|---|
| **文件偏移量**（miniDB 选择） | 天然单调递增，无需额外维护；读取时直接 `fseek` 到 LSN | 文件增长后 LSN 很大；删除旧日志需重新编号 |
| 自增计数器 | 简单直观 | 需要额外持久化计数器；崩溃后要恢复计数器 |
| 时间戳 | 含义直观 | 精度不够可能重复；时钟回拨问题 |

miniDB 选文件偏移量，**零维护成本**，最适合教学。

### 4.3 LSN 的单调递增性

因为记录是**顺序追加**（append-only）到文件末尾，每条记录的起始偏移量严格大于前一条：

```c
// wal.c write_record_header() 第 58-60 行
fseek(fp, 0, SEEK_END);          // 跳到文件末尾
long pos = ftell(fp);            // 当前偏移量 = 这条记录的 LSN
lsn_t lsn = (lsn_t)pos;
```

所以 LSN 天然满足：

```
对任意两条记录 R1, R2，若 R1 先于 R2 写入，则 R1.lsn < R2.lsn
```

### 4.4 LSN 的用途

| 用途 | 说明 |
|---|---|
| **记录顺序** | 恢复时按 LSN 升序重做，保证操作顺序正确 |
| **Checkpoint** | CHECKPOINT 记录的 max_lsn 告诉恢复程序"之前的日志已无需重做" |
| **页 LSN**（真实数据库） | 每个数据页记录"最后一次修改的 LSN"，redo 时若 page_lsn >= record_lsn 则跳过（已刷盘）|
| **WAL 截断** | checkpoint 之后可以安全删除 LSN < checkpoint_lsn 的旧日志 |

miniDB 本章只用到了前两个用途，页 LSN 留作习题。

### 4.5 LSN 的类型定义

`wal.h` 第 8 行：

```c
typedef uint64_t lsn_t;
```

用 64 位无符号整数，即使每秒写 100 万条记录，也能用 58 万年才溢出。`INVALID_LSN` 定义为 0（`wal.h` 第 11 行），因为合法 LSN 至少是 13（第一条记录的偏移量就是 0，但 0 也合法；这里 0 表示"无效"是一种简化）。

---

## 5. Redo 流程：崩溃后怎么恢复

### 5.1 Redo 的核心思想

> **Redo（重做）**：崩溃后，重新执行 WAL 中已提交事务的所有 UPDATE 操作，把数据页恢复到事务提交后的状态。

为什么"重做"就能恢复？因为：

```
事务提交时：
  - WAL 已落盘（包含所有 UPDATE 和 COMMIT）
  - 数据页可能没刷盘

崩溃后：
  - WAL 完好（在磁盘上）
  - 数据页可能是旧的（没刷盘）或新的（已刷盘）

重做 UPDATE：
  - 把 new_data 写回 page 的 offset 处
  - 如果 page 本来就是新的 → 写入相同数据，无副作用（幂等）
  - 如果 page 是旧的 → 写入新数据，恢复成功
```

### 5.2 为什么只需要 redo，不需要 undo？

本章只实现 redo，不做 undo。原因：

```
miniDB 本章的简化假设：
  - 事务提交后才让修改对用户可见
  - 未提交事务的 UPDATE 记录虽然写进了 WAL，但 redo 时被忽略
  - 未提交事务的修改可能已经在 Buffer Pool，但崩溃后内存丢失
  - 数据文件本来就没有未提交事务的修改（因为没刷盘 / 即使刷盘也会被 redo 覆盖）

所以：
  - 已提交事务 → redo → 恢复
  - 未提交事务 → 忽略 → 相当于自动回滚
  - 不需要显式 undo
```

**注意**：这是简化。真实场景下，未提交事务的脏页可能已经刷盘（LRU 淘汰），这时就需要 undo 把旧数据写回去。miniDB 章6 会实现 undo。

### 5.3 Redo 的两遍扫描

`recovery.c` 的 `recovery_redo` 函数分两遍扫描 WAL：

```
第一遍：Analysis（收集已提交事务）
  ┌─────────────────────────────────────────┐
  │  committed[1024] = {false}              │
  │  for each record in WAL:                │
  │      if record.type == COMMIT:          │
  │          committed[record.txn_id] = true│
  └─────────────────────────────────────────┘

第二遍：Redo（重做已提交事务的 UPDATE）
  ┌─────────────────────────────────────────┐
  │  for each record in WAL:                │
  │      if record.type == UPDATE           │
  │         and committed[record.txn_id]:   │
  │          page = fetch(record.page_id)   │
  │          memcpy(page+offset, new_data)  │
  │          mark page dirty                │
  └─────────────────────────────────────────┘
```

**为什么要两遍？** 因为 COMMIT 记录在 UPDATE 之后才写入。一遍扫描时，读到 UPDATE 时还不知道这个事务会不会提交。必须先扫一遍找出所有已提交事务，第二遍才能判断每个 UPDATE 该不该重做。

### 5.4 Redo 时序图

```
崩溃后重启，调用 recovery_redo(bp, wal)：

时间轴 ──────────────────────────────────────────────────────►

阶段1: Analysis（第一遍扫描 WAL）
  │
  ├─ 打开 WAL 迭代器
  ├─ 读记录 [lsn=0  txn=5 BEGIN]    → 跳过
  ├─ 读记录 [lsn=13 txn=5 UPDATE]  → 跳过（暂不知道是否提交）
  ├─ 读记录 [lsn=42 txn=5 COMMIT]  → committed[5] = true ✓
  ├─ 读记录 [lsn=55 txn=6 BEGIN]   → 跳过
  ├─ 读记录 [lsn=68 txn=6 UPDATE]  → 跳过
  │  （没有 txn=6 的 COMMIT，事务未提交）
  ├─ 关闭迭代器
  │
  │  结果：committed = { 5: true, 6: false }
  │
阶段2: Redo（第二遍扫描 WAL）
  │
  ├─ 重新打开迭代器（从头开始）
  ├─ 读记录 [lsn=0  txn=5 BEGIN]   → 不是 UPDATE，跳过
  ├─ 读记录 [lsn=13 txn=5 UPDATE]  → committed[5]=true，重做！
  │     ├─ page = bp_fetch_page(page_id=3)
  │     ├─ memcpy(page.data + 64, new_data=[1,2,3,4], 4)
  │     └─ bp_unpin_page(page_id=3, dirty=true)
  ├─ 读记录 [lsn=42 txn=5 COMMIT]  → 不是 UPDATE，跳过
  ├─ 读记录 [lsn=55 txn=6 BEGIN]   → 跳过
  ├─ 读记录 [lsn=68 txn=6 UPDATE]  → committed[6]=false，跳过
  ├─ 关闭迭代器
  │
  ▼
恢复完成：page3 的 offset=64 处恢复为 [1,2,3,4]
         事务 6 的修改被忽略（相当于回滚）
```

### 5.5 Redo 的幂等性

**幂等（idempotent）**：重复执行同一操作，结果不变。

redo 必须幂等，因为：

- 同一条 UPDATE 记录可能被 redo 多次（多次崩溃恢复）
- page 可能已经是新数据，redo 再次写入相同数据，结果不变

miniDB 的 redo 用 `memcpy` 直接覆盖，天然幂等：

```c
memcpy(page->data + rec.offset, rec.new_data, rec.length);
```

无论 `page->data + rec.offset` 原来是什么，执行后都变成 `rec.new_data`。重复执行结果一样。

**反例（非幂等，危险）**：

```c
// 假设日志记录的是"加 100"操作
page->balance += 100;  // 每次执行都 +100，redo 两次就 +200，错误！
```

所以日志里必须存**绝对值**（new_data）而不是**增量**（+100），这是 redo 幂等的关键。

### 5.6 为什么 committed 数组大小是 1024

`recovery.c` 第 4 行：

```c
#define MAX_TXN_IDS 1024
```

`committed` 数组用 txn_id 直接索引，所以要求 `txn_id < 1024`。这是教学简化，真实数据库会用哈希表。第 12、21 行的 `rec.txn_id < MAX_TXN_IDS` 检查防止越界。

---

## 6. Checkpoint：缩短恢复时间

### 6.1 问题：WAL 越来越长

不做 checkpoint 的话，WAL 文件会无限增长：

```
运行 1 年后，WAL 有 10 亿条记录
崩溃恢复 → 从头扫描 10 亿条 → 耗时数小时
```

而且其中大部分记录对应的脏页早就刷盘了，redo 它们是浪费。

### 6.2 Checkpoint 的作用

> **Checkpoint（检查点）**：定期把所有脏页刷到磁盘，并在 WAL 写一条 CHECKPOINT 记录。恢复时只需从最后一个 checkpoint 开始重做。

```
WAL 文件：
[...旧记录...][CHECKPOINT lsn=1000][...新记录...][COMMIT]
                              │
                              ▼
                     恢复从这里开始
                     之前的记录可以忽略
                     （对应脏页已刷盘）
```

### 6.3 Checkpoint 的步骤

```
执行 checkpoint：
  1. 暂停新事务（或用 fuzzy checkpoint 允许继续）
  2. 把 Buffer Pool 中所有 dirty 页刷到数据文件
     for each page in buffer_pool:
         if page.dirty:
             write page to data_file
             fsync(data_file)
             page.dirty = false
  3. 在 WAL 写 CHECKPOINT 记录，记录当前 max_lsn
     wal_checkpoint(wal, wal_last_lsn(wal))
  4. fsync WAL
  5. 恢复接受新事务
```

### 6.4 为什么必须先刷脏页再写 CHECKPOINT

```
错误顺序（先写 CHECKPOINT 再刷脏页）：
  1. 写 CHECKPOINT lsn=1000 到 WAL，fsync ✓
  2. 💥 刷脏页之前断电
  3. 恢复时看到 CHECKPOINT lsn=1000
     → 认为 lsn < 1000 的记录都已落盘
     → 从 lsn=1000 开始重做
     → 但实际上 lsn=500 的脏页没刷盘！
     → 数据丢失！

正确顺序（先刷脏页再写 CHECKPOINT）：
  1. 刷所有脏页到数据文件，fsync ✓
  2. 写 CHECKPOINT lsn=1000 到 WAL，fsync ✓
  3. 💥 断电
  4. 恢复时看到 CHECKPOINT lsn=1000
     → lsn < 1000 的脏页都已落盘 ✓
     → 从 lsn=1000 开始重做
     → 正确恢复
```

### 6.5 miniDB 的 Checkpoint 实现

`wal.c` 第 132-138 行：

```c
lsn_t wal_checkpoint(wal_t *wal, lsn_t max_lsn) {
    lsn_t lsn = write_record_header(wal->fp, 0, LOG_CHECKPOINT, 8);
    wr_u64(wal->fp, max_lsn);
    fflush(wal->fp);
    wal->last_lsn = lsn;
    return lsn;
}
```

注意：miniDB 的 `wal_checkpoint` **只写 WAL 记录，不刷脏页**。刷脏页是上层（buffer pool / 存储引擎）的职责。调用方必须先刷脏页，再调用 `wal_checkpoint`。

### 6.6 Checkpoint 对恢复时间的改善

| 场景 | 无 Checkpoint | 有 Checkpoint（每 5 分钟）|
|---|---|---|
| 运行 1 天后崩溃 | 扫描 24 小时的 WAL | 最多扫描 5 分钟的 WAL |
| 运行 1 年后崩溃 | 扫描 1 年的 WAL（灾难）| 最多扫描 5 分钟的 WAL |
| WAL 文件大小 | 无限增长 | 可截断旧日志，保持小体积 |

### 6.7 Fuzzy Checkpoint（模糊检查点）

miniDB 的 checkpoint 是** Sharp Checkpoint（锐检查点）**：刷脏页时要暂停事务。

真实数据库多用 **Fuzzy Checkpoint（模糊检查点）**：不暂停事务，只记录"当前活跃事务列表"和"脏页表"，恢复时结合这些信息精确重做。复杂但高效。

---

## 7. ARIES 算法：工业级恢复标准

### 7.1 ARIES 是什么

**ARIES**（Algorithm for Recovery and Isolation Exploiting Semantics）是 IBM 发明的崩溃恢复算法，被 DB2、SQL Server、MySQL InnoDB、PostgreSQL 等几乎所有工业数据库采用。

### 7.2 ARIES 三阶段

```
崩溃
  │
  ▼
┌─────────────────────────────────────────┐
│ 阶段1: Analysis（分析）                 │
│   - 扫描 WAL，从最后一个 checkpoint 开始│
│   - 重建脏页表（Dirty Page Table）      │
│   - 重建活跃事务表（Active Txn Table）  │
│   - 确定 redo 起点                      │
└─────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────┐
│ 阶段2: Redo（重做）                     │
│   - 从 Analysis 确定的起点开始           │
│   - 按 LSN 顺序重做所有 UPDATE          │
│   - 用 page_lsn 判断是否需要重做         │
│   - 幂等：已刷盘的页跳过                 │
└─────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────┐
│ 阶段3: Undo（撤销）                     │
│   - 对所有未提交事务，反向扫描 WAL       │
│   - 用 old_data 撤销 UPDATE             │
│   - 写 CLR（Compensation Log Record）   │
│   - 撤销完成后写 END 记录                │
└─────────────────────────────────────────┘
  │
  ▼
恢复完成，数据库一致
```

### 7.3 三阶段详解

#### Analysis 阶段

```
目的：收集恢复所需的信息
输入：WAL 文件（从最后一个 checkpoint 开始）
输出：
  - DirtyPageTable: { page_id → rec_lsn }  哪些页有未刷盘的修改
  - ActiveTxnTable: { txn_id → last_lsn }  哪些事务未提交
  - redo_start_lsn: 从哪里开始 redo

扫描规则：
  - 遇到 CHECKPOINT：初始化 DirtyPageTable 和 ActiveTxnTable
  - 遇到 UPDATE：把 page_id 加入 DirtyPageTable
  - 遇到 BEGIN：把 txn_id 加入 ActiveTxnTable
  - 遇到 COMMIT/ABORT：从 ActiveTxnTable 移除 txn_id
```

#### Redo 阶段

```
目的：把所有已落盘日志对应的修改重做到数据页
输入：WAL（从 redo_start_lsn 开始），DirtyPageTable
规则：
  - 按 LSN 升序扫描每条 UPDATE
  - 若 page 不在 DirtyPageTable → 已刷盘，跳过
  - 若 page 在 DirtyPageTable 但 page_lsn >= record.lsn → 已重做，跳过
  - 否则：重做 UPDATE，把 new_data 写回 page
```

#### Undo 阶段

```
目的：回滚所有未提交事务的修改
输入：ActiveTxnTable（Analysis 得出）
规则：
  - 找出 ActiveTxnTable 中所有 last_lsn 最大的事务
  - 反向扫描这些事务的 UPDATE 记录
  - 对每条 UPDATE，用 old_data 把页恢复到旧值
  - 写 CLR 记录（记录"已撤销"），防止再次崩溃时重复 undo
  - 所有事务撤销完成后，写 END 记录
```

### 7.4 miniDB 为什么简化

miniDB 本章只实现 **Analysis + Redo 的简化版**，不做 Undo。对比：

| ARIES 完整版 | miniDB 本章 | 简化原因 |
|---|---|---|
| Analysis 重建脏页表和活跃事务表 | 只收集 committed 数组 | 不需要 undo，无需活跃事务表 |
| Redo 用 page_lsn 优化跳过 | Redo 无条件重做所有已提交 UPDATE | 无 page_lsn 字段，简化 |
| Undo 回滚未提交事务 | 忽略未提交事务 | 假设未提交事务的脏页没刷盘 |
| CLR 记录 | 无 | 没有 undo 就不需要 CLR |
| Fuzzy Checkpoint | Sharp Checkpoint | 简化并发控制 |

这些简化在章6（事务与 MVCC）会逐步补全。

### 7.5 ARIES 的核心原则

1. **WAL 规则**：脏页刷盘前，对应日志必须已落盘。（miniDB 遵守）
2. **Redo 规则**：重做时按 LSN 顺序，用 page_lsn 跳过已刷盘的修改。（miniDB 简化，无条件重做）
3. **Undo 规则**：撤销时反向扫描，写 CLR 保证 undo 也满足 WAL。（miniDB 未实现）

---

## 8. fsync 的重要性： fflush ≠ fsync

### 8.1 三个层次的"写入"

```
用户调用 fwrite(fp, data)
  │
  ▼
┌──────────────────────────┐
│ 1. C 库 stdio 缓冲区     │  ← 用户空间内存
│    （fflush 刷出）       │
└──────────────────────────┘
  │ fflush(fp)
  ▼
┌──────────────────────────┐
│ 2. OS 内核 Page Cache    │  ← 内核空间内存
│    （fsync 刷出）        │
└──────────────────────────┘
  │ fsync(fileno(fp))
  ▼
┌──────────────────────────┐
│ 3. 磁盘                  │  ← 持久化
└──────────────────────────┘
```

### 8.2 fflush 和 fsync 的区别

| 操作 | 作用 | 数据去哪 | 断电后 |
|---|---|---|---|
| `fwrite` | 写入 C 库缓冲区 | 用户空间内存 | **丢失** |
| `fflush` | 刷 C 库缓冲区到 OS | 内核 Page Cache | **丢失** |
| `fsync` | 刷 OS 缓冲区到磁盘 | 磁盘 | **保留** ✓ |

**关键**：`fflush` 之后数据还在 OS 内核内存里，断电会丢！只有 `fsync` 之后数据才真正在磁盘上。

### 8.3 miniDB 的简化

`wal.c` 中所有写操作只用 `fflush`，没有 `fsync`：

```c
// wal.c 第 96 行（wal_begin）
fflush(wal->fp);

// wal.c 第 103 行（wal_commit）
fflush(wal->fp);

// wal.c 第 126 行（wal_update）
fflush(wal->fp);
```

这是**教学简化**，真实数据库必须 `fsync`：

```c
// 真实数据库应该这样
fflush(wal->fp);
fsync(fileno(wal->fp));  // 确保落到磁盘
```

### 8.4 为什么 miniDB 只用 fflush

| 原因 | 说明 |
|---|---|
| 教学清晰 | fsync 调用会让代码多一行，分散注意力 |
| 测试方便 | 测试环境断电概率极低，fflush 够用 |
| 性能 | fsync 很慢（毫秒级），教学项目不需要 |
| 文档说明 | 在文档（就是你正在读的这篇）里讲清楚真实需求 |

**但你要记住**：生产环境必须 fsync，否则断电会丢日志，WAL 形同虚设。

### 8.5 fsync 的性能问题

```
fsync 的开销：
  - 一次 fsync 约 1~10 毫秒（机械硬盘更慢）
  - 每次事务提交都 fsync → 每秒最多 100~1000 个事务

优化方案：
  - 组提交（Group Commit）：多个事务的 COMMIT 共用一次 fsync
  - 异步 fsync：后台线程定期 fsync（牺牲一点持久性）
  - fsync on commit：只对 COMMIT 记录 fsync，UPDATE 记录异步
```

### 8.6 写日志的正确姿势（生产级）

```c
lsn_t wal_commit_production(wal_t *wal, txn_id_t txn_id) {
    // 1. 写记录到 C 库缓冲区
    lsn_t lsn = write_record_header(wal->fp, txn_id, LOG_COMMIT, 0);

    // 2. 刷 C 库缓冲区到 OS
    fflush(wal->fp);

    // 3. 刷 OS 缓冲区到磁盘（关键！）
    fsync(fileno(wal->fp));

    wal->last_lsn = lsn;
    return lsn;
}
```

---

## 9. 代码逐行解读

### 9.1 wal.h 完整解读

```c
#ifndef MINIDB_WAL_H
#define MINIDB_WAL_H
```
头文件保护，防止重复包含。

```c
#include "page.h"
#include <stdint.h>
#include <stdbool.h>
```
依赖：`page.h`（提供 `page_id_t`）、标准整数类型、布尔类型。

```c
typedef uint64_t lsn_t;
typedef uint32_t txn_id_t;
```
- `lsn_t`：日志序号，64 位，用文件偏移量。
- `txn_id_t`：事务 ID，32 位，最多约 42 亿个事务。

```c
#define INVALID_LSN ((lsn_t)0)
```
无效 LSN 哨兵值，用于表示"没有日志"。

```c
typedef enum {
    LOG_BEGIN = 0,
    LOG_COMMIT = 1,
    LOG_UPDATE = 2,
    LOG_ABORT = 3,
    LOG_CHECKPOINT = 4,
} log_type_t;
```
五种日志记录类型，数值从 0 连续，方便序列化为单字节。

```c
typedef struct {
    lsn_t lsn;              // 本记录的 LSN
    txn_id_t txn_id;        // 所属事务 ID
    log_type_t type;        // 记录类型
    page_id_t page_id;      // UPDATE: 被修改的页
    uint16_t offset;        // UPDATE: 页内偏移
    uint16_t length;        // UPDATE: 修改长度
    const uint8_t *old_data; // UPDATE: 旧数据（undo 用）
    const uint8_t *new_data; // UPDATE: 新数据（redo 用）
} log_record_t;
```
内存中的日志记录结构。注意 `old_data` 和 `new_data` 是指针，指向迭代器内部的缓冲区（见 9.3 节），读取者用完前不能关闭迭代器。

```c
typedef struct wal wal_t;
```
不透明类型，实现细节藏在 `wal.c` 里（`struct wal`）。

```c
wal_t *wal_open(const char *path);
void   wal_close(wal_t *wal);
```
打开/关闭 WAL 文件。

```c
lsn_t  wal_begin(wal_t *wal, txn_id_t txn_id);
lsn_t  wal_commit(wal_t *wal, txn_id_t txn_id);
lsn_t  wal_abort(wal_t *wal, txn_id_t txn_id);
lsn_t  wal_update(wal_t *wal, txn_id_t txn_id,
                  page_id_t pid, uint16_t offset, uint16_t len,
                  const void *old_data, const void *new_data);
lsn_t  wal_checkpoint(wal_t *wal, lsn_t max_lsn);
```
五种写日志函数，返回本条记录的 LSN。

```c
void   wal_flush(wal_t *wal);
lsn_t  wal_last_lsn(wal_t *wal);
```
- `wal_flush`：手动刷盘（fflush）。
- `wal_last_lsn`：返回最近一次写入的 LSN。

```c
typedef struct wal_iter wal_iter_t;
wal_iter_t *wal_iter_open(wal_t *wal);
bool        wal_iter_next(wal_iter_t *it, log_record_t *rec);
void        wal_iter_close(wal_iter_t *it);
```
迭代器接口，用于顺序读取 WAL 中的所有记录。恢复时用。

### 9.2 wal.c 完整解读

#### 大端序列化辅助函数

```c
static void wr_u32(FILE *fp, uint32_t v) {
    uint8_t b[4] = { (uint8_t)(v>>24), (uint8_t)(v>>16), (uint8_t)(v>>8), (uint8_t)v };
    fwrite(b, 1, 4, fp);
}
```
把 32 位整数按**大端**（高位在前）写入文件。大端序跨平台一致，无论本机 CPU 是大端还是小端，WAL 文件格式都相同。

```c
static void wr_u64(FILE *fp, uint64_t v) {
    uint8_t b[8];
    for (int i = 0; i < 8; i++) b[i] = (uint8_t)(v >> (56 - i * 8));
    fwrite(b, 1, 8, fp);
}
```
64 位大端写入，循环移位。

```c
static void wr_u16(FILE *fp, uint16_t v) {
    uint8_t b[2] = { (uint8_t)(v>>8), (uint8_t)v };
    fwrite(b, 1, 2, fp);
}
```
16 位大端写入。

```c
static uint32_t rd_u32(const uint8_t *p) {
    return ((uint32_t)p[0]<<24)|((uint32_t)p[1]<<16)|((uint32_t)p[2]<<8)|p[3];
}
static uint64_t rd_u64(const uint8_t *p) {
    uint64_t v = 0;
    for (int i = 0; i < 8; i++) v = (v << 8) | p[i];
    return v;
}
static uint16_t rd_u16(const uint8_t *p) {
    return (uint16_t)(((uint16_t)p[0] << 8) | p[1]);
}
```
对应的读取函数，从字节数组按大端解析为整数。

#### write_record_header：写记录头

```c
static lsn_t write_record_header(FILE *fp, txn_id_t txn_id, log_type_t type,
                                 uint32_t extra_len) {
    fseek(fp, 0, SEEK_END);              // 跳到文件末尾
    long pos = ftell(fp);                // 当前偏移 = 这条记录的 LSN
    lsn_t lsn = (lsn_t)pos;

    uint32_t rec_len = 8 + 4 + 1 + extra_len;  // lsn + txn_id + type + extra
    wr_u32(fp, rec_len);                 // 写 rec_len
    wr_u64(fp, lsn);                     // 写 lsn
    wr_u32(fp, txn_id);                  // 写 txn_id
    fputc((int)type, fp);                // 写 type（1 字节）

    return lsn;
}
```
所有写日志函数的公共前缀：跳到文件末尾、计算 LSN、写定长头。`extra_len` 是类型相关数据的长度，由调用方传入。

#### wal_open / wal_close

```c
wal_t *wal_open(const char *path) {
    FILE *fp = fopen(path, "r+b");       // 先尝试打开已有文件
    if (!fp) {
        fp = fopen(path, "w+b");         // 不存在则创建新文件
        if (!fp) return NULL;
    }
    fseek(fp, 0, SEEK_END);             // 定位到末尾，准备追加

    wal_t *wal = malloc(sizeof(wal_t));
    wal->fp = fp;
    wal->last_lsn = 0;
    return wal;
}
```
打开 WAL 文件，`"r+b"` 表示读写二进制（文件必须存在），`"w+b"` 表示创建读写二进制。`b` 在 Linux 下无意义，Windows 下必须。

```c
void wal_close(wal_t *wal) {
    if (!wal) return;
    if (wal->fp) {
        fflush(wal->fp);                // 刷缓冲
        fclose(wal->fp);
    }
    free(wal);
}
```
关闭前 fflush，确保 C 库缓冲区的数据刷到 OS。

#### 五种写日志函数

```c
lsn_t wal_begin(wal_t *wal, txn_id_t txn_id) {
    lsn_t lsn = write_record_header(wal->fp, txn_id, LOG_BEGIN, 0);
    fflush(wal->fp);                    // 立即刷盘
    wal->last_lsn = lsn;
    return lsn;
}
```
`wal_begin`：写一条 BEGIN 记录，extra_len=0（无额外数据），fflush。

```c
lsn_t wal_commit(wal_t *wal, txn_id_t txn_id) {
    lsn_t lsn = write_record_header(wal->fp, txn_id, LOG_COMMIT, 0);
    fflush(wal->fp);
    wal->last_lsn = lsn;
    return lsn;
}
```
`wal_commit`：写一条 COMMIT 记录。**这是事务持久化的关键时刻**——fflush（生产环境要 fsync）之后，事务才算真正提交。

```c
lsn_t wal_abort(wal_t *wal, txn_id_t txn_id) {
    lsn_t lsn = write_record_header(wal->fp, txn_id, LOG_ABORT, 0);
    fflush(wal->fp);
    wal->last_lsn = lsn;
    return lsn;
}
```
`wal_abort`：写一条 ABORT 记录，标记事务中止。

```c
lsn_t wal_update(wal_t *wal, txn_id_t txn_id,
                 page_id_t pid, uint16_t offset, uint16_t len,
                 const void *old_data, const void *new_data) {
    uint32_t extra = 4 + 2 + 2 + len + len;  // page_id + offset + length + old + new
    lsn_t lsn = write_record_header(wal->fp, txn_id, LOG_UPDATE, extra);

    wr_u32(wal->fp, pid);               // 写 page_id
    wr_u16(wal->fp, offset);            // 写 offset
    wr_u16(wal->fp, len);               // 写 length
    fwrite(old_data, 1, len, wal->fp);  // 写 old_data
    fwrite(new_data, 1, len, wal->fp);  // 写 new_data
    fflush(wal->fp);

    wal->last_lsn = lsn;
    return lsn;
}
```
`wal_update`：写一条 UPDATE 记录，包含完整的修改信息。extra 长度 = 4(page_id) + 2(offset) + 2(length) + len(old) + len(new)。

```c
lsn_t wal_checkpoint(wal_t *wal, lsn_t max_lsn) {
    lsn_t lsn = write_record_header(wal->fp, 0, LOG_CHECKPOINT, 8);
    wr_u64(wal->fp, max_lsn);           // 写 max_lsn
    fflush(wal->fp);
    wal->last_lsn = lsn;
    return lsn;
}
```
`wal_checkpoint`：写一条 CHECKPOINT 记录，txn_id 固定为 0（不属于任何事务），extra 是 max_lsn（8 字节）。

#### wal_flush / wal_last_lsn

```c
void wal_flush(wal_t *wal) {
    fflush(wal->fp);
}
lsn_t wal_last_lsn(wal_t *wal) {
    return wal->last_lsn;
}
```
辅助函数，简单直接。

#### 迭代器实现

```c
struct wal_iter {
    FILE *fp;
    uint8_t buf[PAGE_SIZE * 2 + 128];   // 读取缓冲区
    uint8_t data_buf[PAGE_SIZE];        // 存放 old_data/new_data
};
```
迭代器结构。`buf` 用于读取整条记录，`data_buf` 用于存放 UPDATE 记录的 old_data 和 new_data（连续存放，old 在前 new 在后）。

```c
wal_iter_t *wal_iter_open(wal_t *wal) {
    wal_iter_t *it = malloc(sizeof(wal_iter_t));
    it->fp = wal->fp;
    fseek(it->fp, 0, SEEK_SET);         // 定位到文件开头
    return it;
}
```
打开迭代器，定位到 WAL 文件开头，准备从头扫描。

```c
bool wal_iter_next(wal_iter_t *it, log_record_t *rec) {
    FILE *fp = it->fp;

    uint8_t len_buf[4];
    size_t n = fread(len_buf, 1, 4, fp);    // 先读 4 字节的 rec_len
    if (n != 4) return false;               // 读不够 → 文件结束

    uint32_t rec_len = rd_u32(len_buf);
    if (rec_len == 0 || rec_len > sizeof(it->buf)) return false;  // 非法长度

    n = fread(it->buf, 1, rec_len, fp);     // 读整条记录
    if (n != rec_len) return false;         // 读不够 → 不完整记录（可能崩溃时写一半）

    const uint8_t *p = it->buf;
    rec->lsn = rd_u64(p);       p += 8;     // 解析 lsn
    rec->txn_id = rd_u32(p);    p += 4;     // 解析 txn_id
    rec->type = (log_type_t)*p; p += 1;     // 解析 type

    rec->page_id = INVALID_PAGE_ID;         // 默认值
    rec->offset = 0;
    rec->length = 0;
    rec->old_data = NULL;
    rec->new_data = NULL;

    if (rec->type == LOG_UPDATE) {
        rec->page_id = rd_u32(p);  p += 4;  // 解析 page_id
        rec->offset = rd_u16(p);   p += 2;  // 解析 offset
        rec->length = rd_u16(p);   p += 2;  // 解析 length

        memcpy(it->data_buf, p, rec->length);           // 拷贝 old_data
        rec->old_data = it->data_buf;
        rec->new_data = it->data_buf + rec->length;     // new_data 紧跟 old_data
        // 注意：这里有个 bug，应该再 memcpy new_data，见下文
    }

    return true;
}
```

**重要细节**：第 186-188 行只 memcpy 了 `old_data`，没有单独 memcpy `new_data`。但 `new_data` 指向 `data_buf + length`，而 `data_buf + length` 处的数据是从 `buf` 里 `memcpy` 过来的吗？

仔细看：`memcpy(it->data_buf, p, rec->length)` 只拷贝了 `length` 字节（old_data），`p` 此时已经指向 old_data 的末尾。但 `buf` 里 old_data 后面紧跟着 new_data，所以应该再拷贝一次：

```c
memcpy(it->data_buf, p, rec->length);                  // old_data
memcpy(it->data_buf + rec->length, p + rec->length, rec->length);  // new_data
```

当前代码只拷贝了 old_data，`new_data` 指向的 `data_buf + length` 区域是未初始化的！**这是源码的一个 bug**，redo 时会读到垃圾数据。读者可以思考如何修复（见习题第 8 题）。

```c
void wal_iter_close(wal_iter_t *it) {
    free(it);
}
```
关闭迭代器，释放内存。注意不关闭 `fp`，因为 `fp` 是 `wal` 的，由 `wal_close` 关闭。

### 9.3 recovery.h 完整解读

```c
#ifndef MINIDB_RECOVERY_H
#define MINIDB_RECOVERY_H

#include "buffer_pool.h"
#include "wal.h"

void recovery_redo(buffer_pool_t *bp, wal_t *wal);

#endif
```
极简头文件，只暴露一个函数 `recovery_redo`：给定 buffer pool 和 WAL，执行 redo 恢复。

### 9.4 recovery.c 完整解读

```c
#include "recovery.h"
#include <string.h>

#define MAX_TXN_IDS 1024
```
包含头文件，定义最大事务 ID 数（committed 数组大小）。

```c
void recovery_redo(buffer_pool_t *bp, wal_t *wal) {
    bool committed[MAX_TXN_IDS] = {false};    // 已提交事务标记数组
```
栈上分配 1024 个 bool，初始化为全 false。`committed[txn_id]` 为 true 表示该事务已提交。

```c
    // === 第一遍：Analysis，收集已提交事务 ===
    wal_iter_t *it = wal_iter_open(wal);
    log_record_t rec;
    while (wal_iter_next(it, &rec)) {
        if (rec.type == LOG_COMMIT && rec.txn_id < MAX_TXN_IDS) {
            committed[rec.txn_id] = true;
        }
    }
    wal_iter_close(it);
```
打开迭代器，逐条读取 WAL 记录。遇到 COMMIT 记录就把对应 txn_id 标记为已提交。`txn_id < MAX_TXN_IDS` 防止数组越界。

```c
    // === 第二遍：Redo，重做已提交事务的 UPDATE ===
    it = wal_iter_open(wal);
    while (wal_iter_next(it, &rec)) {
        if (rec.type == LOG_UPDATE &&
            rec.txn_id < MAX_TXN_IDS &&
            committed[rec.txn_id]) {
            page_t *page = bp_fetch_page(bp, rec.page_id);
            if (page) {
                memcpy(page->data + rec.offset, rec.new_data, rec.length);
                bp_unpin_page(bp, rec.page_id, true);  // 标记为脏页
            }
        }
    }
    wal_iter_close(it);
}
```
重新打开迭代器（从头扫描）。对每条 UPDATE 记录，若所属事务已提交：
1. `bp_fetch_page` 把目标页加载到 Buffer Pool
2. `memcpy` 把 `new_data` 写到页的 `offset` 处（重做修改）
3. `bp_unpin_page(..., true)` 释放页并标记为脏（`true` = dirty），后续会被刷盘

注意：redo 后页只是被标记为脏，没有立即刷盘。刷盘由 Buffer Pool 的后台任务或正常关闭时完成。

### 9.5 函数调用关系图

```
recovery_redo(bp, wal)
  │
  ├─ wal_iter_open(wal) ──── 第一遍
  │    │
  │    └─ wal_iter_next(it, &rec)  循环
  │         └─ 读 WAL 记录，遇到 COMMIT 则标记 committed[txn_id]=true
  │
  ├─ wal_iter_close(it)
  │
  ├─ wal_iter_open(wal) ──── 第二遍
  │    │
  │    └─ wal_iter_next(it, &rec)  循环
  │         └─ 若 UPDATE 且 committed[txn_id]:
  │              ├─ bp_fetch_page(bp, page_id)
  │              ├─ memcpy(page->data + offset, new_data, length)
  │              └─ bp_unpin_page(bp, page_id, dirty=true)
  │
  └─ wal_iter_close(it)
```

---

## 10. 实战演示：模拟崩溃和恢复

### 10.1 场景设置

我们模拟以下场景：

```
初始状态：
  数据文件：page0 = [0,0,0,0,0,0,0,0]  （8 字节全零）
  WAL 文件：空

操作序列：
  1. 开始事务 T1
  2. 修改 page0 offset=0：[0,0,0,0] → [1,2,3,4]
  3. 提交 T1
  4. 开始事务 T2
  5. 修改 page0 offset=4：[0,0,0,0] → [5,6,7,8]
  6. 💥 断电（T2 未提交）
```

### 10.2 正常执行阶段的时序图

```
时间轴 ──────────────────────────────────────────────────────────►

[1] wal_begin(T1)
    WAL: [BEGIN T1]
    数据文件: page0 = [0,0,0,0,0,0,0,0]  （未变）
    Buffer Pool: page0 = [0,0,0,0,0,0,0,0]  （未变）

[2] wal_update(T1, page0, offset=0, old=[0,0,0,0], new=[1,2,3,4])
    WAL: [BEGIN T1][UPDATE page0 off=0 len=4 old=0000 new=1234]
    数据文件: page0 = [0,0,0,0,0,0,0,0]  （未变）
    Buffer Pool: page0 = [0,0,0,0,0,0,0,0]  （未变，WAL 先写）

    然后修改 Buffer Pool：
    Buffer Pool: page0 = [1,2,3,4,0,0,0,0]  （dirty）

[3] wal_commit(T1)
    WAL: [BEGIN T1][UPDATE ...][COMMIT T1]
    数据文件: page0 = [0,0,0,0,0,0,0,0]  （还没刷盘）
    Buffer Pool: page0 = [1,2,3,4,0,0,0,0]  （dirty）
    → 返回"提交成功"给用户

[4] wal_begin(T2)
    WAL: [BEGIN T1][UPDATE ...][COMMIT T1][BEGIN T2]

[5] wal_update(T2, page0, offset=4, old=[0,0,0,0], new=[5,6,7,8])
    WAL: [BEGIN T1][UPDATE ...][COMMIT T1][BEGIN T2][UPDATE page0 off=4 len=4 old=0000 new=5678]
    Buffer Pool: page0 = [1,2,3,4,5,6,7,8]  （dirty）

[6] 💥 断电！
    内存全部丢失：
    - Buffer Pool 没了
    - OS Page Cache 没了
    只剩磁盘：
    - 数据文件: page0 = [0,0,0,0,0,0,0,0]  （从未刷盘）
    - WAL 文件: [BEGIN T1][UPDATE ...][COMMIT T1][BEGIN T2][UPDATE ...]
                （fflush 假设已落盘；生产环境需 fsync）
```

### 10.3 重启恢复阶段的时序图

```
时间轴 ──────────────────────────────────────────────────────────►

重启数据库，调用 recovery_redo(bp, wal)：

阶段1: Analysis（第一遍扫描）
  │
  ├─ 打开 WAL 迭代器
  ├─ 读 [BEGIN T1]      → 不是 COMMIT，跳过
  ├─ 读 [UPDATE T1]     → 不是 COMMIT，跳过
  ├─ 读 [COMMIT T1]     → committed[1] = true ✓
  ├─ 读 [BEGIN T2]      → 不是 COMMIT，跳过
  ├─ 读 [UPDATE T2]     → 不是 COMMIT，跳过
  └─ 关闭迭代器

  committed = { 1: true, 2: false }

阶段2: Redo（第二遍扫描）
  │
  ├─ 重新打开迭代器
  ├─ 读 [BEGIN T1]      → 不是 UPDATE，跳过
  ├─ 读 [UPDATE T1]     → committed[1]=true，重做！
  │     ├─ page = bp_fetch_page(page0)
  │     │   → 从磁盘加载 page0 = [0,0,0,0,0,0,0,0]
  │     ├─ memcpy(page.data + 0, [1,2,3,4], 4)
  │     │   → page0 = [1,2,3,4,0,0,0,0]
  │     └─ bp_unpin_page(page0, dirty=true)
  │
  ├─ 读 [COMMIT T1]     → 不是 UPDATE，跳过
  ├─ 读 [BEGIN T2]      → 不是 UPDATE，跳过
  ├─ 读 [UPDATE T2]     → committed[2]=false，跳过！
  │     （T2 未提交，修改被忽略，相当于回滚）
  │
  └─ 关闭迭代器

恢复结果：
  Buffer Pool: page0 = [1,2,3,4,0,0,0,0]  （dirty）
  数据文件:   page0 = [0,0,0,0,0,0,0,0]  （还没刷盘，但 Buffer Pool 已正确）

  后续 Buffer Pool 刷盘后：
  数据文件: page0 = [1,2,3,4,0,0,0,0]  ✓

分析：
  - T1 已提交 → 修改被 redo → 生效 ✓
  - T2 未提交 → 修改被忽略 → 相当于回滚 ✓
  - 数据一致，无丢失 ✓
```

### 10.4 伪代码模拟

```c
// 模拟正常执行
wal_t *wal = wal_open("test.wal");
buffer_pool_t *bp = bp_open("test.dat");

// T1
wal_begin(wal, 1);
wal_update(wal, 1, 0, 0, 4, old_0000, new_1234);
// 修改 Buffer Pool
page_t *p = bp_fetch_page(bp, 0);
memcpy(p->data + 0, new_1234, 4);
bp_unpin_page(bp, 0, true);
wal_commit(wal, 1);

// T2
wal_begin(wal, 2);
wal_update(wal, 2, 0, 4, 4, old_0000, new_5678);
p = bp_fetch_page(bp, 0);
memcpy(p->data + 4, new_5678, 4);
bp_unpin_page(bp, 0, true);
// 💥 模拟崩溃：不 wal_commit，直接 exit(1)
exit(1);  // 进程崩溃，内存丢失
```

```c
// 模拟重启恢复
wal_t *wal = wal_open("test.wal");
buffer_pool_t *bp = bp_open("test.dat");

recovery_redo(bp, wal);  // 恢复！

// 验证
page_t *p = bp_fetch_page(bp, 0);
// p->data 应该是 [1,2,3,4,0,0,0,0]
assert(p->data[0] == 1);
assert(p->data[1] == 2);
assert(p->data[2] == 3);
assert(p->data[3] == 4);
assert(p->data[4] == 0);  // T2 的修改被回滚
assert(p->data[5] == 0);
assert(p->data[6] == 0);
assert(p->data[7] == 0);
```

### 10.5 不同崩溃时机的恢复结果

| 崩溃时机 | WAL 内容 | committed | redo 结果 | page0 最终值 |
|---|---|---|---|---|
| T1 BEGIN 后 | [BEGIN T1] | {} | 无 | [0,0,0,0,0,0,0,0] |
| T1 UPDATE 后 | [BEGIN T1][UPDATE T1] | {} | T1 未提交，跳过 | [0,0,0,0,0,0,0,0] |
| T1 COMMIT 后 | [BEGIN T1][UPDATE T1][COMMIT T1] | {1} | redo T1 | [1,2,3,4,0,0,0,0] |
| T2 BEGIN 后 | ...[BEGIN T2] | {1} | redo T1 | [1,2,3,4,0,0,0,0] |
| T2 UPDATE 后 | ...[BEGIN T2][UPDATE T2] | {1} | redo T1，跳过 T2 | [1,2,3,4,0,0,0,0] |
| T2 COMMIT 后 | ...[COMMIT T2] | {1,2} | redo T1 和 T2 | [1,2,3,4,5,6,7,8] |

观察：无论何时崩溃，已提交事务的修改都恢复，未提交事务的修改都回滚。这就是 WAL + Redo 的威力。

---

## 11. 与真实数据库对比

### 11.1 功能对比表

| 特性 | miniDB 本章 | MySQL InnoDB | PostgreSQL | SQLite |
|---|---|---|---|---|
| WAL 机制 | ✓ 顺序追加 | ✓ redo log + binlog | ✓ WAL | ✓ rollback journal / WAL |
| LSN | 文件偏移量 | redo log 文件偏移 + block 号 | WAL 文件偏移 | 记录序号 |
| Redo | ✓ 两遍扫描 | ✓ ARIES | ✓ ARIES | ✓ 简化版 |
| Undo | ✗ 章6 实现 | ✓ ARIES | ✓ ARIES | ✓ |
| Checkpoint | Sharp | Fuzzy | Fuzzy | Sharp |
| fsync | ✗ 只 fflush | ✓ 可配置 | ✓ 可配置 | ✓ 可配置 |
| 组提交 | ✗ | ✓ | ✓ | ✗ |
| 页 LSN 优化 | ✗ | ✓ | ✓ | ✗ |
| CLR 记录 | ✗ | ✓ | ✓ | ✗ |
| 并发恢复 | ✗ | ✓ | ✓ | ✗ |

### 11.2 InnoDB 的 redo log

InnoDB 有两个 redo log 文件循环使用（`ib_logfile0`, `ib_logfile1`），固定大小：

```
┌──────────────────────────────────┐
│         ib_logfile0             │
│  [记录1][记录2]...[记录N][空余] │
└──────────────────────────────────┘
                  ↑
              写指针

写满后切换到 ib_logfile1：
┌──────────────────────────────────┐
│         ib_logfile1             │
│  [记录N+1]...[记录M][空余]      │
└──────────────────────────────────┘
                  ↑
              写指针

两个文件循环使用，LSN 是逻辑序号，不等于文件偏移量。
```

对比 miniDB：miniDB 用单个 WAL 文件无限增长，简单但不实用。

### 11.3 PostgreSQL 的 WAL

PostgreSQL 的 WAL 称为 XLOG，由多个 segment 文件组成（默认 16MB 一个）：

```
pg_wal/
  000000010000000000000001  (segment 1, 16MB)
  000000010000000000000002  (segment 2, 16MB)
  000000010000000000000003  (segment 3, 16MB)
  ...
```

LSN 是 64 位，高 32 位是 segment 号，低 32 位是 segment 内偏移。PostgreSQL 支持归档 WAL（PITR，Point-in-Time Recovery），可以恢复到任意时间点。

### 11.4 SQLite 的 WAL

SQLite 有两种模式：

**Rollback Journal 模式**（默认）：
```
修改前：把旧数据写入 journal 文件
修改数据文件
提交：删除 journal 文件
崩溃恢复：若 journal 存在，用 journal 里的旧数据回滚
```
这是**undo log** 思路，和 miniDB 的 redo log 相反。

**WAL 模式**（`PRAGMA journal_mode=WAL`）：
```
修改前：把新数据写入 WAL 文件
读时：先查 WAL，再查数据文件
提交：WAL 记录标记为提交
崩溃恢复：WAL 中已提交的记录重做到数据文件
```
和 miniDB 的思路一致。

### 11.5 为什么 miniDB 选择 redo log

| 方案 | 优点 | 缺点 |
|---|---|---|
| **Redo log**（miniDB） | 顺序写性能好；支持 PITR；ARIES 标准 | 需要 undo log 配合 |
| Undo log（SQLite 默认） | 实现简单；单文件 | 随机写；不支持 PITR |

miniDB 选 redo log 是为了和工业级数据库（InnoDB、PostgreSQL）对齐，方便后续扩展。

---

## 12. 习题

### 基础题

**题1**：如果 WAL 写了一半就断电，恢复时会怎样？如何检测不完整的记录？

**提示**：看 `wal_iter_next` 的返回值。`fread` 读不够 `rec_len` 字节时返回 false，迭代终止。所以不完整记录会被自动忽略，但这条记录对应的事务可能"丢"了——如果它是 COMMIT，事务会被当作未提交。

**题2**：redo 操作为什么必须是幂等的（重复执行结果一样）？miniDB 的 redo 是幂等的吗？

**提示**：因为同一条记录可能被 redo 多次（多次崩溃）。miniDB 用 `memcpy` 覆盖，天然幂等。若日志记录的是增量（"+100"），则不幂等。

**题3**：如果不先写日志直接改数据页，什么场景下会丢数据？

**提示**：改了数据页并刷盘，但还没写日志就崩溃。数据文件是新数据，但日志没有记录，无法 redo（其实也不需要 redo，但下次崩溃时无法恢复到这个状态）。更严重的是事务做了一半：page0 刷盘了，page5 没刷盘，数据不一致。

**题4**：checkpoint 时为什么要先刷脏页再写 CHECKPOINT 记录？反过来行不行？

**提示**：见 6.4 节。反过来会导致恢复时误以为脏页已刷盘，跳过 redo，丢数据。

**题5**：组提交（Group Commit）是什么？为什么能提高性能？

**提示**：多个事务的 COMMIT 共用一次 fsync。因为 fsync 很慢（毫秒级），合并多个事务的 fsync 能大幅提高吞吐量。

### 进阶题

**题6**：miniDB 的 `committed` 数组大小是 1024，如果事务 ID 超过 1024 会怎样？如何修复？

**提示**：`rec.txn_id < MAX_TXN_IDS` 检查会跳过，导致该事务的 COMMIT 被忽略，redo 时当作未提交。修复方案：用哈希表代替数组。

**题7**：miniDB 的 redo 没有用 page LSN 优化，每次都从头重做所有已提交事务的 UPDATE。请描述如何引入 page LSN 优化，需要改哪些代码？

**提示**：给 `page_t` 加一个 `lsn` 字段，记录"最后一次修改的 LSN"。redo 时若 `page.lsn >= record.lsn`，说明该修改已在页中，跳过。需要修改 `page.h`、`wal_update`（更新页 LSN）、`recovery_redo`（加判断）。

**题8**：`wal_iter_next` 中只 `memcpy` 了 `old_data`，没有 `memcpy` `new_data`（见 9.2 节）。请确认这是否是 bug，如果是，给出修复代码。

**提示**：是 bug。`rec->new_data` 指向 `data_buf + length`，但该区域未拷贝。修复：

```c
memcpy(it->data_buf, p, rec->length);                              // old_data
memcpy(it->data_buf + rec->length, p + rec->length, rec->length);  // new_data
```

**题9**：miniDB 只用 `fflush`，不用 `fsync`。请修改 `wal.c`，在 `wal_commit` 中加入 `fsync`，并讨论对性能的影响。

**提示**：

```c
#include <unistd.h>

lsn_t wal_commit(wal_t *wal, txn_id_t txn_id) {
    lsn_t lsn = write_record_header(wal->fp, txn_id, LOG_COMMIT, 0);
    fflush(wal->fp);
    fsync(fileno(wal->fp));  // 新增
    wal->last_lsn = lsn;
    return lsn;
}
```

性能影响：每次 commit 多一次 fsync（1~10ms），吞吐量下降。可用组提交缓解。

**题10**：设计一个测试用例，验证 miniDB 的崩溃恢复正确性。要求：模拟"提交后崩溃"和"未提交崩溃"两种场景，检查数据是否正确。

**提示**：

```c
void test_recovery_committed() {
    // 1. 创建 WAL 和数据文件
    // 2. 执行事务 T1：修改 page0，提交
    // 3. 模拟崩溃：关闭 WAL 和 BP，不刷盘
    // 4. 重新打开，调用 recovery_redo
    // 5. 验证 page0 的修改已恢复
}

void test_recovery_uncommitted() {
    // 1. 创建 WAL 和数据文件
    // 2. 执行事务 T1：修改 page0，不提交
    // 3. 模拟崩溃
    // 4. 重新打开，调用 recovery_redo
    // 5. 验证 page0 的修改未生效（回滚）
}
```

### 思考题

**题11**：如果 miniDB 要实现 PITR（Point-in-Time Recovery，恢复到指定时间点），需要哪些改动？

**提示**：WAL 记录加时间戳；恢复时按时间戳停止 redo；保留历史 WAL 不删除。

**题12**：miniDB 的 WAL 是单文件无限增长。如果 WAL 文件太大，如何安全截断？

**提示**：checkpoint 之后，LSN < checkpoint_lsn 的记录可以删除。但 miniDB 用文件偏移量作 LSN，删除前部会改变 LSN，需要重新编号或改用逻辑 LSN。

**题13**：为什么 ARIES 要求 undo 阶段也写日志（CLR 记录）？如果不写，再次崩溃时会怎样？

**提示**：undo 本身也是修改，若 undo 到一半崩溃，没有日志记录就无法知道 undo 进度，可能重复 undo 或漏 undo。CLR 记录 undo 的进度，保证 undo 也是可恢复的。

**题14**：miniDB 的 redo 是两遍扫描，能否优化为一遍？需要什么前提？

**提示**：若 UPDATE 记录里能直接判断事务是否已提交（如加一个 committed 标志），则可一遍扫描。但 COMMIT 在 UPDATE 之后写入，一遍扫描时无法知道后续是否有 COMMIT。除非用"反向扫描"或"延迟判断"技巧。

**题15**：真实数据库的 fuzzy checkpoint 不暂停事务，如何保证正确性？

**提示**：checkpoint 时记录"当前活跃事务列表"和"脏页表"，恢复时结合这些信息精确判断哪些页需要 redo、哪些事务需要 undo。允许 checkpoint 期间继续写 WAL，但 checkpoint 记录里要包含足够的信息。

---

## 附录 A：WAL 记录格式速查

```
所有记录共享头部：
┌──────────┬──────────┬──────────┬──────────┐
│ rec_len  │   lsn    │  txn_id  │   type   │
│  4B u32  │  8B u64  │  4B u32  │  1B u8   │
└──────────┴──────────┴──────────┴──────────┘
全部大端序

BEGIN (type=0):
  头部 + 无额外数据
  rec_len = 13

COMMIT (type=1):
  头部 + 无额外数据
  rec_len = 13

ABORT (type=3):
  头部 + 无额外数据
  rec_len = 13

UPDATE (type=2):
  头部 + [page_id 4B][offset 2B][length 2B][old_data len B][new_data len B]
  rec_len = 21 + 2*length

CHECKPOINT (type=4):
  头部 + [max_lsn 8B]
  rec_len = 21
  txn_id 固定为 0
```

## 附录 B：关键函数速查

| 函数 | 文件:行 | 作用 |
|---|---|---|
| `wal_open` | wal.c:71 | 打开 WAL 文件 |
| `wal_close` | wal.c:85 | 关闭 WAL 文件 |
| `wal_begin` | wal.c:94 | 写 BEGIN 记录 |
| `wal_commit` | wal.c:101 | 写 COMMIT 记录 |
| `wal_abort` | wal.c:108 | 写 ABORT 记录 |
| `wal_update` | wal.c:115 | 写 UPDATE 记录 |
| `wal_checkpoint` | wal.c:132 | 写 CHECKPOINT 记录 |
| `wal_iter_open` | wal.c:150 | 打开 WAL 迭代器 |
| `wal_iter_next` | wal.c:157 | 读取下一条记录 |
| `wal_iter_close` | wal.c:194 | 关闭迭代器 |
| `recovery_redo` | recovery.c:6 | 执行 redo 恢复 |
| `write_record_header` | wal.c:56 | 写记录头（内部） |

## 附录 C：术语表

| 术语 | 英文 | 含义 |
|---|---|---|
| 预写日志 | Write-Ahead Log (WAL) | 先写日志再改数据的机制 |
| 日志序号 | Log Sequence Number (LSN) | 日志记录的唯一编号 |
| 重做 | Redo | 重新执行已记录的修改 |
| 撤销 | Undo | 回滚已记录的修改 |
| 检查点 | Checkpoint | 缩短恢复时间的机制 |
| 脏页 | Dirty Page | 内存中已修改但未刷盘的页 |
| 幂等 | Idempotent | 重复执行结果不变 |
| 组提交 | Group Commit | 多个事务共用一次 fsync |
| 模糊检查点 | Fuzzy Checkpoint | 不暂停事务的 checkpoint |
| 补偿日志记录 | Compensation Log Record (CLR) | 记录 undo 操作的日志 |
| 时间点恢复 | Point-in-Time Recovery (PITR) | 恢复到指定时间点 |

---

## 附录 D：常见问题 FAQ

**Q1：WAL 文件删除了会怎样？**

A：灾难性后果。WAL 是恢复的唯一依据，删除后无法 redo，数据可能不一致。绝对不要手动删除 WAL 文件。

**Q2：WAL 文件能压缩吗？**

A：可以，但只能在 checkpoint 之后。LSN < checkpoint_lsn 的记录已无用，可以删除。miniDB 用文件偏移量作 LSN，删除前部会改变 LSN，需要特殊处理（如改用逻辑 LSN）。

**Q3：为什么不用 undo log 代替 redo log？**

A：两者作用不同。redo log 保证已提交事务的修改不丢（持久性），undo log 保证未提交事务的修改可回滚（原子性）。ARIES 同时需要两者。miniDB 本章只实现 redo，章6 补 undo。

**Q4：redo 时如果页不在 Buffer Pool 怎么办？**

A：`bp_fetch_page` 会从磁盘加载该页到 Buffer Pool。redo 可能触发大量磁盘 I/O，这是 checkpoint 重要原因之一——减少需要 redo 的页。

**Q5：miniDB 的 WAL 是文本还是二进制？**

A：二进制。用 `fwrite` 写字节，大端序。二进制比文本紧凑、解析快，但不可读（用文本编辑器打开是乱码）。

**Q6：能否用 SSD 的原子写特性省掉 WAL？**

A：理论上可以（某些新型 SSD 支持原子写），但：(1) 不是所有硬件都支持；(2) WAL 还用于复制、PITR 等功能，不止防崩溃；(3) 工业实践仍用 WAL。

**Q7：分布式数据库的 WAL 和单机一样吗？**

A：思路类似但更复杂。分布式 WAL 要解决跨节点一致性，常用 Paxos/Raft 协议。miniDB 是单机 WAL，不涉及分布式。

---

## 附录 E：推荐阅读

| 资料 | 说明 |
|---|---|
| ARIES 论文（Mohan et al., 1992） | ARIES 算法的原始论文，经典中的经典 |
| 《Database Internals》by Stéphane Farault | 数据库内部原理，含 WAL 和恢复章节 |
| 《Designing Data-Intensive Applications》第3章 | 存储与检索，含 WAL 思想 |
| MySQL InnoDB 文档 | 工业级 redo log 实现参考 |
| PostgreSQL 文档 "Reliability and Write-Ahead Log" | PostgreSQL WAL 实现参考 |
| SQLite 文档 "Write-Ahead Logging" | SQLite WAL 模式参考 |

---

## 附录 F：miniDB 恢复流程总览图

```
┌─────────────────────────────────────────────────────────────────┐
│                      miniDB 崩溃恢复总览                        │
└─────────────────────────────────────────────────────────────────┘

正常运⾏：
  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
  │ wal_begin│───►│wal_update│───►│wal_commit│───►│ 修改 BP  │
  └──────────┘    └──────────┘    └──────────┘    └──────────┘
                       │                │
                       │ fflush         │ fflush
                       ▼                ▼
                 ┌──────────────────────────┐
                 │      WAL 文件（磁盘）    │
                 │  [BEGIN][UPDATE][COMMIT] │
                 └──────────────────────────┘

  ┌──────────┐
  │ 修改 BP  │  （内存，dirty）
  └──────────┘
       │
       │ 后台刷盘
       ▼
  ┌──────────┐
  │ 数据文件 │  （磁盘）
  └──────────┘

💥 崩溃！内存全部丢失，只剩磁盘

重启恢复：
  ┌──────────────────────────────────────────────────────────┐
  │ recovery_redo(bp, wal)                                   │
  │                                                          │
  │  阶段1: Analysis（第一遍扫描 WAL）                       │
  │    ┌─────────────────────────────────────────────┐       │
  │    │ committed[1024] = {false}                   │       │
  │    │ for each record in WAL:                     │       │
  │    │   if record.type == COMMIT:                │       │
  │    │     committed[record.txn_id] = true        │       │
  │    └─────────────────────────────────────────────┘       │
  │                       │                                  │
  │                       ▼                                  │
  │  阶段2: Redo（第二遍扫描 WAL）                          │
  │    ┌─────────────────────────────────────────────┐       │
  │    │ for each record in WAL:                     │       │
  │    │   if record.type == UPDATE                  │       │
  │    │      and committed[record.txn_id]:          │       │
  │    │     page = bp_fetch_page(record.page_id)    │       │
  │    │     memcpy(page+offset, record.new_data)    │       │
  │    │     bp_unpin_page(page, dirty=true)         │       │
  │    └─────────────────────────────────────────────┘       │
  │                       │                                  │
  │                       ▼                                  │
  │  恢复完成，数据库一致                                    │
  └──────────────────────────────────────────────────────────┘
```

---

上一章：[章4 堆表存储](04-heap.md) | 下一章：[章6 事务与并发控制](06-txn-mvcc.md)
