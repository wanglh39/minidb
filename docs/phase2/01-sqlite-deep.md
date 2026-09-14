# 章1：SQLite 深入

> SQLite 是世界上部署最广的数据库——每台手机、每个浏览器都内置。本章从源码级理解 SQLite 架构，与阶段1的 miniDB 逐层对照。
>
> **本章目标**：读完本章后，你应当能够：
> 1. 说清 SQLite 的历史定位和"嵌入式数据库"的含义
> 2. 画出 SQLite 的八层架构并解释每层职责
> 3. 把 SQLite 的每个组件和 miniDB 对应起来，讲出异同
> 4. 用十六进制查看器读懂 SQLite 文件头和 B-Tree 页
> 5. 解释 WAL 原理、checkpoint 三种模式、三种 journal 模式的差异
> 6. 区分字节码 VM 和 Volcano 迭代器两种执行模型
> 7. 独立完成 4 道动手习题

---

## 1. SQLite 是什么

### 1.1 一句话定义

**SQLite 是一个用 C 语言写成的、无需独立服务进程、整个数据库就是一个文件的嵌入式关系型数据库引擎。**

把这句话拆开看：

| 关键词 | 含义 | 对比对象 |
|---|---|---|
| **C 语言写成** | 源码约 15 万行 C，可移植到几乎所有平台 | PostgreSQL 用 C，MySQL 用 C/C++ |
| **无需独立服务进程** | 不像 MySQL/PG 那样要先 `mysqld_safe` 启动守护进程 | MySQL 需要后台服务 |
| **整个数据库就是一个文件** | `test.db` 一个文件就是整个数据库，复制即备份 | PG 一个库对应一个目录树 |
| **嵌入式** | 以库的形式链接进宿主程序，没有 client/server 协议 | MySQL/PG 是 C/S 架构 |

### 1.2 历史

```
时间轴
─────────────────────────────────────────────────────
2000  D. Richard Hipp 为美军军舰制导系统写 SQLite
       起因：他们需要一个不需要数据库管理员、
             崩了能自己恢复的小数据库
2001  v1.0 发布，公开源码
2004  v2.0 重写，改用 B-Tree（之前是线性列表）
2005  v3.0 发布，支持 UTF-8/16、BLOB、64-bit
2009  引入 WAL 模式（重大里程碑）
2011  加入 VFS 抽象层，方便移植到嵌入式设备
2018  加入 BEGIN CONCURRENT 实验性并发写
2024  持续维护，仍是单文件、仍是 Hipp 一人主导
─────────────────────────────────────────────────────
```

**作者 D. Richard Hipp** 至今仍是 SQLite 的核心维护者，这在一个全球部署量最大的数据库里非常罕见。SQLite 不接受外部代码贡献（源码公有领域，但提交由 Hipp 团队控制），以保证代码质量的极度一致性。

### 1.3 核心特点

| 特点 | 说明 | 对新手的意义 |
|---|---|---|
| **零配置** | 不用装服务、不用建用户、不用配端口 | `import sqlite3; sqlite3.connect('a.db')` 就能用 |
| **单文件** | 整个库 = 一个 `.db` 文件（WAL 模式多两个辅助文件） | 复制/备份/传输就是 `cp` 一句话 |
| **公有领域** | 不是 GPL、不是 MIT，是真正的公有领域 | 商业闭源产品可任意使用，无法律风险 |
| **跨平台** | 同一份代码跑在 iOS/Android/Windows/Linux/RTOS | 嵌入式设备首选 |
| **ACID** | 完整事务支持，崩溃不丢数据 | 手机突然断电，数据不坏 |
| **SQL-92 子集** | 支持 SQL 标准的大部分，但不支持 RIGHT JOIN、存储过程等 | 日常 CRUD 够用 |
| **无网络协议** | 没有 wire protocol，进程内直接函数调用 | 延迟极低，但只能本机访问 |

### 1.4 为什么每台手机都有它

这是 SQLite 最传奇的地方。让我们看一张部署图：

```
              ┌────────────────────────────────┐
              │       你的手机（任意品牌）       │
              └────────────────────────────────┘
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
   ┌─────────┐      ┌──────────┐      ┌──────────┐
   │  iOS    │      │ Android  │      │ 鸿蒙 等  │
   │ CoreFS  │      │  sqlite  │      │  sqlite  │
   │ sqlite  │      │  (系统库) │      │  (系统库) │
   └─────────┘      └──────────┘      └──────────┘
        │                 │                 │
        └─────────────────┴─────────────────┘
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
        通讯录、短信、相册          浏览器历史、Cookie
        备忘录、健康数据            各 App 的本地缓存
        每个 App 的本地数据         PWA 的 IndexedDB 底层
```

**原因有三**：

1. **体积小**：编译后约 1MB，对手机存储无压力
2. **无服务进程**：不会常驻内存占资源，App 用完即释放
3. **崩溃可靠**：手机随时可能断电、被杀进程，SQLite 的 ACID 保证数据不损坏

> **新手常见误解**：以为 SQLite "性能差"。实际上 SQLite 单机写入可达每秒 10 万+ 行（WAL 模式 + 合理 page_size），远超大多数应用的需求。它"慢"的是高并发写入场景，而不是单机吞吐。

### 1.5 SQLite 不擅长什么

为了建立正确预期，先说清楚 SQLite 的边界：

| 场景 | 是否适合 SQLite | 原因 |
|---|---|---|
| 单机本地存储 | ✅ 完美 | 这就是它的主场 |
| 移动端 App 数据 | ✅ 完美 | 部署量证明 |
| 中小网站（< 100 QPS 写） | ✅ 适合 | 单文件好维护 |
| 数据分析（读多写少） | ✅ 适合 | DuckDB 更专业，但 SQLite 也能用 |
| 高并发写入（> 1000 写/秒） | ❌ 不适合 | 只有一个写者，会排队 |
| 多机共享数据库 | ❌ 不适合 | 文件锁不能跨机器 |
| 大数据量（> 1TB） | ⚠️ 勉强 | 理论 281TB，但单文件管理困难 |
| 复杂权限管理 | ❌ 不适合 | 没有 GRANT/REVOKE 用户体系 |

---

## 2. SQLite 架构总览

### 2.1 八层架构图

```
┌─────────────────────────────────────────────────────┐
│  ① SQL Interface (C API)                            │
│     sqlite3_open / exec / prepare / step / finalize │
├─────────────────────────────────────────────────────┤
│  ② Compiler                                         │
│     ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│     │ Tokenizer│→ │  Parser  │→ │ CodeGen(字节码)│  │
│     └──────────┘  └──────────┘  └──────────────┘  │
├─────────────────────────────────────────────────────┤
│  ③ Virtual Machine (字节码执行引擎)                  │
│     OpenRead / Column / ResultRow / Halt ...        │
├─────────────────────────────────────────────────────┤
│  ④ B-Tree Layer                                     │
│     表 B-Tree (type 5/13) + 索引 B-Tree (type 2/10) │
├─────────────────────────────────────────────────────┤
│  ⑤ Pager (页管理器)                                 │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│   │  WAL     │  │  Cache   │  │  Journal  │        │
│   │ (可选)   │  │ (page    │  │ (rollback)│        │
│   │          │  │  cache)  │  │           │        │
│   └──────────┘  └──────────┘  └──────────┘        │
├─────────────────────────────────────────────────────┤
│  ⑥ OS Interface (OSAL)                              │
│     互斥锁 / 内存分配 / 随机数 / 时间戳              │
├─────────────────────────────────────────────────────┤
│  ⑦ VFS (Virtual File System)                        │
│     unix-vfs / win32-vfs / 内存-vfs / 自定义 vfs    │
├─────────────────────────────────────────────────────┤
│  ⑧ Storage (磁盘文件 / 内存 / 网络块设备)            │
└─────────────────────────────────────────────────────┘
```

### 2.2 每层职责详解

#### ① SQL Interface（C API）

这是应用程序直接调用的层。核心 5 个函数：

```c
sqlite3_open("test.db", &db);              // 打开数据库
sqlite3_prepare(db, sql, -1, &stmt, 0);    // 编译 SQL 为 stmt
sqlite3_step(stmt);                        // 执行一步，取一行
sqlite3_column_text(stmt, 0);              // 读取当前行的列
sqlite3_finalize(stmt);                    // 释放 stmt
sqlite3_close(db);                         // 关闭数据库
```

> **新手记忆法**：`open → prepare → step → column → finalize → close`，就像"开门 → 备菜 → 炒一步 → 装盘 → 洗锅 → 关门"。

#### ② Compiler（编译器）

把 SQL 字符串翻译成字节码。分三步：

```
SQL 文本
  "SELECT name FROM users WHERE id=42"
   │
   ▼  Tokenizer (词法分析)
Token 流
  [SELECT] [name] [FROM] [users] [WHERE] [id] [=] [42]
   │
   ▼  Parser (语法分析)
AST (抽象语法树)
  SelectStmt
    ├─ columns: [name]
    ├─ table:   users
    └─ where:   id == 42
   │
   ▼  CodeGen (代码生成)
字节码指令数组
  [OpenRead, Integer, NotExists, Column, ResultRow, Close, Halt]
```

#### ③ Virtual Machine（字节码 VM）

一个栈式虚拟机，逐条执行字节码。这是 SQLite 与 miniDB 执行器最大的区别——**SQLite 不用迭代器，用解释执行**。

#### ④ B-Tree Layer

SQLite 用的是 **B-Tree（不是 B+Tree）**！这点非常反直觉。表数据存在叶子页，内部页只存索引键和子页指针。索引也是 B-Tree。

#### ⑤ Pager（页管理器）

所有页的读写都经过 Pager。它负责：
- 维护 page cache（LRU）
- 处理 WAL 或 rollback journal
- 写前先记日志（write-ahead logging 或 undo logging）

#### ⑥ OS Interface（OSAL）

抽象 OS 相关功能：互斥锁、内存分配、随机数、当前时间。让 SQLite 能在不同 OS 上编译。

#### ⑦ VFS（虚拟文件系统）

抽象文件 I/O。SQLite 内置 unix-vfs、win32-vfs，也允许用户自定义 VFS（比如把数据库放在内存里、放在网络块设备上）。

#### ⑧ Storage

物理存储层。通常是磁盘上的一个 `.db` 文件，WAL 模式下还有 `.db-wal` 和 `.db-shm`。

### 2.3 一次查询的完整流程

```
应用调用 sqlite3_exec("SELECT ...")
        │
        ▼
┌─ SQL Interface ─────────────────────────┐
│  sqlite3_prepare_v2()                   │
└─────────────────────────────────────────┘
        │
        ▼
┌─ Compiler ──────────────────────────────┐
│  Tokenize → Parse → CodeGen             │
│  产出: sqlite3_stmt (含字节码)           │
└─────────────────────────────────────────┘
        │
        ▼
┌─ VM ────────────────────────────────────┐
│  sqlite3_step() 逐条执行字节码          │
│  遇到 OpenRead → 调用 B-Tree            │
└─────────────────────────────────────────┘
        │
        ▼
┌─ B-Tree ────────────────────────────────┐
│  查找 rootpage → 沿指针下行到叶子       │
│  需要读页 → 调用 Pager                   │
└─────────────────────────────────────────┘
        │
        ▼
┌─ Pager ─────────────────────────────────┐
│  先查 page cache → 命中则返回            │
│  未命中 → 调用 VFS 从磁盘读              │
└─────────────────────────────────────────┘
        │
        ▼
┌─ VFS ───────────────────────────────────┐
│  read(fd, buf, page_size, offset)       │
└─────────────────────────────────────────┘
        │
        ▼
   磁盘 I/O 完成，页数据逐层返回
        │
        ▼
   VM 执行 Column 指令，把列值放进结果寄存器
        │
        ▼
   sqlite3_step() 返回 SQLITE_ROW
        │
        ▼
   应用调用 sqlite3_column_text() 取值
```

---

## 3. 与 miniDB 逐层对照

阶段1 我们用 C 造了一个 miniDB。现在把它的每一层和 SQLite 对应起来，看看"教学版"和"工业版"的差距。

### 3.1 总览对照表

| 层 | miniDB（阶段1） | SQLite（工业版） | 核心差异 |
|---|---|---|---|
| **SQL 接口** | `parser_parse()` 返回 AST 直接执行 | `sqlite3_prepare()` 返回字节码 | miniDB 即析即执，SQLite 有编译阶段 |
| **词法分析** | 手写递归下降 | 手写递归下降（LALR-ish） | 都手写，无 flex/bison |
| **语法分析** | 手写递归下降 | 手写 `parse.y`（Lemon 解析器） | SQLite 用自家 Lemon 生成器 |
| **代码生成** | 直接构造 AST 节点 | 生成字节码指令数组 | SQLite 多一层 IR |
| **执行** | Volcano 迭代器（open/next/close） | 字节码栈式 VM | 两种完全不同的执行模型 |
| **B-Tree** | 固定阶 B+Tree | 变长 B-Tree（非 B+Tree） | SQLite 用 B-Tree，叶子不存链表 |
| **页管理** | Slotted Page 4096B 固定 | 变长页 512~65536B 可配 | SQLite 页大小可配 |
| **缓存** | LRU/Clock Buffer Pool | mmap 或自家 page cache | SQLite 默认用 OS mmap |
| **WAL** | 单文件追加 redo 日志 | WAL mode (frame-based 含整页) | SQLite WAL 含完整页镜像 |
| **事务** | 2PL 行级锁 + MVCC | WAL + 数据库级文件锁 | miniDB 行级，SQLite 库级 |
| **恢复** | redo 回放 | WAL 回放 或 rollback undo | SQLite 默认 rollback journal |
| **网络** | 自写 TCP server | 无（嵌入式） | SQLite 不支持远程访问 |

### 3.2 逐组件深入对比

#### 3.2.1 解析器对比

```
miniDB 解析器:                      SQLite 解析器:
┌────────────────┐                 ┌────────────────┐
│  SQL 字符串     │                 │  SQL 字符串     │
└────────────────┘                 └────────────────┘
       │                                  │
       ▼                                  ▼
  手写 lexer                       手写 tokenizer (tokenize.c)
       │                                  │
       ▼                                  ▼
  递归下降 parse                   Lemon parser (parse.y → parse.c)
       │                                  │
       ▼                                  ▼
  AST 节点                         AST 节点 (Select / Insert / ...)
       │                                  │
       ▼                                  ▼
  直接执行                         交给 CodeGen 生成字节码
```

**关键差异**：
- miniDB 的 AST 直接被执行器消费（Volcano 模式）
- SQLite 的 AST 只是中间产物，还要经过 CodeGen 翻译成字节码
- SQLite 用 Lemon 而非 yacc/bison，因为 Lemon 是线程安全的、C 友好的

#### 3.2.2 执行器对比

| 维度 | miniDB Volcano | SQLite 字节码 VM |
|---|---|---|
| **执行方式** | 拉模型（next() 拉一行） | 推模型（VM 推到 ResultRow） |
| **代码形态** | 每个算子一个 struct + 3 函数 | 一个大 switch-case 解释器 |
| **内存** | 每个算子有状态，需 open/close | 共享寄存器组，无状态算子 |
| **调试** | 算子树清晰，易加日志 | 要 `EXPLAIN` 看字节码，不直观 |
| **性能** | 函数调用开销 | 解释执行 + 寄存器，更紧凑 |
| **优化** | 易做 pipeline 推 pushdown | 在 CodeGen 阶段做优化 |

#### 3.2.3 B-Tree 对比

```
miniDB B+Tree:                     SQLite B-Tree:
   [10|20|30]                        [10|20|30]
   /   |   | \                       /   |   | \
 [3,5][10,15][20,25][30,35]       [3,5][10,15][20,25][30,35]
   ↔    ↔     ↔     ↔             (无叶子兄弟指针!)
 兄弟指针相连                       叶子之间靠父节点重新定位
```

| 维度 | miniDB B+Tree | SQLite B-Tree |
|---|---|---|
| **叶子链表** | 有，范围扫描快 | 无，范围扫描要回溯父节点 |
| **数据位置** | 数据只在叶子，内部只存键 | 数据就在叶子，内部只存键 |
| **页分裂** | 固定阶，分裂逻辑简单 | 变长，要处理 overflow page |
| **溢出页** | 不支持 | 支持（大行存到 overflow chain） |

> **为什么 SQLite 不用 B+Tree？** 历史原因 + 简化实现。B-Tree 在 SQLite 的设计下已经够用，加叶子链表会破坏"页可独立修改"的性质。

#### 3.2.4 事务对比

| 维度 | miniDB | SQLite |
|---|---|---|
| **锁粒度** | 行级（2PL） | 数据库级（一个文件一个锁） |
| **MVCC** | 有，读不阻塞写 | 无（WAL 模式下读不阻塞写，但不是 MVCC） |
| **隔离级别** | 可配置（RC/RR/SS） | 只有一种（接近 SNAPSHOT） |
| **死锁检测** | 有 wait-for 图 | 无（库级锁不会死锁） |

**小结**：miniDB 是"教科书式"的行级锁 + MVCC，SQLite 是"工程取舍"的库级锁 + WAL。SQLite 牺牲并发度换实现简单和可靠性。

---

## 4. SQLite 存储格式

这是本章最硬核的部分。我们将从字节级拆解一个 SQLite 文件。

### 4.1 文件头（前 100 字节）

每个 SQLite 数据库文件的前 100 字节是固定的文件头：

```
偏移   大小   含义                                      示例值
─────────────────────────────────────────────────────────────
0      16     魔数: "SQLite format 3\000"              "SQLite format 3\0"
16     2      页大小 (大端, 2 的幂, 512~65536)          0x1000 = 4096
18     1      文件格式写版本 (1=legacy, 2=WAL)           2
19     1      文件格式读版本 (1=legacy, 2=WAL)           2
20     1      每页末尾 reserved space 字节数             0
21     1      最大嵌入 payload 比例 (通常 64)            64
22     1      最小嵌入 payload 比例 (通常 32)            32
23     1      叶子 payload 比例 (通常 32)                32
24     4      文件变更计数器 (大端)                      0x00000001
28     4      数据库大小（页数，0 则从文件大小推断）      0x00000003
32     4      第一个 freelist trunk page 的页号          0
36     4      freelist 总页数                            0
40     4      schema cookie (schema 版本号)              0x00000001
44     4      schema 格式版本 (1~4)                      4
48     4      默认 page cache 大小                       0x00000000
52     4      最大根页号（自动增量建表用）               0x00000002
56     4      text 编码 (1=UTF8, 2=UTF16le, 3=UTF16be)   1
60     4      user version (用户自定义)                  0
64     4      incremental vacuum 模式 (0=off)           0
68     4      application id (用户自定义)                0
72     20     保留扩展 (全 0)                            0...
92     4      version-valid-for (变更计数快照)           0x00000001
96     4     SQLite 版本号 (version-for)                0x002d0002
```

> **新手提示**：用 `xxd test.db | head -n 7` 就能看到前 100 字节的十六进制。第 17、18 字节就是页大小（大端）。

### 4.2 页结构

整个文件按页对齐，每页 `page_size` 字节。页号从 1 开始。

```
┌─────────────────────────────────────────────────┐
│  Page 1 (含文件头 100B + B-Tree 页头)            │
├─────────────────────────────────────────────────┤
│  Page 2                                         │
├─────────────────────────────────────────────────┤
│  Page 3                                         │
├─────────────────────────────────────────────────┤
│  ...                                            │
└─────────────────────────────────────────────────┘
```

**页号约定**：
- Page 1：永远是 sqlite_schema 表的 rootpage（含文件头）
- Page 2+：其他表/索引的 rootpage，由 schema 决定

### 4.3 B-Tree 页类型

SQLite 的 B-Tree 页有四种类型，由页头的第一个字节区分：

| 类型值 | 含义 | 对应 miniDB | 用途 |
|---|---|---|---|
| 2 | 内部索引页 | btree internal node | 索引的非叶子节点 |
| 5 | 内部表页 | btree internal node | 表的非叶子节点 |
| 10 | 叶子索引页 | btree leaf node | 索引的叶子节点 |
| 13 | 叶子表页 | btree leaf node + heap | 表的叶子节点（存实际行） |

```
页头结构（叶子页 8 字节，内部页 12 字节）:

偏移  大小  含义
0     1     页类型 (2/5/10/13)
1     2     第一个 freeblock 偏移 (0=无)
3     2     本页 cell 数量
5     2     cell 内容区起始偏移
7     1     碎片字节数
─── 仅内部页 ───
8     4     最右子页页号 (right-most pointer)
```

### 4.4 Cell 结构

每个 cell 是一个键值对（内部页）或一行数据（叶子表页）。

```
叶子表页的 Cell (type 13):
┌──────────────┬──────────────┬─────────────┐
│ payload size │ rowid (varint)│ record data │
│  (varint)    │  (varint)     │  (变长)     │
└──────────────┴──────────────┴─────────────┘

内部表页的 Cell (type 5):
┌──────────────┬──────────────┬─────────────┐
│ 左子页页号    │ key (rowid)  │  (无 payload)│
│  (4 bytes)   │  (varint)    │              │
└──────────────┴──────────────┴─────────────┘

叶子索引页的 Cell (type 10):
┌──────────────┬─────────────┐
│ payload size │ record data │
│  (varint)    │  (变长)     │
└──────────────┴─────────────┘

内部索引页的 Cell (type 2):
┌──────────────┬──────────────┬─────────────┐
│ 左子页页号    │ payload size │ record data │
│  (4 bytes)   │  (varint)    │  (变长)     │
└──────────────┴──────────────┴─────────────┘
```

### 4.5 Record 格式

每个 cell 的 payload 是一个 record，格式如下：

```
┌─────────────┬──────────────┬─────────┬─────────┬─────┐
│ header size │ serial type  │ serial  │ value   │ ... │
│  (varint)   │  1 (varint)  │ type 2  │ 1       │     │
│             │              │(varint) │         │     │
└─────────────┴──────────────┴─────────┴─────────┴─────┘
   ←────── header ──────→←────── body (各列实际值) ──────→
```

**列类型代码（serial type）**：

| 代码 | 含义 | 占用字节 |
|---|---|---|
| 0 | NULL | 0 |
| 1 | 1-byte 有符号整数 | 1 |
| 2 | 2-byte 有符号整数 | 2 |
| 3 | 3-byte 有符号整数 | 3 |
| 4 | 4-byte 有符号整数 | 4 |
| 5 | 6-byte 有符号整数 | 6 |
| 6 | 8-byte 有符号整数 | 8 |
| 7 | IEEE 754 8-byte 浮点 | 8 |
| 8 | 整数 0（无 payload） | 0 |
| 9 | 整数 1（无 payload） | 0 |
| N≥12, 偶数 | BLOB, (N-12)/2 字节 | (N-12)/2 |
| N≥13, 奇数 | TEXT, (N-13)/2 字节 | (N-13)/2 |

> **对比 miniDB**：miniDB 用固定类型（INT32/INT64/FLOAT），每列固定 4 或 8 字节。SQLite 用变长 serial type，整数 1 字节就存 1 字节，省空间。代价是解析时要多读 header。

### 4.6 Varint 编码

SQLite 大量使用 varint（变长整数）来省空间。规则：

```
单字节: 0vvvvvvv                    (值 = v, 范围 0~127)
双字节: 1vvvvvvv 0vvvvvvv           (值 = v<<7 | v)
三字节: 1vvvvvvv 1vvvvvvv 0vvvvvvv  (值 = v<<14 | v<<7 | v)
...最多 9 字节
```

每个字节最高位是"继续位"：1 表示后面还有字节，0 表示这是最后一字节。

**例子**：数字 200 的编码
```
200 = 0b11001000
拆成 7-bit 组: 0000001 1001000
编码: 10000001 01001000  (两字节)
```

### 4.7 动手验证文件格式

```python
# read_header.py - 读取并解析 SQLite 文件头
import struct

with open('test.db', 'rb') as f:
    h = f.read(100)

print(f"魔数:       {h[0:16]}")
print(f"页大小:     {struct.unpack('>H', h[16:18])[0]}")
print(f"写版本:     {h[18]}")
print(f"读版本:     {h[19]}")
print(f"变更计数:   {struct.unpack('>I', h[24:28])[0]}")
print(f"页数:       {struct.unpack('>I', h[28:32])[0]}")
print(f"schema版本: {struct.unpack('>I', h[44:48])[0]}")
print(f"text编码:   {h[56]}  (1=UTF8)")
print(f"SQLite版本: {struct.unpack('>I', h[96:100])[0]}")
```

---

## 5. SQLite WAL 模式

### 5.1 为什么需要 WAL

默认的 rollback journal 模式：写之前先把**原页**备份到 journal，修改直接写主文件。崩溃时用 journal 回滚。

```
默认模式 (rollback journal):
  写操作:
    1. 读出主文件中的原页 P_old
    2. 把 P_old 写入 journal 文件
    3. 把新页 P_new 写入主文件
    4. 提交: 删除 journal
  
  崩溃恢复:
    如果 journal 存在 → 用 journal 覆盖主文件 → 回滚
```

**问题**：每次写都要先读再写主文件，主文件随机写很慢。而且写时阻塞所有读。

**WAL 的思路**：改页不写主文件，追加到 WAL 文件。读时先查 WAL 再查主文件。

### 5.2 WAL 文件结构

```
WAL 文件 (db.sqlite-wal):
┌──────────────────────────────────────────────────┐
│  WAL Header (32 字节)                            │
│  ├─ 魔数 0x377f0682 或 0x377f0683                │
│  ├─ 文件格式版本 (3007000)                       │
│  ├─ page size                                    │
│  ├─ checkpoint 序号                              │
│  ├─ salt-1, salt-2 (随机)                        │
│  └─ checksum-1, checksum-2                      │
├──────────────────────────────────────────────────┤
│  Frame 1                                         │
│  ├─ Frame Header (24 字节)                       │
│  │  ├─ 页号                                      │
│  │  ├─ 提交后的 db 大小 (非提交帧为 0)            │
│  │  ├─ salt-1, salt-2 (同 WAL header)            │
│  │  └─ checksum                                 │
│  └─ Page Data (page_size 字节)                   │
├──────────────────────────────────────────────────┤
│  Frame 2                                         │
├──────────────────────────────────────────────────┤
│  ...                                             │
└──────────────────────────────────────────────────┘
```

**关键概念**：
- **Frame**：一个页的修改记录，含页号 + 完整页内容
- **提交帧**：`db_size` 非 0 的帧，标志一个事务结束
- **mxFrame**：WAL 中最后一个有效帧的位置

### 5.3 WAL 读写流程

```
写入流程 (WAL mode):
  ┌─────────────┐
  │ 修改页 P     │
  └─────────────┘
        │
        ▼
  ┌─────────────────────────────┐
  │ 1. 获取写锁 (库级，唯一写者) │
  └─────────────────────────────┘
        │
        ▼
  ┌─────────────────────────────┐
  │ 2. 把 P 的新内容追加为       │
  │    WAL 的一个新 frame       │
  └─────────────────────────────┘
        │
        ▼
  ┌─────────────────────────────┐
  │ 3. 更新 WAL header 的       │
  │    mxFrame 和 checksum      │
  └─────────────────────────────┘
        │
        ▼
  ┌─────────────────────────────┐
  │ 4. 释放写锁                  │
  │  (主文件没动!)              │
  └─────────────────────────────┘


读取流程 (WAL mode):
  ┌─────────────┐
  │ 读取页 P     │
  └─────────────┘
        │
        ▼
  ┌─────────────────────────────┐
  │ 1. 查 WAL 的 frame 索引     │
  │    (在 .shm 共享内存里)     │
  └─────────────────────────────┘
        │
        ▼
  ┌─────────────┐         ┌─────────────┐
  │ WAL 有 P?   │──是──→  │ 返回 WAL 中 │
  └─────────────┘         │ 的最新副本  │
        │ 否                └─────────────┘
        ▼
  ┌─────────────────────────────┐
  │ 2. 从主数据库文件读 P       │
  └─────────────────────────────┘
```

### 5.4 Checkpoint 机制

WAL 不能无限增长，需要定期把 WAL 中的页写回主文件，这叫 **checkpoint**。

三种 checkpoint 模式：

| 模式 | 行为 | 适用场景 |
|---|---|---|
| **PASSIVE**（默认） | 尝试 checkpoint，如果有人正在读则跳过未处理部分 | 大多数场景 |
| **FULL** | 等待所有读者结束，完整 checkpoint，再重置 WAL | 需要回收 WAL 空间 |
| **RESTART** | 像 FULL，但 checkpoint 后立即重置 WAL 文件 | 需要立即释放 WAL 空间 |

```
Checkpoint 过程:

  WAL 文件:                          主文件:
  ┌────────┐                        ┌────────┐
  │Frame 1 │ (页 5, 新内容)         │ Page 5 │ (旧)
  │Frame 2 │ (页 8, 新内容)         │ Page 8 │ (旧)
  │Frame 3 │ (页 5, 更新内容)       │ ...    │
  └────────┘                        └────────┘
        │
        ▼  checkpoint
  把每个页的最新 frame 写回主文件:
  ┌────────┐                        ┌────────┐
  │ (清空) │                        │ Page 5 │ (新, 来自 Frame 3)
  │        │                        │ Page 8 │ (新, 来自 Frame 2)
  │        │                        │ ...    │
  └────────┘                        └────────┘
```

### 5.5 与 miniDB WAL 对比

| 维度 | miniDB WAL | SQLite WAL |
|---|---|---|
| **日志内容** | redo 日志（只记操作） | 完整页镜像（记整页） |
| **文件形式** | 单文件追加 | 单文件追加 + shm 共享内存 |
| **回放方式** | 重做操作 | 直接覆盖页 |
| **并发** | 单线程 | 多读单写 |
| **checkpoint** | 简单截断 | PASSIVE/FULL/RESTART 三档 |
| **崩溃恢复** | 回放 redo | 重放 WAL 到最后提交帧 |

> **核心差异**：miniDB 的 WAL 是"逻辑日志"（记操作），SQLite 的 WAL 是"物理日志"（记整页）。物理日志回放快但日志大，逻辑日志紧凑但回放要重做操作。

### 5.6 .shm 共享内存文件

WAL 模式下还有第三个文件 `db.sqlite-shm`，它是共享内存索引：

```
.shm 文件内容 (简化):
┌──────────────────────────────┐
│ WAL Frame Index              │
│ ├─ 页号 → 最新 frame 位置     │
│ ├─ 读者计数                  │
│ └─ checkpoint 锁             │
└──────────────────────────────┘
```

作用：让多个进程快速查找"某页在 WAL 的哪个 frame"，不用每次扫整个 WAL。

---

## 6. SQLite 执行模型：字节码 VM

### 6.1 两种执行模型

数据库执行 SQL 有两种主流模型：

```
模型 A: Volcano 迭代器 (miniDB 用)
┌─────────────────────────────────────┐
│  每个算子是一个对象，有 3 个方法:     │
│    open()  - 初始化                  │
│    next()  - 返回下一行 (或 EOF)     │
│    close() - 清理                    │
│                                     │
│  查询树:                             │
│    Project                           │
│      └─ Filter (id=42)               │
│           └─ Scan(users)             │
│                                     │
│  执行: 调用 Project.next()           │
│        → 内部调 Filter.next()        │
│        → 内部调 Scan.next()          │
│        → 返回一行逐层上传             │
└─────────────────────────────────────┘

模型 B: 字节码 VM (SQLite 用)
┌─────────────────────────────────────┐
│  SQL 先编译成字节码指令数组:          │
│  [OpenRead, Integer, NotExists,     │
│   Column, ResultRow, Close, Halt]   │
│                                     │
│  VM 逐条解释执行:                    │
│    pc = 0                            │
│    while True:                       │
│      op = program[pc]               │
│      execute(op)                    │
│      pc += 1                        │
└─────────────────────────────────────┘
```

### 6.2 字节码示例

```sql
SELECT name FROM users WHERE id = 42;
```

编译为字节码（用 `EXPLAIN` 可看到）：

```
addr  opcode         p1    p2    p3    comment
─────────────────────────────────────────────────
0     OpenRead       0     1     0     打开 users 表 (rootpage=1)
1     Integer        42    1     0     R1 = 42
2     NotExists      0     7     1     if not exists(R1) goto 7
3     Column         0     1     2     R2 = users.name
4     ResultRow      2     1     0     返回 R2
5     Next           0     2     0     下一行 (这里用不上)
6     Close          0     0     0     关闭游标
7     Halt           0     0     0     结束
```

**指令解读**：
- `OpenRead`：打开表准备读，p1=游标号，p2=rootpage
- `Integer`：把常量放进寄存器
- `NotExists`：如果 rowid 不存在则跳转（p2 是跳转目标）
- `Column`：读当前行的某列到寄存器
- `ResultRow`：把寄存器里的行作为结果返回
- `Halt`：结束执行

### 6.3 更复杂的例子

```sql
SELECT u.name, o.amount
FROM users u JOIN orders o ON u.id = o.user_id
WHERE o.amount > 100;
```

字节码（简化）：

```
addr  opcode         p1  p2  p3   comment
─────────────────────────────────────────
0     OpenRead       0   1   0    打开 users
1     OpenRead       1   2   0    打开 orders
2     Rewind         0   10  0    游标0 回到开头, 空则跳 10
3     Rewind         1   9   0    游标1 回到开头, 空则跳 9
4     Column         1   2   1    R1 = orders.amount
5     Integer        100 2   0    R2 = 100
6     Le             2   1   3    if R2 <= R1 跳 3 (下一条 order)
7     Column         0   1   3    R3 = users.name
8     Column         1   2   4    R4 = orders.amount
9     ResultRow      3   2   0    返回 R3, R4
10    Next           1   4   0    下一条 order
11    Next           0   3   0    下一条 user
12    Close          0   0   0
13    Close          1   0   0
14    Halt
```

### 6.4 用 EXPLAIN 查看

```python
import sqlite3
conn = sqlite3.connect('test.db')
for row in conn.execute("EXPLAIN SELECT name FROM users WHERE id=42"):
    print(row)
# 输出: (addr, opcode, p1, p2, p3, p4, p5, comment)
```

### 6.5 VM vs Volcano 对比

| 维度 | Volcano (miniDB) | 字节码 VM (SQLite) |
|---|---|---|
| **抽象** | 算子树 | 指令序列 |
| **控制流** | 函数调用递归 | pc 自增 + 跳转 |
| **状态** | 每算子独立 | 全局寄存器组 |
| **向量化** | 易加 batch | 难（一行一行） |
| **调试** | 算子树直观 | EXPLAIN 看字节码 |
| **代码量** | 多（每算子一类） | 少（一个大 switch） |
| **优化** | pushdown 改树 | CodeGen 阶段改指令 |

> **为什么 SQLite 选 VM？** 历史原因 + 代码紧凑。一个解释器约 2000 行 C，比写几十个算子类简单。性能上，寄存器 VM 的分支预测很好，实测不输 Volcano。

---

## 7. SQLite 并发模型

### 7.1 三种 journal 模式

SQLite 的并发行为由 `PRAGMA journal_mode` 决定：

| 模式 | 并发读 | 并发写 | 崩溃恢复 | 典型用途 |
|---|---|---|---|---|
| **DELETE**（默认） | 多读 | 单写（全库锁） | rollback journal | 兼容性最好 |
| **TRUNCATE** | 多读 | 单写（全库锁） | 同 DELETE，journal 截断而非删除 | 避免文件系统元数据开销 |
| **PERSIST** | 多读 | 单写（全库锁） | 同 DELETE，journal 保留 | 避免反复创建文件 |
| **WAL** | 多读 | 单写（但读不阻塞写） | WAL 回放 | 推荐模式 |
| **MEMORY** | 无 | 无 | 无（内存库） | 临时计算 |
| **OFF** | 多读 | 单写 | 无（崩溃丢数据） | 极速但危险 |

### 7.2 锁的层级

SQLite 有 5 种锁级别（文件锁）：

```
锁级别 (从弱到强):
┌──────────┬──────────────────────────────────┐
│ UNLOCKED │ 无锁，未访问数据库                │
├──────────┼──────────────────────────────────┤
│ SHARED   │ 共享锁，可读，多个读者可同时持有  │
├──────────┼──────────────────────────────────┤
│ RESERVED │ 保留锁，打算写但还没开始写        │
│          │ (此时其他读者仍可进入)            │
├──────────┼──────────────────────────────────┤
│ PENDING  │ 待定锁，等所有读者退出            │
│          │ (不再接受新读者)                  │
├──────────┼──────────────────────────────────┤
│ EXCLUSIVE│ 独占锁，正在写，无人可读          │
└──────────┴──────────────────────────────────┘
```

**加锁流程（DELETE 模式写事务）**：

```
开始写事务:
  UNLOCKED → SHARED → RESERVED → PENDING → EXCLUSIVE
                                          │
                                          ▼
                                     写入主文件
                                          │
                                          ▼
  提交后: EXCLUSIVE → UNLOCKED
```

**WAL 模式的锁**：

```
WAL 写事务:
  UNLOCKED → SHARED → RESERVED (写 WAL, 不升 EXCLUSIVE)
  
WAL 读事务:
  UNLOCKED → SHARED (读 WAL + 主文件, 不阻塞写)
```

### 7.3 WAL 模式下的并发

```python
# WAL 模式下读写并行的例子
import sqlite3, threading, time

def setup():
    conn = sqlite3.connect('concurrent.db')
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t(id INTEGER, val TEXT)")
    conn.close()

def writer():
    conn = sqlite3.connect('concurrent.db', isolation_level=None)
    for i in range(100):
        conn.execute("INSERT INTO t VALUES(?, ?)", (i, f"w{i}"))
        time.sleep(0.001)

def reader():
    conn = sqlite3.connect('concurrent.db', isolation_level=None)
    for _ in range(100):
        rows = conn.execute("SELECT count(*) FROM t").fetchone()
        time.sleep(0.001)

setup()
t1 = threading.Thread(target=writer)
t2 = threading.Thread(target=reader)
t1.start(); t2.start()
t1.join(); t2.join()
# WAL 模式下: reader 和 writer 几乎不互相阻塞
# DELETE 模式下: writer 的 EXCLUSIVE 锁会阻塞 reader
```

### 7.4 与 miniDB 并发对比

| 维度 | miniDB | SQLite |
|---|---|---|
| **锁粒度** | 行级 | 数据库级 |
| **锁类型** | 共享/排他（S/X） | 5 级文件锁 |
| **死锁** | 可能（需检测） | 不可能（单锁） |
| **MVCC** | 有 | 无（WAL 类似快照但非 MVCC） |
| **并发写** | 多写 | 单写 |
| **隔离级别** | RC/RR/SS 可选 | 固定（接近 SNAPSHOT） |

> **取舍**：miniDB 的行级锁 + MVCC 并发度高，但实现复杂（死锁检测、版本清理、可见性判断）。SQLite 的库级锁简单可靠，适合嵌入式场景（手机上很少有高并发写）。

---

## 8. Python 实验详解

本节给出 4 个可运行的 Python 脚本，每个都附详细解读。

### 8.1 实验1：探索 SQLite 文件格式

**文件**：`phase2/01-sqlite-deep/sqlite_internals.py`

```python
# sqlite_internals.py - 探索 SQLite 文件格式和页结构
import sqlite3
import struct
import os

# === 第1步: 建一个测试数据库 ===
if os.path.exists('test.db'):
    os.remove('test.db')

conn = sqlite3.connect('test.db')
conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
conn.execute("INSERT INTO users VALUES (1, 'Alice', 30)")
conn.execute("INSERT INTO users VALUES (2, 'Bob', 25)")
conn.commit()
conn.close()

# === 第2步: 读取文件头 ===
with open('test.db', 'rb') as f:
    header = f.read(100)

print("=== 文件头 ===")
print(f"魔数:       {header[0:16]}")
page_size = struct.unpack('>H', header[16:18])[0]
print(f"页大小:     {page_size} 字节")
print(f"写版本:     {header[18]}  (1=legacy, 2=WAL)")
print(f"读版本:     {header[19]}")
print(f"变更计数:   {struct.unpack('>I', header[24:28])[0]}")
print(f"数据库页数: {struct.unpack('>I', header[28:32])[0]}")
print(f"text编码:   {struct.unpack('>I', header[56:60])[0]}  (1=UTF8)")

# === 第3步: 读取第2页的页头, 判断页类型 ===
with open('test.db', 'rb') as f:
    f.seek(page_size)  # 跳到第2页
    page = f.read(page_size)
    page_type = page[0]
    types = {2: '内部索引', 5: '内部表', 10: '叶子索引', 13: '叶子表'}
    print(f"\n=== 第2页 ===")
    print(f"页类型:     {page_type} ({types.get(page_type, '未知')})")
    n_cells = struct.unpack('>H', page[3:5])[0]
    print(f"cell 数:    {n_cells}")

# === 第4步: 用 PRAGMA 查询元信息 ===
conn = sqlite3.connect('test.db')
print("\n=== PRAGMA 查询 ===")
print(f"page_size:    {conn.execute('PRAGMA page_size').fetchone()[0]}")
print(f"page_count:   {conn.execute('PRAGMA page_count').fetchone()[0]}")
print(f"journal_mode: {conn.execute('PRAGMA journal_mode').fetchone()[0]}")
print(f"freelist_count: {conn.execute('PRAGMA freelist_count').fetchone()[0]}")
print(f"table_info:   {conn.execute('PRAGMA table_info(users)').fetchall()}")
conn.close()
```

**预期输出解读**：

```
=== 文件头 ===
魔数:       b'SQLite format 3\x00'
页大小:     4096 字节              ← 默认 4KB
写版本:     1  (1=legacy, 2=WAL)   ← 默认 rollback journal
读版本:     1
变更计数:   1                       ← 改过 1 次
数据库页数: 2                       ← 共 2 页 (schema + users)
text编码:   1  (1=UTF8)

=== 第2页 ===
页类型:     13 (叶子表页)           ← users 表数据在叶子页
cell 数:    2                       ← 2 行数据 (Alice, Bob)

=== PRAGMA 查询 ===
page_size:    4096
page_count:   2
journal_mode: delete                ← 默认模式
freelist_count: 0
table_info:   [(0, 'id', 'INTEGER', 1, None, 1),
                (1, 'name', 'TEXT', 0, None, None),
                (2, 'age', 'INTEGER', 0, None, None)]
```

### 8.2 实验2：WAL vs DELETE 性能对比

**文件**：`phase2/01-sqlite-deep/wal_experiment.py`

```python
# wal_experiment.py - WAL vs Rollback journal 性能对比
import sqlite3, time, os

def bench(mode, n=10000):
    db = f'bench_{mode}.db'
    if os.path.exists(db): os.remove(db)
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA journal_mode={mode}")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, val TEXT)")
    t0 = time.time()
    for i in range(n):
        conn.execute("INSERT INTO t VALUES(?, ?)", (i, f"val_{i}"))
    conn.commit()
    elapsed = time.time() - t0
    conn.close()
    return elapsed

# 单事务批量插入
print("=== 单事务 10000 行 ===")
print(f"DELETE 模式: {bench('DELETE'):.3f}s")
print(f"WAL    模式: {bench('WAL'):.3f}s")

# 每行一个事务 (最坏情况)
def bench_per_row(mode, n=1000):
    db = f'bench2_{mode}.db'
    if os.path.exists(db): os.remove(db)
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute(f"PRAGMA journal_mode={mode}")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, val TEXT)")
    t0 = time.time()
    for i in range(n):
        conn.execute("INSERT INTO t VALUES(?, ?)", (i, f"val_{i}"))
    elapsed = time.time() - t0
    conn.close()
    return elapsed

print("\n=== 每行一个事务 1000 行 ===")
print(f"DELETE 模式: {bench_per_row('DELETE'):.3f}s")
print(f"WAL    模式: {bench_per_row('WAL'):.3f}s")
```

**预期输出解读**：

```
=== 单事务 10000 行 ===
DELETE 模式: 0.12s    ← 一次性提交，journal 只写一次
WAL    模式: 0.08s    ← WAL 追加写更快

=== 每行一个事务 1000 行 ===
DELETE 模式: 1.50s    ← 每次都要 fsync journal + 主文件
WAL    模式: 0.30s    ← 只 fsync WAL (顺序写快)
```

**结论**：
- 批量提交时差异不大（journal 只写一次）
- 每行提交时 WAL 快 5 倍（顺序写 vs 随机写）
- 真实场景下 WAL 通常快 2~5 倍

### 8.3 实验3：并发读写对比

**文件**：`phase2/01-sqlite-deep/concurrent_test.py`

```python
# concurrent_test.py - WAL vs DELETE 并发对比
import sqlite3, threading, time, os

def test_concurrent(mode):
    db = f'conc_{mode}.db'
    if os.path.exists(db): os.remove(db)
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA journal_mode={mode}")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, val TEXT)")
    conn.commit()
    conn.close()

    errors = []
    def writer():
        try:
            c = sqlite3.connect(db, isolation_level=None, timeout=5)
            c.execute(f"PRAGMA journal_mode={mode}")
            for i in range(100):
                c.execute("INSERT INTO t VALUES(?, ?)", (i, f"w{i}"))
                time.sleep(0.002)
        except Exception as e:
            errors.append(('writer', e))

    def reader():
        try:
            c = sqlite3.connect(db, isolation_level=None, timeout=5)
            c.execute(f"PRAGMA journal_mode={mode}")
            for _ in range(100):
                c.execute("SELECT count(*) FROM t").fetchone()
                time.sleep(0.002)
        except Exception as e:
            errors.append(('reader', e))

    t0 = time.time()
    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=reader)
    t1.start(); t2.start()
    t1.join(); t2.join()
    return time.time() - t0, errors

for mode in ['DELETE', 'WAL']:
    elapsed, errs = test_concurrent(mode)
    print(f"{mode}: {elapsed:.2f}s, 错误: {len(errs)}")
    for who, e in errs:
        print(f"  {who}: {e}")
```

**预期输出**：

```
DELETE: 2.50s, 错误: 0    ← reader 等 writer 释放 EXCLUSIVE 锁
WAL:    0.30s, 错误: 0    ← reader 读 WAL, writer 写 WAL, 互不干扰
```

### 8.4 实验4：miniDB vs SQLite 对比

**文件**：`phase2/01-sqlite-deep/sqlite_vs_minidb.py`

```python
# sqlite_vs_minidb.py - miniDB 与 SQLite 功能性能对照
import sqlite3, time, os, sys

# 假设 miniDB 在阶段1 已实现, 这里用伪代码示意
# sys.path.append('../../phase1/build')
# import minidb

def bench_sqlite(n=10000):
    if os.path.exists('cmp.db'): os.remove('cmp.db')
    conn = sqlite3.connect('cmp.db')
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    t0 = time.time()
    for i in range(n):
        conn.execute("INSERT INTO t VALUES(?, ?)", (i, f"v{i}"))
    conn.commit()
    t1 = time.time()
    # 查询测试
    for _ in range(100):
        conn.execute("SELECT * FROM t WHERE id = 5000").fetchone()
    t2 = time.time()
    conn.close()
    return t1 - t0, t2 - t1

# 注: miniDB 部分需阶段1 完成后才能跑
# def bench_minidb(n=10000):
#     db = minidb.Database('cmp_mini.db')
#     db.execute("CREATE TABLE t(id INT, v VARCHAR(100))")
#     ...

ins, qry = bench_sqlite()
print(f"SQLite: 插入 10000 行 {ins:.3f}s, 查询 100 次 {qry:.3f}s")
```

---

## 9. C 扩展开发

SQLite 允许用 C 写自定义函数和聚合，动态加载。

### 9.1 自定义标量函数

**文件**：`phase2/01-sqlite-deep/sqlite_extension.c`

```c
/* sqlite_extension.c - SQLite C 扩展: 自定义 SQL 函数 */
#include <sqlite3ext.h>
SQLITE_EXTENSION_INIT1

#include <string.h>
#include <stdio.h>

/* 自定义函数: reverse(text) -> 反转字符串 */
static void reverse_func(sqlite3_context *ctx, int argc, sqlite3_value **argv) {
    const unsigned char *text = sqlite3_value_text(argv[0]);
    int len = sqlite3_value_bytes(argv[0]);
    if (text == NULL) {
        sqlite3_result_null(ctx);
        return;
    }
    unsigned char *result = sqlite3_malloc(len + 1);
    for (int i = 0; i < len; i++) {
        result[i] = text[len - 1 - i];
    }
    result[len] = '\0';
    sqlite3_result_text(ctx, (char *)result, len, sqlite3_free);
}

/* 自定义函数: crc32(text) -> CRC32 校验和 */
static void crc32_func(sqlite3_context *ctx, int argc, sqlite3_value **argv) {
    const unsigned char *text = sqlite3_value_text(argv[0]);
    int len = sqlite3_value_bytes(argv[0]);
    unsigned int crc = 0xFFFFFFFF;
    for (int i = 0; i < len; i++) {
        crc ^= text[i];
        for (int j = 0; j < 8; j++) {
            crc = (crc >> 1) ^ (0xEDB88320 & (-(crc & 1)));
        }
    }
    sqlite3_result_int(ctx, crc ^ 0xFFFFFFFF);
}

/* 扩展入口点 (函数名必须叫 sqlite3_extension_init 或 sqlite3_<basename>_init) */
int sqlite3_extension_init(sqlite3 *db, char **err, const char *api) {
    SQLITE_EXTENSION_INIT2(api);
    int rc = SQLITE_OK;
    rc = sqlite3_create_function(db, "reverse", 1, SQLITE_UTF8, NULL,
                                 reverse_func, NULL, NULL);
    if (rc != SQLITE_OK) return rc;
    rc = sqlite3_create_function(db, "crc32", 1, SQLITE_UTF8, NULL,
                                 crc32_func, NULL, NULL);
    return rc;
}
```

### 9.2 编译步骤

```bash
# Linux / macOS
gcc -fPIC -shared sqlite_extension.c -o sqlite_extension.so

# Windows (MSVC)
cl /LD sqlite_extension.c /link /out:sqlite_extension.dll

# Windows (MinGW)
gcc -shared sqlite_extension.c -o sqlite_extension.dll

# 验证
ls -la sqlite_extension.*
```

### 9.3 加载和使用

```python
# load_extension.py - 加载 C 扩展并使用
import sqlite3

conn = sqlite3.connect('test.db')
conn.enable_load_extension(True)
conn.load_extension('./sqlite_extension')  # 自动加 .so/.dll 后缀

# 使用自定义函数
print(conn.execute("SELECT reverse('hello')").fetchone())
# 输出: ('olleh',)

print(conn.execute("SELECT crc32('hello')").fetchone())
# 输出: (907060870,)

# 在 WHERE 子句中使用
conn.execute("CREATE TABLE t(id INTEGER, name TEXT)")
conn.execute("INSERT INTO t VALUES (1, 'alice'), (2, 'bob')")
conn.commit()
print(conn.execute("SELECT name FROM t WHERE reverse(name) LIKE 'b%'").fetchall())
# 输出: [('bob',)]  (因为 reverse('bob')='bob' 以 b 开头)
```

### 9.4 自定义聚合函数

```python
# 自定义聚合: 统计中位数
import sqlite3

class Median:
    def __init__(self):
        self.values = []
    def step(self, value):       # 每行调用一次
        self.values.append(value)
    def finalize(self):          # 最后调用一次, 返回结果
        if not self.values:
            return None
        self.values.sort()
        n = len(self.values)
        if n % 2 == 1:
            return self.values[n // 2]
        return (self.values[n//2 - 1] + self.values[n//2]) / 2

conn = sqlite3.connect(':memory:')
conn.create_aggregate("median", 1, Median)
conn.execute("CREATE TABLE t(v REAL)")
conn.executemany("INSERT INTO t VALUES (?)", [(1.0,), (2.0,), (3.0,), (4.0,), (5.0,)])
print(conn.execute("SELECT median(v) FROM t").fetchone())
# 输出: (3.0,)
```

### 9.5 编译加载流程图

```
┌─────────────────────┐
│ sqlite_extension.c  │  (源码)
└─────────────────────┘
          │
          ▼  gcc -fPIC -shared
┌─────────────────────┐
│ sqlite_extension.so │  (动态库)
│   (或 .dll / .dylib)│
└─────────────────────┘
          │
          ▼  conn.load_extension()
┌─────────────────────┐
│  SQLite 运行时       │  (函数注册进 SQLite)
│  reverse() / crc32()│
└─────────────────────┘
          │
          ▼  SELECT reverse('hello')
┌─────────────────────┐
│  'olleh'             │  (结果)
└─────────────────────┘
```

---

## 10. SQLite 限制和适用场景

### 10.1 硬限制

| 限制项 | 默认值 | 可否调整 | 说明 |
|---|---|---|---|
| 最大数据库大小 | 281 TB | 否（页号 32 位） | 理论值，实际受文件系统限制 |
| 单行最大 | 1 GB | 否 | BLOB 理论 1GB |
| 单页最大 | 65536 B | 是（编译时） | 默认 4096 |
| 最大列数 | 32767 | 是（编译时） | 通常够用 |
| 最大表数 | 无限 | — | 受存储限制 |
| JOIN 嵌套深度 | 64 | 是（编译时） | 复杂查询可能不够 |
| 表达式深度 | 1000 | 是（编译时） | — |
| 函数参数 | 127 | 是（编译时） | — |
| 并发写入 | 1 | 否 | 即使 WAL 也单写 |
| LIKE 长度 | 50000 | 是（编译时） | 超过则不用索引 |

### 10.2 软限制（性能边界）

| 场景 | 建议上限 | 超过会怎样 |
|---|---|---|
| 单库行数 | 10 亿行 | 性能下降，备份慢 |
| 单库大小 | 100 GB | checkpoint 变慢 |
| 并发连接 | 100 | 锁竞争加剧 |
| 单行大小 | 1 MB | 跨页存储，读写慢 |
| BLOB 大小 | 100 MB | 内存压力大 |

### 10.3 适用场景决策树

```
你的需求是什么?
│
├─ 单机本地存储?
│   └─ ✅ SQLite (默认选择)
│
├─ 移动端 App?
│   └─ ✅ SQLite (系统内置)
│
├─ 中小网站 (< 100 写/秒)?
│   ├─ 数据量 < 100GB?
│   │   └─ ✅ SQLite (WAL 模式)
│   └─ 数据量 > 100GB?
│       └─ ⚠️ PostgreSQL
│
├─ 高并发写入 (> 1000 写/秒)?
│   └─ ❌ PostgreSQL / MySQL
│
├─ 多机共享数据库?
│   └─ ❌ PostgreSQL / MySQL
│
├─ 数据分析 (OLAP)?
│   └─ ⚠️ DuckDB (SQLite 的分析版表亲)
│
└─ 嵌入式设备?
    └─ ✅ SQLite (专为嵌入式设计)
```

---

## 11. 与真实数据库对比

### 11.1 嵌入式 vs C/S 对比

| 维度 | SQLite (嵌入式) | PostgreSQL (C/S) | MySQL (C/S) |
|---|---|---|---|
| **架构** | 库，进程内 | 服务进程 | 服务进程 |
| **部署** | 复制 1 个文件 | 装服务 + 建库 | 装服务 + 建库 |
| **网络** | 无 | 有（5432） | 有（3306） |
| **并发** | 单写 | 多写 | 多写 |
| **锁粒度** | 库级 | 行级 | 行级 |
| **MVCC** | 无 | 有 | 有（InnoDB） |
| **存储过程** | 无 | 有（PL/pgSQL） | 有 |
| **用户权限** | 无 | 有（GRANT/REVOKE） | 有 |
| **复制** | 无 | 流复制 | 主从复制 |
| **协议** | 函数调用 | 自定义二进制 | 自定义二进制 |
| **延迟** | < 1μs | ~100μs (本机) | ~100μs (本机) |

### 11.2 性能对比（单机）

```
单事务批量插入 100 万行 (越小越快):

插入吞吐 (行/秒)
  │
  │  ████████████████████████████  SQLite WAL:  ~500,000
  │  ███████████████████████      PostgreSQL:   ~400,000
  │  █████████████████████        MySQL:        ~350,000
  │
  └──────────────────────────────────────────

单行查询延迟 (μs, 越小越快):
  │
  │  █                           SQLite:       ~2 μs
  │  ██████████                  PostgreSQL:   ~50 μs
  │  ███████████                 MySQL:        ~55 μs
  │
  └──────────────────────────────────────────

  SQLite 延迟极低因为没有网络往返
```

### 11.3 功能对比

| SQL 特性 | SQLite | PostgreSQL | MySQL |
|---|---|---|---|
| `RIGHT JOIN` | ❌ | ✅ | ✅ |
| `FULL OUTER JOIN` | ❌ | ✅ | ✅ |
| 窗口函数 | ✅ (3.25+) | ✅ | ✅ (8.0+) |
| CTE / 递归 CTE | ✅ | ✅ | ✅ (8.0+) |
| `UPSERT` (ON CONFLICT) | ✅ | ✅ | ✅ (8.0+) |
| 存储过程 | ❌ | ✅ | ✅ |
| 触发器 | ✅ (行级) | ✅ (语句+行级) | ✅ |
| 自定义类型 | ❌ | ✅ | ⚠️ 有限 |
| JSON | ✅ (1.9+) | ✅ | ✅ |
| 数组类型 | ❌ | ✅ | ❌ |
| 物化视图 | ❌ | ✅ | ❌ |
| 全文搜索 | ✅ (FTS5) | ✅ (tsvector) | ✅ (FULLTEXT) |

### 11.4 何时从 SQLite 迁移

```
出现以下任一信号 → 考虑迁移到 PostgreSQL/MySQL:

1. 并发写入 > 100/秒, 出现锁等待
2. 需要多机共享数据
3. 数据库 > 100GB, 备份/维护困难
4. 需要行级权限控制
5. 需要存储过程/触发器做复杂业务
6. 需要主从复制/读写分离
7. 需要非本机访问数据库
```

---

## 12. 文件清单

| 文件 | 内容 | 行数估计 |
|---|---|---|
| `sqlite_internals.py` | 探索 SQLite 文件格式和页结构 | ~60 |
| `wal_experiment.py` | WAL vs Rollback journal 性能对比 | ~50 |
| `concurrent_test.py` | WAL vs DELETE 并发读写对比 | ~50 |
| `sqlite_vs_minidb.py` | miniDB 与 SQLite 功能性能对照 | ~40 |
| `sqlite_extension.c` | C 扩展：自定义 SQL 函数 | ~50 |
| `load_extension.py` | 加载 C 扩展并使用 | ~20 |

---

## 13. 习题

### 习题 1：读取文件头（基础）

用 Python 读取任意 SQLite 数据库文件的前 100 字节，提取并打印：
- 魔数（前 16 字节）
- 页大小
- 文件格式版本
- 数据库总页数
- text 编码

**提示**：用 `struct.unpack('>H', ...)` 读大端 16 位整数。

**验证**：用 `PRAGMA page_size` 和 `PRAGMA page_count` 对比你的解析结果。

### 习题 2：WAL 并发观察（进阶）

1. 创建一个 WAL 模式的数据库
2. 启动一个写线程持续插入数据
3. 同时启动一个读线程持续查询 count(*)
4. 观察读线程是否能实时看到写线程的插入
5. 改为 DELETE 模式重做，对比行为差异

**思考**：为什么 WAL 模式下读能看到写，DELETE 模式下读会被阻塞？

### 习题 3：C 扩展开发（进阶）

编写一个 C 扩展，添加 `regex_match(pattern, text)` 函数：
- 参数 1：正则表达式字符串
- 参数 2：待匹配字符串
- 返回：1（匹配）或 0（不匹配）

**步骤**：
1. 用 C 标准库 `<regex.h>` 或 PCRE 库
2. 实现 `regex_match_func` 函数
3. 在 `sqlite3_extension_init` 中注册
4. 编译为 `.so` / `.dll`
5. 用 Python 加载并测试 `SELECT * FROM t WHERE regex_match('^a', name)`

### 习题 4：性能对比（综合）

对比 miniDB 和 SQLite 插入 10000 行的性能：
1. 两个数据库都建同样的表
2. 同样的插入语句
3. 测量并对比时间
4. 用 `EXPLAIN` 看 SQLite 的字节码
5. 分析性能差异的原因

**思考**：miniDB 哪里比 SQLite 快？哪里慢？为什么？

### 习题 5：页结构分析（挑战）

用 Python 读取一个 SQLite 文件的第 2 页（叶子表页）：
1. 解析页头，得到 cell 数量
2. 解析 cell 指针数组
3. 对每个 cell，解析 payload size、rowid、record
4. 解析 record header，得到各列的 serial type
5. 解析 record body，得到各列实际值

**目标**：不通过 sqlite3 库，纯字节解析还原出表数据。

**提示**：参考第 4 节的格式定义。varint 解析是难点。

---

## 14. 本章小结

### 14.1 核心知识点回顾

```
┌─────────────────────────────────────────────────────┐
│  SQLite 核心知识图谱                                │
├─────────────────────────────────────────────────────┤
│                                                     │
│  1. 定位: 嵌入式、单文件、零配置、公有领域           │
│                                                     │
│  2. 架构 8 层:                                       │
│     API → Compiler → VM → B-Tree → Pager           │
│     → OSAL → VFS → Storage                          │
│                                                     │
│  3. vs miniDB 关键差异:                              │
│     - 执行: 字节码 VM vs Volcano 迭代器              │
│     - B-Tree: B-Tree vs B+Tree                      │
│     - 并发: 库级锁 vs 行级锁+MVCC                   │
│     - WAL: 物理日志(整页) vs 逻辑日志(操作)          │
│                                                     │
│  4. 存储格式:                                        │
│     - 文件头 100B (页大小/版本/编码)                 │
│     - 4 种页类型 (2/5/10/13)                         │
│     - Record: header + body, varint 编码             │
│                                                     │
│  5. WAL: 追加写 + checkpoint + .shm 索引             │
│                                                     │
│  6. 并发: 5 级锁, WAL 读不阻塞写                     │
│                                                     │
│  7. 限制: 单写、无网络、无用户权限                    │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### 14.2 与阶段1 的呼应

| 阶段1 (miniDB) | 阶段2 (SQLite) | 你学到了什么 |
|---|---|---|
| 手写 B+Tree | 看到 B-Tree 工业实现 | 树形索引的工程取舍 |
| 手写 Volcano 算子 | 看到字节码 VM | 两种执行模型的权衡 |
| 手写 redo WAL | 看到物理页 WAL | 日志的物理 vs 逻辑 |
| 手写 2PL+MVCC | 看到库级锁 | 并发的简单 vs 复杂 |
| 手写 slotted page | 看到 SQLite 变长页 | 存储格式的紧凑性 |

### 14.3 下章预告

下一章我们将深入 **PostgreSQL**——一个真正的 C/S 数据库。你会看到：
- PostgreSQL 的多进程架构（vs SQLite 单进程）
- MVCC 的真实实现（多版本可见性判断）
- 流复制和逻辑复制
- 丰富的扩展生态（PostGIS、pgvector 等）

> **过渡思考**：从 SQLite 到 PostgreSQL，本质是从"嵌入式"到"服务端"的范式转变。SQLite 优化的是"单机零延迟"，PostgreSQL 优化的是"多机高并发"。

---

## 附录 A：常用 PRAGMA 速查

| PRAGMA | 作用 | 示例 |
|---|---|---|
| `page_size` | 查/设页大小 | `PRAGMA page_size=8192;` |
| `page_count` | 查总页数 | `PRAGMA page_count;` |
| `journal_mode` | 查/设日志模式 | `PRAGMA journal_mode=WAL;` |
| `wal_checkpoint` | 触发 checkpoint | `PRAGMA wal_checkpoint(TRUNCATE);` |
| `synchronous` | fsync 级别 | `PRAGMA synchronous=NORMAL;` |
| `cache_size` | page cache 大小 | `PRAGMA cache_size=-10000;` (10MB) |
| `foreign_keys` | 外键检查 | `PRAGMA foreign_keys=ON;` |
| `table_info` | 表结构 | `PRAGMA table_info(users);` |
| `index_list` | 索引列表 | `PRAGMA index_list(users);` |
| `freelist_count` | 空闲页数 | `PRAGMA freelist_count;` |
| `integrity_check` | 完整性检查 | `PRAGMA integrity_check;` |
| `encoding` | 字符编码 | `PRAGMA encoding;` |
| `compile_options` | 编译选项 | `PRAGMA compile_options;` |

## 附录 B：EXPLAIN 速查

```sql
-- 查看字节码 (不执行)
EXPLAIN SELECT * FROM users WHERE id = 42;

-- 查看字节码 + 估算代价 (不执行)
EXPLAIN QUERY PLAN SELECT * FROM users WHERE id = 42;

-- 复杂查询的 EXPLAIN
EXPLAIN QUERY PLAN
SELECT u.name, o.amount
FROM users u
JOIN orders o ON u.id = o.user_id
WHERE o.amount > 100
ORDER BY o.amount DESC
LIMIT 10;
```

**EXPLAIN 输出解读**：
- `SEARCH users USING INDEX` → 用了索引
- `SCAN users` → 全表扫描（可能缺索引）
- `USE TEMP B-TREE FOR ORDER BY` → 排序用了临时表（可优化）

## 附录 C：进一步阅读

| 资源 | 链接 | 用途 |
|---|---|---|
| SQLite 官方文档 | https://www.sqlite.org/docs.html | 权威参考 |
| SQLite 源码 | https://www.sqlite.org/src | 15 万行 C 源码 |
| SQLite 文件格式 | https://www.sqlite.org/fileformat2.html | 本节第 4 节的权威来源 |
| SQLite Architecture | https://www.sqlite.org/arch.html | 架构设计文档 |
| Hipp 访谈 | 搜 "Richard Hipp SQLite interview" | 设计哲学 |
| 《SQLite Database System: Design and Implementation》 | 图书 | 深入源码 |

---

> **下一章**：[02-postgresql-deep.md](02-postgresql-deep.md) — PostgreSQL 深入
