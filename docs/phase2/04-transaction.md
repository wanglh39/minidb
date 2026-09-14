# 章4：事务与隔离级别实战

> 事务是数据库区别于文件系统的核心特征。本章从 ACID 出发，逐一复现脏读、不可重复读、幻读、写偏斜四种并发异常，对比四级隔离级别的能力边界，并用 Python + SQLite/PostgreSQL 实地验证。最后回到阶段1的 miniDB，对照 2PL + MVCC 的实现选择。
>
> **本章读者**：第一次接触事务的初学者。所有概念都配现实类比、时序图、复现步骤，建议边读边动手运行 `concurrency_demos/` 下的脚本。

---

## 目录

- [1. ACID 四性质回顾](#1-acid-四性质回顾)
- [2. 四级隔离级别](#2-四级隔离级别)
- [3. 脏读（Dirty Read）](#3-脏读dirty-read)
- [4. 不可重复读（Non-Repeatable Read）](#4-不可重复读non-repeatable-read)
- [5. 幻读（Phantom Read）](#5-幻读phantom-read)
- [6. 写偏斜（Write Skew）](#6-写偏斜write-skew)
- [7. PostgreSQL 的 Snapshot Isolation 与 SSI](#7-postgresql-的-snapshot-isolation-与-ssi)
- [8. 死锁产生与排查](#8-死锁产生与排查)
- [9. 乐观锁 vs 悲观锁](#9-乐观锁-vs-悲观锁)
- [10. Python 并发复现脚本详解](#10-python-并发复现脚本详解)
- [11. 与 miniDB 的 2PL + MVCC 对照](#11-与-minidb-的-2pl--mvcc-对照)
- [12. 习题](#12-习题)
- [小结](#小结)

---

## 1. ACID 四性质回顾

事务是"要么全做、要么全不做"的一组操作。ACID 是事务必须满足的四个性质，是数据库区别于普通文件系统的根本标志。

### 1.1 一句话理解 ACID

| 性质 | 全称 | 一句话 | 现实类比 |
|---|---|---|---|
| **A** | Atomicity 原子性 | 全做或全不做 | 转账：扣款和加款必须同时成功或同时失败 |
| **C** | Consistency 一致性 | 数据始终合法 | 银行总账：转账前后总金额不变 |
| **I** | Isolation 隔离性 | 并发互不干扰 | 两人同时取款：不能看到对方的中间状态 |
| **D** | Durability 持久性 | 提交不丢 | 存折盖章：盖章后即使柜员断电，记录也在 |

### 1.2 各性质详细解释与现实类比

#### A — Atomicity（原子性）

**定义**：事务内的所有操作是一个不可分割的单元，要么全部成功提交，要么全部回滚，不存在"做了一半"的中间状态。

**银行转账类比**：
```
场景：Alice 给 Bob 转 100 元
操作1：Alice.balance -= 100   （扣款）
操作2：Bob.balance   += 100   （加款）

原子性要求：
  - 两个操作都成功 → 提交，永久生效
  - 任一操作失败 → 全部回滚，回到转账前状态
  - 不允许出现"扣了 Alice 的钱却没加给 Bob"的情况
```

**实现机制**：
- **WAL（Write-Ahead Log，预写日志）**：修改数据前先把"要做什么"记到日志
- **Undo Log（回滚日志）**：记录修改前的旧值，失败时用来回滚
- 崩溃后重启扫描 WAL：已提交的重做（Redo），未提交的回滚（Undo）

**违反原子性的后果**：钱凭空消失或凭空产生，账目对不上。

#### C — Consistency（一致性）

**定义**：事务执行前后，数据库始终满足所有预定义的完整性约束（主键、外键、CHECK、唯一索引、触发器等）。

**银行转账类比**：
```
约束：所有账户余额之和 = 银行总资产（恒定）

转账前：Alice=300, Bob=200, 总和=500
转账 100：
  Alice.balance -= 100  → Alice=200
  Bob.balance   += 100  → Bob=300
转账后：Alice=200, Bob=300, 总和=500  ← 约束依然满足

如果只做了一半：
  Alice=200, Bob=200, 总和=400  ← 违约！钱消失了
```

**关键认识**：**C 是应用层的责任**，数据库提供约束检查机制，但"什么是合法状态"由业务建模决定。A/I/D 是数据库机制保证的，C 是 A/I/D + 正确约束的推论。

**实现机制**：
- 主键约束、外键约束、NOT NULL
- CHECK 约束（如 `balance >= 0`）
- 唯一索引
- 触发器（复杂业务规则）

#### I — Isolation（隔离性）

**定义**：并发执行的事务互相独立，一个事务的中间状态对其他事务不可见，效果等价于某种串行执行。

**银行取款类比**：
```
场景：Alice 余额 100，两个柜员同时各取 80

无隔离（裸并发）：
  柜员A 读到 balance=100
  柜员B 读到 balance=100        ← 都看到旧值
  柜员A 写 balance=100-80=20
  柜员B 写 balance=100-80=20   ← 覆盖了 A 的写
  结果：余额 20，但取走了 160！钱凭空多出 60

有隔离（可串行化）：
  柜员A 读 balance=100 → 写 20 → 提交
  柜员B 读 balance=20  → 余额不足，拒绝
  或：柜员B 等待 A 提交后再读，看到 20，拒绝
```

**核心矛盾**：完全隔离（可串行化）代价高、并发低。于是有了**分级隔离**——用较低隔离换取更高并发，代价是允许某些异常。这是本章重点。

#### D — Durability（持久性）

**定义**：事务一旦提交，修改就永久保存，即使系统崩溃、断电、磁盘故障也不会丢失。

**银行存折类比**：
```
场景：柜员给你存 1000 元，盖章后告诉你"存好了"

持久性要求：
  - 盖章瞬间，记录已写入持久介质（存折/账本）
  - 即使柜员电脑马上断电，你的 1000 元也在
  - 不能"存了但没真写进账本"

实现：fsync 强制把数据从内存刷到磁盘
```

**实现机制**：
- **WAL + fsync**：提交时把日志强制刷盘，即使数据页还没写
- **同步复制**（PostgreSQL streaming replication）：提交后同步到备库
- **电池备份磁盘缓存**：RAID 卡带电池，断电后缓存不丢

**权衡**：每次提交都 fsync 很慢。可以牺牲一点持久性换性能：
- `synchronous_commit=off`：提交不等 fsync，性能高但崩溃可能丢最近提交
- `fsync=off`（极端）：完全不 fsync，最快但崩溃可能损坏数据库

### 1.3 ACID 实现机制对照表

| 性质 | 全称 | 含义 | miniDB 实现 | SQLite 实现 | PostgreSQL 实现 |
|---|---|---|---|---|---|
| **A** | Atomicity 原子性 | 事务内所有操作要么全做要么全不做 | WAL + undo | Rollback Journal / WAL | WAL + undo（abort 时） |
| **C** | Consistency 一致性 | 事务前后满足完整性约束 | 约束检查 | 约束 + 触发器 | 约束 + 触发器 + CHECK |
| **I** | Isolation 隔离性 | 并发事务互不干扰 | 2PL / MVCC | WAL + busy handler | MVCC + Snapshot |
| **D** | Durability 持久性 | 提交后即使崩溃也不丢 | WAL fsync | WAL fsync | WAL fsync + sync rep |

> **关键认识**：C 是应用层约束，A/I/D 是数据库机制。A 和 I 是数据库最核心的两个保证，D 依赖存储 fsync，C 依赖正确建模。

### 1.4 原子性实现：WAL + Undo 详解

```
事务 T 执行流程（ARIES 简化）：
  1. BEGIN：分配事务ID，记录 <T, BEGIN> 到 WAL
  2. 修改页 P：先写 <T, P, undo_image, redo_image> 到 WAL
  3. 在内存中修改 P（脏页）
  4. COMMIT：写 <T, COMMIT> 到 WAL，fsync
  5. ABORT：按 WAL 反向扫描，用 undo_image 回滚

崩溃恢复：
  Redo 阶段：重放所有已记录的 redo
  Undo 阶段：回滚未 COMMIT 的事务
```

**新手理解要点**：
1. **先写日志再改数据**（Write-Ahead）——这是"预写日志"名字的由来
2. 日志顺序追加，速度快；数据页随机写，慢
3. 提交时只 fsync 日志（小），数据页可以慢慢刷盘（异步）
4. 崩溃后日志完整 → 可以重做或回滚 → 数据恢复一致

### 1.5 隔离性实现：从串行化到弱隔离

理想隔离是**可串行化**（Serializable）：并发执行结果等价于某个串行执行。但完全可串行化代价高，于是有了分级隔离。

```
可串行化（Serializable）
  ↑ 代价越高，并发越低
  │
  ├─ Snapshot Isolation（PG 默认 RR 实际是 SI）
  │
  ├─ Repeatable Read
  │
  ├─ Read Committed（PG 默认）
  │
  ├─ Read Uncommitted
  │
  └─ 无隔离（并发裸读写）
```

**为什么要有分级**：完全可串行化要求所有事务像排队一样一个一个执行，并发度太低。实际业务中很多场景能容忍某些异常，于是降低隔离级别换取并发性能。**选隔离级别 = 在正确性和性能之间权衡**。

---

## 2. 四级隔离级别

### 2.1 SQL 标准定义的四级

| 级别 | 脏读 | 不可重复读 | 幻读 | 写偏斜 | PostgreSQL | SQLite |
|---|---|---|---|---|---|---|
| **Read Uncommitted** | ✓ 可能 | ✓ 可能 | ✓ 可能 | ✓ 可能 | 降级为 RC | 不支持 |
| **Read Committed** | ✗ 防止 | ✓ 可能 | ✓ 可能 | ✓ 可能 | 支持 | 默认（WAL） |
| **Repeatable Read** | ✗ 防止 | ✗ 防止 | ✓ 可能 | ✓ 可能 | 实为 Snapshot Isolation | 支持 |
| **Serializable** | ✗ 防止 | ✗ 防止 | ✗ 防止 | ✗ 防止 | SSI 真可串行化 | 支持 |

> **重要陷阱**：
> - SQL 标准的 Repeatable Read 允许幻读，但 **PostgreSQL 的 RR 实际是 Snapshot Isolation**，已防止幻读，但仍允许写偏斜。
> - SQLite 的"Serializable"在 WAL 模式下实际是 Snapshot Isolation，不是真可串行化。
> - 只有 PostgreSQL 的 Serializable 用 **SSI（Serializable Snapshot Isolation）** 算法实现真可串行化。

### 2.2 级别关系图（能力与代价）

```
隔离能力（防止异常的能力）        并发代价（性能损失）
  强                              低
  ↑                              ↑
Serializable  防止所有四种       最低并发，可能 abort 重试
  │                              │
Repeatable Read (PG=SI)  防前三种 + 幻读   中等
  │                              │
Read Committed  防脏读           较高并发
  │                              │
Read Uncommitted  几乎不防        最高并发（但几乎没人用）
  ↓                              ↓
  弱                             高
```

### 2.3 各级别语义详解

#### 2.3.1 Read Uncommitted（读未提交）

**含义**：允许读到其他事务**未提交**的数据（脏读）。

**现实类比**：
```
柜员A 正在办理转账，刚扣了 Alice 的钱还没加给 Bob，
柜员B 此时查 Alice 余额，看到了扣款后的数字。
柜员A 转账失败回滚，Alice 余额恢复。
柜员B 看到的是一个"根本不存在"的中间状态。
```

**特点**：
- 几乎没有数据库真正实现这个级别
- PostgreSQL 收到 RU 请求会**静默降级为 RC**
- SQLite 完全不支持
- 理论价值大于实用价值（用于理解隔离级别阶梯）

#### 2.3.2 Read Committed（读已提交）

**含义**：
- 每条语句执行时获取**新的快照**
- 只能看到已提交的数据
- 同一事务内两次相同查询可能得到不同结果（不可重复读）

**现实类比**：
```
你查余额，看到 100（已提交的真实数据）。
另一笔交易把余额改成 200 并提交。
你再次查余额，看到 200。
两次查询结果不同，但每次看到的都是"真实已提交"的状态。
```

```sql
-- PostgreSQL Read Committed 行为
BEGIN ISOLATION LEVEL READ COMMITTED;
  SELECT balance FROM accounts WHERE id = 1;  -- 100
  -- [另一事务把 balance 改成 200 并提交]
  SELECT balance FROM accounts WHERE id = 1;  -- 200  ← 变了！
COMMIT;
```

**适用场景**：
- 大多数 OLTP 业务（PostgreSQL 默认）
- 能容忍同一事务内数据变化
- 短事务、简单读写

#### 2.3.3 Repeatable Read（可重复读）

**含义**：
- 事务开始时获取**一次快照**，整个事务用同一快照
- 同一事务内两次相同查询结果一致
- SQL 标准允许幻读，但 PG/SI 不允许

**现实类比**：
```
你打开银行App查余额，看到 100。
此时另一笔交易把余额改成 200 并提交。
你刷新余额，仍然看到 100（用打开时的快照）。
你关闭App重开，才看到 200。
整个会话期间，你看到的是"打开那一刻的快照"。
```

```sql
-- PostgreSQL Repeatable Read（实为 Snapshot Isolation）
BEGIN ISOLATION LEVEL REPEATABLE READ;
  SELECT balance FROM accounts WHERE id = 1;  -- 100
  -- [另一事务把 balance 改成 200 并提交]
  SELECT balance FROM accounts WHERE id = 1;  -- 100  ← 不变
COMMIT;
```

**适用场景**：
- 报表生成（需要一致快照）
- 多步读-决策-写操作
- 不能容忍不可重复读的业务

#### 2.3.4 Serializable（可串行化）

**含义**：
- 保证并发执行结果等价于某个串行执行
- PostgreSQL 用 SSI 算法，检测到冲突时抛 `could not serialize access due to read/write dependencies` 错误
- 代价：可能 abort 重试

**现实类比**：
```
银行规定"同一时刻只能有一个柜员修改账户"。
Serializable 就像强制排队：
  柜员A 操作时，柜员B 必须等 A 做完才能操作。
  结果永远等价于"先A后B"或"先B后A"的某个串行顺序。
  不会出现并发导致的中间态问题。
代价：并发度低，可能需要重试。
```

```sql
-- PostgreSQL Serializable
BEGIN ISOLATION LEVEL SERIALIZABLE;
  -- ... 读写操作 ...
COMMIT;  -- 可能抛 serialization_failure，需重试
```

**适用场景**：
- 写偏斜敏感的业务（医院排班、库存约束）
- 对正确性要求极高
- 愿意接受重试代价

### 2.4 各级别设置方式

```sql
-- PostgreSQL：会话级
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SET TRANSACTION ISOLATION LEVEL READ COMMITTED;
SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;

-- PostgreSQL：事务级
BEGIN ISOLATION LEVEL SERIALIZABLE;
  -- ...
COMMIT;

-- PostgreSQL：全局默认
SET default_transaction_isolation = 'serializable';

-- SQLite：通过 PRAGMA 和驱动层
PRAGMA journal_mode=WAL;       -- 启用 SI
-- SQLite 不支持 SET TRANSACTION ISOLATION LEVEL
-- 通过 Python 驱动层的 isolation_level 参数控制
```

```python
import sqlite3
# SQLite 通过 isolation_level 参数控制
conn = sqlite3.connect('test.db', isolation_level='IMMEDIATE')  # 相当于 RR
conn = sqlite3.connect('test.db', isolation_level=None)         # autocommit
conn = sqlite3.connect('test.db', isolation_level='EXCLUSIVE')  # 立即获取写锁
```

---

## 3. 脏读（Dirty Read）

### 3.1 定义

事务 T2 读到了事务 T1 **未提交**的数据，若 T1 随后回滚，T2 就读到了根本不存在的值。

### 3.2 时序图

```
时间 →
T1:  BEGIN     UPDATE x=2          ROLLBACK
                    ↓
T2:           BEGIN  READ x(=2!)   COMMIT
                       ↑ 脏读！读到了未提交的 2
                       ↑ T1 回滚后，这个 2 根本不存在
```

**详细步骤分解**：
```
时刻 t1: T1 BEGIN
时刻 t2: T1 UPDATE x=2      （x 在内存中改为 2，未提交）
时刻 t3: T2 BEGIN
时刻 t4: T2 READ x           （读到 2，因为 RU 允许读未提交）
时刻 t5: T2 COMMIT           （基于错误的 2 做了决策并提交）
时刻 t6: T1 ROLLBACK         （x 恢复为 1，但 T2 已经用了错误的 2）
后果：T2 基于一个"从未存在过"的值做了不可撤回的决策
```

### 3.3 现实危害

基于脏数据做决策，导致逻辑错误。例如：
- 转账中读到中间态余额，做了超额扣款
- 报表统计包含未提交的临时数据，数字对不上
- 库存系统读到未提交的扣减，以为没货，拒绝正常订单

### 3.4 复现步骤

**前提**：需要 Read Uncommitted 隔离级别。

**现实限制**：SQLite 和 PostgreSQL 都不支持脏读：
- PostgreSQL 的 RU 会**静默降级为 RC**，无法脏读
- SQLite 完全不支持 RU
- 因此**无法在真实数据库复现真脏读**，只能用模拟说明

**模拟复现步骤**：
1. 建表 `accounts(id, balance)`，插入 `id=1, balance=100`
2. T1 在 RU 级别 BEGIN，UPDATE balance=200（不提交）
3. T2 在 RU 级别 BEGIN，SELECT balance（若真脏读会得到 200）
4. T1 ROLLBACK
5. T2 再次 SELECT（得到 100，但之前已用过 200 做决策）

### 3.5 如何避免

**最简单**：使用 Read Committed 或更高级别。所有主流数据库默认就防脏读。

```python
# 见 concurrency_demos/dirty_read.py
# SQLite 无法真正脏读，脚本演示"如果允许脏读会发生什么"
# 并对比 Read Committed 下确实不会脏读
```

### 3.6 防止原理

```
Read Committed 防脏读原理：
  每条语句获取新快照，快照只包含"已提交"的数据
  T1 未提交的修改不在任何快照里 → T2 看不到
  T1 提交后，新快照才包含其修改
```

---

## 4. 不可重复读（Non-Repeatable Read）

### 4.1 定义

事务 T2 内，同一查询执行两次，结果不同，因为 T1 在中间**修改并提交**了已有行。

### 4.2 时序图

```
时间 →
T1:  BEGIN          UPDATE x=2; COMMIT
                        ↓
T2:  BEGIN  READ x(=1)        READ x(=2!)  COMMIT
                              ↑ 不可重复读！同一事务内 x 变了
```

**详细步骤分解**：
```
时刻 t1: T2 BEGIN
时刻 t2: T2 READ x           （得到 1，RC 下每条语句新快照）
时刻 t3: T1 BEGIN
时刻 t4: T1 UPDATE x=2
时刻 t5: T1 COMMIT           （x 现在是 2，已提交）
时刻 t6: T2 READ x           （RC 下取新快照，得到 2）
时刻 t7: T2 COMMIT
后果：T2 内两次读 x 结果不同，可能基于第一次读做错误决策
```

### 4.3 与脏读的区别

| 异常 | 读到的是 | 提交状态 | 危害程度 |
|---|---|---|---|
| 脏读 | 未提交数据 | 未提交 | 高（数据可能根本不存在） |
| 不可重复读 | 已提交数据 | 已提交 | 中（数据真实，但事务内不一致） |

**关键区别**：脏读读"未提交"，不可重复读读"已提交"。不可重复读读到的值是真实的，只是事务内一致性被破坏。

### 4.4 现实危害

- 报表生成中，第一次读总数 100，第二次读 101，报表自相矛盾
- 读-决策-写模式：读余额 100 → 决定扣 80 → 中间被改 → 写入错误
- 转账校验：第一次读 Alice 余额够，准备扣款，中间被别的交易改了，扣款后变负

### 4.5 复现步骤

**前提**：Read Committed 级别（PostgreSQL 默认）。

**复现步骤**：
1. 建表 `accounts(id, balance)`，插入 `id=1, balance=100`
2. T2 在 RC 级别 BEGIN
3. T2 SELECT balance WHERE id=1 → 得到 100
4. T1 在 RC 级别 BEGIN，UPDATE balance=200 WHERE id=1，COMMIT
5. T2 再次 SELECT balance WHERE id=1 → 得到 200（变了！）
6. T2 COMMIT

### 4.6 如何避免

**方法**：使用 Repeatable Read 或更高级别。

**原理**：RR/SI 在事务开始时取一次快照，整个事务用同一快照，中间别人的提交对当前事务不可见。

```python
# 见 concurrency_demos/non_repeatable_read.py
# 在 Read Committed 下复现，在 Repeatable Read 下防止
```

### 4.7 防止原理

```
Repeatable Read / Snapshot Isolation 防不可重复读原理：
  事务开始时拍快照 S，记录"此刻已提交的数据"
  整个事务期间所有读都用 S
  T1 中间提交的修改 xmin > S.xmin → 对 T2 不可见
  T2 两次读 x 都得到快照中的值 → 一致
```

---

## 5. 幻读（Phantom Read）

### 5.1 定义

事务 T2 内，同一**范围查询**执行两次，结果集行数不同，因为 T1 在中间**插入并提交**了新行（满足范围条件）。

### 5.2 时序图

```
时间 →
T1:  BEGIN          INSERT (x=5, 满足范围); COMMIT
                        ↓
T2:  BEGIN  COUNT(WHERE x>0)=100   COUNT(WHERE x>0)=101!  COMMIT
                                   ↑ 幻读！多了一行"幻影"
```

**详细步骤分解**：
```
时刻 t1: T2 BEGIN
时刻 t2: T2 SELECT COUNT(*) WHERE x>0   （得到 100）
时刻 t3: T1 BEGIN
时刻 t4: T1 INSERT (x=5)               （满足 x>0 范围）
时刻 t5: T1 COMMIT
时刻 t6: T2 SELECT COUNT(*) WHERE x>0  （RC 下得到 101）
时刻 t7: T2 COMMIT
后果：T2 内范围查询行数变化，多了一行"幻影"
```

### 5.3 与不可重复读的区别

| 异常 | 变化类型 | 操作 | SQL 标准 RR 能否防 |
|---|---|---|---|
| 不可重复读 | 已有行的值被修改 | UPDATE | 能防 |
| 幻读 | 新行插入或旧行删除 | INSERT / DELETE | **不能防**（标准） |

**关键区别**：
- 不可重复读：**已有行**的值被修改（UPDATE）
- 幻读：**新行被插入**或旧行被删除（INSERT/DELETE），导致结果集**成员**变化

### 5.4 现实危害

- 统计在线人数：第一次 100，第二次 101，趋势图抖动
- 唯一性检查：第一次查"无同名用户"，准备插入，中间别人插了同名，自己再插就冲突
- 范围分页：第一次查第 1-10 条，中间插了新行，第二次查第 11-20 条，结果重复或遗漏

### 5.5 复现步骤

**SQL 标准下**：Repeatable Read 级别（标准允许幻读）。

**PostgreSQL 实际行为**：PG 的 RR 是 Snapshot Isolation，**不会**幻读。要在 PG 复现幻读需用 Read Committed。

**复现步骤（RC 下）**：
1. 建表 `items(id, val)`，插入 100 行 val>0
2. T2 在 RC 级别 BEGIN
3. T2 SELECT COUNT(*) WHERE val>0 → 得到 100
4. T1 在 RC 级别 BEGIN，INSERT (val=5)，COMMIT
5. T2 再次 SELECT COUNT(*) WHERE val>0 → 得到 101（幻读！）
6. T2 COMMIT

### 5.6 如何避免

**方法**：
- SQL 标准：Serializable
- PostgreSQL：Repeatable Read（因 SI）即可
- 也可用谓词锁（predicate lock）或 `SELECT ... FOR UPDATE` 加范围锁

```python
# 见 concurrency_demos/phantom_read.py
# 在 Read Committed 下复现幻读
# 在 Repeatable Read（SQLite/PG SI）下防止
```

### 5.7 防止原理

```
Snapshot Isolation 防幻读原理：
  事务开始时拍快照 S
  T1 新插入的行 xmin > S.xmin → 对 T2 不可见
  T2 两次范围查都只看到快照前的行 → 行数一致

谓词锁防幻读原理（标准 Serializable）：
  T2 的范围查询在"val>0"这个谓词上加共享谓词锁
  T1 要插入满足 val>0 的行 → 与谓词锁冲突 → 阻塞或 abort
```

---

## 6. 写偏斜（Write Skew）

### 6.1 定义

两个事务各自读取重叠的数据集，各自修改**不相交**的子集，单独看都合法，合起来破坏全局约束。Snapshot Isolation 无法防止。

### 6.2 经典案例：医院排班

**业务规则**：至少留 1 人值班。

**初始状态**：Alice 和 Bob 都在值班（on_call=true），count=2。

**并发场景**：
- Alice 想下班，查"当前值班人数 ≥ 2 吗？" → 是 → 把自己改成下班
- Bob 想下班，查"当前值班人数 ≥ 2 吗？" → 是 → 把自己改成下班
- 两人都提交 → 无人值班 → 违约！

### 6.3 时序图

```
时间 →
T1(Alice):  BEGIN  READ count(on_call)=2  UPDATE Alice=off  COMMIT
T2(Bob):    BEGIN  READ count(on_call)=2  UPDATE Bob=off    COMMIT

约束：count(on_call) >= 1
初始：Alice=on, Bob=on  → count=2
结果：Alice=off, Bob=off → count=0  ← 违约！
```

**详细步骤分解**：
```
时刻 t1: T1 BEGIN
时刻 t2: T1 SELECT count(*) WHERE on_call   （得到 2）
时刻 t3: T2 BEGIN
时刻 t4: T2 SELECT count(*) WHERE on_call   （得到 2，T1 还没提交）
时刻 t5: T1 UPDATE Alice.on_call=false      （写 Alice 行）
时刻 t6: T1 COMMIT                          （Alice 下班）
时刻 t7: T2 UPDATE Bob.on_call=false        （写 Bob 行，与 T1 不冲突）
时刻 t8: T2 COMMIT                          （Bob 下班）
后果：Alice=off, Bob=off, count=0，违反"至少1人值班"约束
```

### 6.4 为什么只有 Serializable 才能防止

**为什么 Snapshot Isolation 防不住**：
- SI 检查的是 **write-write 冲突**（两个事务写同一行）
- 写偏斜中 T1 写 Alice、T2 写 Bob，**没有写写冲突**，SI 放行
- 但存在 **rw 依赖**：T1 读 Bob 的状态决定写 Alice，T2 读 Alice 的状态决定写 Bob，形成危险结构

**为什么 RR（标准）也防不住**：
- 标准 RR 只保证"已读行的值可重复"，不保证"读到的行集不变"
- 写偏斜涉及的是"读行集 + 写不同行集"，超出 RR 保证范围

**为什么只有 Serializable（SSI）能防**：
- SSI 额外追踪 **rw 依赖**（读-写依赖）
- 检测到 T1 读←→T2 写 形成**环**时，abort 其中一个
- 这是 SSI 相对 SI 的核心增强

### 6.5 其他写偏斜例子

**例子1：防超卖（约束：同一商品最多 2 人购买）**
```
初始：商品 G 有 0 人购买，limit=2
T1: 查"已购<2?" → 是 → 插入用户A购买记录
T2: 查"已购<2?" → 是 → 插入用户B购买记录
结果：2 人购买，合法
但如果 limit=1：
T1: 查"已购<1?" → 是 → 插入
T2: 查"已购<1?" → 是 → 插入
结果：2 人购买，超卖！
```

**例子2：资金转移（约束：总余额 ≥ 0）**
```
初始：账户 A=100, B=100, 总和=200
约束：A + B >= 100（保底）
T1: 查 A+B=200 ≥ 100 → A -= 150 → A=-50
T2: 查 A+B=200 ≥ 100 → B -= 150 → B=-50
结果：A+B=-100，违约！
```

### 6.6 复现步骤

**前提**：Repeatable Read / Snapshot Isolation。

**复现步骤**：
1. 建表 `doctors(name, on_call)`，插入 Alice=on, Bob=on
2. T1 在 RR 级别 BEGIN
3. T1 SELECT count(*) WHERE on_call → 得到 2
4. T2 在 RR 级别 BEGIN
5. T2 SELECT count(*) WHERE on_call → 得到 2
6. T1 UPDATE Alice.on_call=false，COMMIT
7. T2 UPDATE Bob.on_call=false，COMMIT
8. SELECT count(*) WHERE on_call → 得到 0（违约！）

### 6.7 如何避免

**方法**：
- **Serializable**（PG SSI 会检测并 abort）
- **悲观锁**：`SELECT ... FOR UPDATE` 锁住读到的行
- **乐观锁**：约束检查 + 重试
- **物化约束**：用触发器或 CHECK 在提交时强制检查

```python
# 见 concurrency_demos/write_skew.py
# 在 Repeatable Read 下复现写偏斜
# 在 Serializable 下复现被阻止（SQLite 不支持真 SSI，需 PG）
# 用 SELECT FOR UPDATE 演示悲观锁防止
```

### 6.8 悲观锁防止写偏斜示例

```sql
-- 用 SELECT FOR UPDATE 锁住读到的行
BEGIN;
SELECT name FROM doctors WHERE on_call = true FOR UPDATE;  -- 锁住所有值班行
-- 此时 T2 的同样 SELECT FOR UPDATE 会阻塞，等 T1 提交
SELECT count(*) FROM doctors WHERE on_call = true;  -- 2
UPDATE doctors SET on_call = false WHERE name = 'Alice';
COMMIT;  -- 释放锁

-- T2 现在才能拿到锁，看到 count=1，不再下班
```

---

## 7. PostgreSQL 的 Snapshot Isolation 与 SSI

### 7.1 快照隔离（Snapshot Isolation）原理

**核心思想**：每个事务开始时拍一张"快照"（记录当前所有活跃事务），整个事务只看到快照之前已提交的数据。

```
事务 T 的快照 S = (xmin, xmax, xip)
  xmin：T 之前所有已提交事务的最大ID + 1
  xmax：T 的事务ID
  xip：T 开始时仍活跃的事务ID列表

元组 V（由事务 V.xmin 创建，V.xmax 删除）对 T 可见：
  1. V.xmin < T.xmin 且 V.xmin 已提交 且 V.xmin ∉ xip  （创建者已提交且在快照前）
  2. V.xmax == 0 或 V.xmax >= T.xmin 或 V.xmax ∈ xip    （未被已提交事务删除）
```

**新手理解**：
```
快照就像拍照：
  - 按快门那一刻，记录"此刻已发生的事"（已提交事务列表）
  - 拍完之后发生的事（新提交），照片里看不到
  - 整个事务期间都用这张照片，所以看到的数据一致

xmin = "照片时间戳"
xip = "拍照时还在进行中的事务"（这些事务的提交对当前事务不可见）
```

### 7.2 Snapshot Isolation 能防什么

| 异常 | SI 能否防止 | 原因 |
|---|---|---|
| 脏读 | ✓ | 只看已提交 |
| 不可重复读 | ✓ | 整个事务用同一快照 |
| 幻读 | ✓ | 新插入行 xmin > 快照，不可见 |
| 写偏斜 | ✗ | 无写写冲突，SI 检测不到 |
| 丢失更新 | ✓ | First-commit-wins，后提交者 abort |

### 7.3 为什么 RR（SI）不能防写偏斜

**根本原因**：SI 的冲突检测只看 **write-write**，不看 **read-write**。

```
写偏斜的冲突结构：
  T1: 读{Alice, Bob}  写{Alice}
  T2: 读{Alice, Bob}  写{Bob}

SI 检测：
  T1 写 {Alice}, T2 写 {Bob} → 不相交 → 无 ww 冲突 → 放行

但实际存在 rw 依赖：
  T1 读 Bob → Bob 状态影响 T1 的决策
  T2 写 Bob → T2 改了 Bob 状态
  形成 T1 --rw--> T2
  同理 T2 --rw--> T1
  形成环 → 危险结构 → 应该 abort 一个
```

**SI 的盲区**：只看写写冲突，看不到"读影响写决策"的间接依赖。

### 7.4 SSI（Serializable Snapshot Isolation）

PostgreSQL 9.1+ 用 SSI 实现真可串行化。核心是检测 **rw 依赖环**。

```
写偏斜的危险结构：
  T1 --rw--> T2 --rw--> T1  （形成环）

SSI 检测：
  每个 SIREAD 锁记录"谁读了什么"
  写操作检查是否与已有 SIREAD 形成冲突
  检测到环就 abort 其中一个事务
```

**SSI 工作流程**：
```
1. 事务读数据时，在读的行/页/表上加 SIREAD 锁（不阻塞写，只记录"谁读了"）
2. 事务写数据时，检查被写的行是否有别人的 SIREAD 锁
   - 有 → 形成 rw 依赖，记录到依赖图
3. 提交时检查依赖图是否有环
   - 有环 → abort 一个事务，抛 serialization_failure
   - 无环 → 提交成功
```

### 7.5 SSI 实战演示

```sql
-- PostgreSQL SSI 演示
SET default_transaction_isolation = 'serializable';

-- 会话1
BEGIN;
SELECT count(*) FROM doctors WHERE on_call = true;  -- 2
UPDATE doctors SET on_call = false WHERE name = 'Alice';
COMMIT;  -- 成功

-- 会话2（并发）
BEGIN;
SELECT count(*) FROM doctors WHERE on_call = true;  -- 2
UPDATE doctors SET on_call = false WHERE name = 'Bob';
COMMIT;  -- ERROR: could not serialize access due to read/write dependencies
```

### 7.6 序列化冲突检测细节

**SIREAD 锁层级**：
```
表级 SIREAD 锁
  └─ 页级 SIREAD 锁
       └─ 行级 SIREAD 锁

读操作：从表到行，按需加 SIREAD 锁（不阻塞任何写）
写操作：检查被写行的 SIREAD 锁
  - 若有其他事务的 SIREAD 锁 → 记录 rw 依赖
  - 检查依赖图是否出现环
```

**冲突类型**：
```
rw-conflict：T1 读 R，T2 写 R → T1 --rw--> T2
ww-conflict：T1 写 R，T2 写 R → SI 已处理（first-commit-wins）

危险结构（形成环）：
  T1 --rw--> T2 --rw--> T1  → 写偏斜
  T1 --rw--> T2 --rw--> T3 --rw--> T1  → 三事务写偏斜
```

### 7.7 SQLite 的隔离级别

SQLite 的隔离模型较特殊：

| 模式 | 隔离行为 |
|---|---|
| **ROLLBACK JOURNAL（默认）** | 串行化（写时全库锁，读时共享锁） |
| **WAL** | 快照隔离（读不阻塞写，写不阻塞读） |

```sql
-- SQLite 设置隔离级别（通过 PRAGMA）
PRAGMA journal_mode=WAL;       -- 启用 SI
-- SQLite 不支持 SET TRANSACTION ISOLATION LEVEL
-- 通过 Python 驱动层的 isolation_level 参数控制
```

```python
import sqlite3
conn = sqlite3.connect('test.db', isolation_level='IMMEDIATE')  # 相当于 RR
conn = sqlite3.connect('test.db', isolation_level=None)         # autocommit
```

**重要**：SQLite 的"Serializable"在 WAL 模式下实际是 Snapshot Isolation，**不是真可串行化**，防不住写偏斜。

### 7.8 异常关系总结

```
              脏读
                ↑
          Read Uncommitted 防止
                ↑
          不可重复读
                ↑
          Read Committed 防止
                ↑
            幻读
                ↑
          Repeatable Read 防止（SQL标准）
          Snapshot Isolation 防止（PG/SI）
                ↑
            写偏斜
                ↑
          Serializable 防止（SSI）
```

---

## 8. 死锁产生与排查

### 8.1 死锁定义

两个或多个事务互相等待对方持有的锁，形成循环依赖，永远无法推进。

### 8.2 时序图

```
时间 →
T1:  LOCK(A)          LOCK(B) [等待T2]
                          ↓
T2:          LOCK(B)          LOCK(A) [等待T1]
                              ↓
                    死锁！T1等T2的B，T2等T1的A
```

**详细步骤分解**：
```
时刻 t1: T1 LOCK(A) 成功   （T1 持有 A）
时刻 t2: T2 LOCK(B) 成功   （T2 持有 B）
时刻 t3: T1 LOCK(B) 等待   （B 被 T2 持有，T1 阻塞）
时刻 t4: T2 LOCK(A) 等待   （A 被 T1 持有，T2 阻塞）
状态：T1 等 T2 释放 B，T2 等 T1 释放 A → 永远等下去
```

### 8.3 死锁的必要条件（Coffman 四条件）

死锁发生必须**同时**满足以下四个条件，缺一不可：

#### 条件1：互斥（Mutual Exclusion）
资源同一时刻只能被一个事务持有。
```
现实类比：单人卫生间，同一时刻只能一个人用。
数据库：X 锁（写锁）互斥，同一行同时只能一个事务写。
```

#### 条件2：持有并等待（Hold and Wait）
事务持有部分锁的同时，请求新锁。
```
现实类比：你占着卫生间A，又想去卫生间B，但B有人，你就等着。
数据库：T1 持有 A 的锁，又请求 B 的锁，B 被 T2 持有，T1 等待。
```

#### 条件3：不可抢占（No Preemption）
不能强行夺走别人持有的锁，只能等对方主动释放。
```
现实类比：你不能把卫生间里的人拖出来，只能等他出来。
数据库：锁不能被抢，只能等持有者提交/回滚后释放。
```

#### 条件4：循环等待（Circular Wait）
存在事务的环形等待链。
```
现实类比：A等B，B等C，C等A，三人互相等。
数据库：T1等T2的锁，T2等T1的锁，形成环。
```

> **破坏任一条件即可避免死锁**。最实用的策略是破坏"循环等待"——让所有事务按固定顺序加锁。

### 8.4 数据库的死锁处理

| 数据库 | 检测方式 | 解决方式 |
|---|---|---|
| **PostgreSQL** | 等待图周期检测（默认 1s） | abort 代价最小的事务，抛 `deadlock_detected` |
| **SQLite** | 不会死锁（全库写锁） | — |
| **miniDB** | 超时 + 等待图 | abort 或等待超时 |

**PostgreSQL 死锁检测流程**：
```
1. 事务等待锁超过 deadlock_timeout（默认 1s）
2. 触发等待图检测
3. 构建等待图：节点=事务，边=等待关系
4. 检测图中是否有环
5. 有环 → 选一个事务 abort（通常选代价最小的）
6. 抛 ERROR: deadlock detected
```

### 8.5 死锁排查方法

```sql
-- PostgreSQL 死锁排查
-- 1. 查看最近死锁
SELECT * FROM pg_stat_activity WHERE state = 'active';

-- 2. 查看锁等待
SELECT l.locktype, l.relation::regclass, l.pid, l.mode, l.granted,
       a.query
FROM pg_locks l
JOIN pg_stat_activity a ON l.pid = a.pid
WHERE NOT l.granted;

-- 3. 死锁日志（postgresql.conf 配置）
-- log_locks = on
-- deadlock_timeout = '200ms'

-- 4. 查看最近死锁详情（日志）
-- grep "deadlock detected" /var/log/postgresql/postgresql.log
```

**排查步骤**：
1. 从日志找到 `deadlock detected` 错误
2. 查看当时两个事务的 SQL（日志会打印）
3. 分析加锁顺序，找出循环
4. 调整加锁顺序，使所有事务一致

### 8.6 死锁避免策略

#### 策略1：固定加锁顺序（最常用）
所有事务按同一顺序访问资源。
```
坏：T1 先锁A后锁B，T2 先锁B后锁A → 可能死锁
好：T1 先锁A后锁B，T2 先锁A后锁B → 不会死锁

实现：按主键排序，所有事务都按 id 升序加锁
```

#### 策略2：一次性加锁
事务开始时锁定所有需要的资源。
```
T1: LOCK(A), LOCK(B)  -- 一次性
T2: LOCK(A), LOCK(B)  -- 一次性
要么全拿到，要么全拿不到，不会死锁
缺点：降低并发
```

#### 策略3：短事务
减少持锁时间，降低死锁概率。
```
好：事务只包含必要操作，尽快提交
坏：事务里夹着网络请求、用户输入等待
```

#### 策略4：乐观并发控制
读时不加锁，提交时检测冲突。
```
读时不锁 → 不会互相等待 → 不会死锁
提交时检测冲突 → 冲突则重试
```

```python
# 见 concurrency_demos/deadlock.py
# 复现 T1 先锁A后锁B、T2 先锁B后锁A 的死锁
# 演示固定加锁顺序避免死锁
```

### 8.7 死锁复现步骤

**复现步骤**：
1. 建表 `accounts(id, balance)`，插入 id=1 和 id=2
2. T1: BEGIN, UPDATE accounts SET balance=0 WHERE id=1（锁住 id=1）
3. T2: BEGIN, UPDATE accounts SET balance=0 WHERE id=2（锁住 id=2）
4. T1: UPDATE accounts SET balance=0 WHERE id=2（等 T2 释放）
5. T2: UPDATE accounts SET balance=0 WHERE id=1（等 T1 释放）
6. 死锁！数据库检测到并 abort 一个事务

**避免**：让 T1 和 T2 都先锁 id=1 再锁 id=2。

---

## 9. 乐观锁 vs 悲观锁

### 9.1 悲观锁（Pessimistic Locking）

**假设**：冲突很可能发生，先锁住再操作。

**现实类比**：公共卫生间装锁，进去就锁门，别人必须等。

```sql
-- SELECT ... FOR UPDATE 悲观锁
BEGIN;
SELECT balance FROM accounts WHERE id = 1 FOR UPDATE;  -- 加行锁
-- 其他事务修改 id=1 会阻塞
UPDATE accounts SET balance = balance - 100 WHERE id = 1;
COMMIT;  -- 释放锁
```

**工作流程**：
```
1. SELECT ... FOR UPDATE → 加 X 锁，其他事务的写/锁读都阻塞
2. 应用层读取数据
3. 应用层计算新值
4. UPDATE 写入新值
5. COMMIT 释放锁

特点：持锁期间其他事务必须等
```

| 优点 | 缺点 |
|---|---|
| 确保成功 | 降低并发 |
| 逻辑简单 | 可能死锁 |
| 适合写多 | 持锁时间长 |

**适用场景**：
- 冲突率高（写多读少）
- 长事务、复杂校验
- 必须确保成功的场景

### 9.2 乐观锁（Optimistic Locking）

**假设**：冲突很少，先操作，提交时检测冲突。

**现实类比**：Git 提交，先在本地改，push 时检测冲突，冲突了再合并。

#### 方式1：版本号
```sql
BEGIN;
SELECT balance, version FROM accounts WHERE id = 1;  -- balance=100, version=5
-- 应用层计算
UPDATE accounts SET balance = 50, version = version + 1
  WHERE id = 1 AND version = 5;  -- 0 行受影响说明被别人改了
COMMIT;
```

#### 方式2：CAS（Compare And Swap）
```sql
UPDATE accounts SET balance = 50
  WHERE id = 1 AND balance = 100;  -- 0 行受影响说明 balance 已变
```

**工作流程**：
```
1. SELECT 读取数据 + 版本号（不加锁）
2. 应用层计算新值
3. UPDATE ... WHERE version = 读到的版本号
4. 检查 rowcount：
   - 1 → 成功
   - 0 → 被别人改了，重试或报错

特点：读不加锁，提交时才检测冲突
```

| 优点 | 缺点 |
|---|---|
| 高并发 | 冲突需重试 |
| 无死锁 | 适合读多 |
| 持锁时间短 | 需要版本字段 |

**适用场景**：
- 冲突率低（读多写少）
- 短事务、简单更新
- 高并发场景

### 9.3 对比表

| 维度 | 悲观锁 | 乐观锁 |
|---|---|---|
| **假设** | 冲突多 | 冲突少 |
| **加锁时机** | 操作前 | 提交时 |
| **持锁时长** | 长 | 短（几乎不持锁） |
| **死锁** | 可能 | 不可能 |
| **冲突处理** | 等待 | 重试 |
| **并发度** | 低 | 高 |
| **实现** | SELECT FOR UPDATE | 版本号 / CAS |
| **适合** | 写多、长事务 | 读多、短事务 |

### 9.4 选择依据

```
冲突率低 + 读多写少  →  乐观锁
冲突率高 + 写多       →  悲观锁
长事务 + 复杂校验     →  悲观锁
短事务 + 简单更新     →  乐观锁
需要确保成功         →  悲观锁
高并发优先           →  乐观锁
```

### 9.5 乐观锁实战：库存扣减

```python
# 乐观锁扣减库存
def deduct_optimistic(conn, item_id, qty):
    for attempt in range(3):
        stock, version = conn.execute(
            "SELECT stock, version FROM items WHERE id=?", (item_id,)
        ).fetchone()
        if stock < qty:
            return False, "库存不足"
        cur = conn.execute(
            "UPDATE items SET stock=stock-?, version=version+1 "
            "WHERE id=? AND version=?",
            (qty, item_id, version)
        )
        if cur.rowcount == 1:
            return True, "扣减成功"
    return False, "冲突重试耗尽"
```

### 9.6 悲观锁实战：库存扣减

```python
# 悲观锁扣减库存
def deduct_pessimistic(conn, item_id, qty):
    conn.execute("BEGIN IMMEDIATE")  # 获取写锁
    stock = conn.execute(
        "SELECT stock FROM items WHERE id=?", (item_id,)
    ).fetchone()[0]
    if stock < qty:
        conn.execute("ROLLBACK")
        return False, "库存不足"
    conn.execute(
        "UPDATE items SET stock=stock-? WHERE id=?",
        (qty, item_id)
    )
    conn.execute("COMMIT")
    return True, "扣减成功"
```

---

## 10. Python 并发复现脚本详解

### 10.1 环境准备

```bash
# SQLite（Python 内置，无需安装）
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"

# PostgreSQL（可选，用于 SSI 演示）
docker run --name pg -e POSTGRES_PASSWORD=secret -p 5432:5432 -d postgres:16
pip install psycopg2-binary
```

### 10.2 脚本一览

| 脚本 | 演示内容 | 复现的异常 |
|---|---|---|
| `dirty_read.py` | 脏读模拟 + RC 防止 | 脏读 |
| `non_repeatable_read.py` | RC 复现 / RR 防止 | 不可重复读 |
| `phantom_read.py` | RC 复现 / RR(SI) 防止 | 幻读 |
| `write_skew.py` | RR 复现 / 悲观锁防止 | 写偏斜 |
| `deadlock.py` | 死锁复现 + 顺序加锁避免 | 死锁 |
| `isolation_levels.py` | 四级隔离对比 | 综合 |

### 10.3 运行方式

```bash
cd phase2/04-transaction/concurrency_demos
python dirty_read.py
python non_repeatable_read.py
python phantom_read.py
python write_skew.py
python deadlock.py
python isolation_levels.py
```

### 10.4 dirty_read.py 详解

**用途**：演示脏读的概念，并验证 SQLite/PG 在默认级别下不会脏读。

**用法**：
```bash
python dirty_read.py
```

**预期输出**：
```
=== 脏读演示 ===

注意：SQLite 和 PostgreSQL 都不支持真脏读（RU 降级为 RC）
本脚本模拟"如果允许脏读会发生什么"

[模拟脏读]
T1: BEGIN, UPDATE balance=200 (未提交)
T2: READ balance = 200  ← 如果允许脏读，会读到 200
T1: ROLLBACK
T2: READ balance = 100  ← 实际值恢复，但 T2 已用过错误的 200

[SQLite 实际行为（Read Committed）]
T1: BEGIN, UPDATE balance=200 (未提交)
T2: READ balance = 100  ← 读不到未提交的 200，防脏读成功
T1: ROLLBACK
T2: READ balance = 100  ← 始终一致
结果：未发生脏读
```

**关键代码逻辑**：
```python
# 1. 建表插数据
# 2. T1 BEGIN，UPDATE 不提交
# 3. T2 SELECT（SQLite 下读不到未提交值）
# 4. T1 ROLLBACK
# 5. 对比"如果脏读会怎样" vs "实际防脏读"
```

### 10.5 non_repeatable_read.py 详解

**用途**：在 Read Committed 下复现不可重复读，在 Repeatable Read 下防止。

**用法**：
```bash
python non_repeatable_read.py
```

**预期输出**：
```
=== 不可重复读演示 ===

[Read Committed] 复现不可重复读:
  T2 第一次读: balance = 100
  T1 修改并提交: balance = 200
  T2 第二次读: balance = 200  ← 变了！不可重复读
  结果: 发生不可重复读

[Repeatable Read] 防止不可重复读:
  T2 第一次读: balance = 100
  T1 修改并提交: balance = 200
  T2 第二次读: balance = 100  ← 不变，快照隔离
  结果: 未发生不可重复读
```

**关键代码逻辑**：
```python
# 1. 建表插数据
# 2. RC 模式：T2 BEGIN → 读 → T1 改并提交 → T2 再读（变了）
# 3. RR 模式：T2 BEGIN → 读 → T1 改并提交 → T2 再读（不变）
# 4. 对比两种隔离级别的行为
```

### 10.6 phantom_read.py 详解

**用途**：在 Read Committed 下复现幻读，在 Repeatable Read（SI）下防止。

**用法**：
```bash
python phantom_read.py
```

**预期输出**：
```
=== 幻读演示 ===

[Read Committed] 复现幻读:
  T2 第一次范围查: count = 100
  T1 插入新行并提交
  T2 第二次范围查: count = 101  ← 多了！幻读
  结果: 发生幻读

[Repeatable Read] 防止幻读:
  T2 第一次范围查: count = 100
  T1 插入新行并提交
  T2 第二次范围查: count = 100  ← 不变，快照隔离
  结果: 未发生幻读
```

**关键代码逻辑**：
```python
# 1. 建表插 100 行
# 2. RC 模式：T2 范围查 → T1 插入并提交 → T2 再范围查（多了）
# 3. RR 模式：T2 范围查 → T1 插入并提交 → T2 再范围查（不变）
# 4. 对比
```

### 10.7 write_skew.py 详解

**用途**：在 Repeatable Read 下复现写偏斜，用悲观锁防止。

**用法**：
```bash
python write_skew.py
```

**预期输出**：
```
=== 写偏斜演示 ===

场景：医院排班，至少留1人值班
初始：Alice=on, Bob=on

[Repeatable Read] 复现写偏斜:
  T1(Alice): 查 count=2, Alice 下班, COMMIT
  T2(Bob):   查 count=2, Bob 下班, COMMIT
  最终 count = 0  ← 违约！无人值班
  结果: 发生写偏斜

[悲观锁 SELECT FOR UPDATE] 防止写偏斜:
  T1(Alice): 锁住所有值班行, 查 count=2, Alice 下班, COMMIT
  T2(Bob):   等待锁... T1 提交后拿到锁, 查 count=1, 不下班
  最终 count = 1  ← 合法
  结果: 未发生写偏斜
```

**关键代码逻辑**：
```python
# 1. 建表 doctors(name, on_call)，插 Alice/Bob
# 2. RR 模式：两事务并发下班 → 都成功 → 违约
# 3. 悲观锁模式：SELECT FOR UPDATE 锁住读到的行 → 串行化 → 合法
# 4. 对比
```

### 10.8 deadlock.py 详解

**用途**：复现死锁，演示固定加锁顺序避免死锁。

**用法**：
```bash
python deadlock.py
```

**预期输出**：
```
=== 死锁演示 ===

[不同加锁顺序] 复现死锁:
  T1: UPDATE id=1 (锁住1), UPDATE id=2 (等待)
  T2: UPDATE id=2 (锁住2), UPDATE id=1 (等待)
  → 死锁！数据库检测到，abort 一个事务
  错误: database is locked / deadlock detected

[固定加锁顺序] 避免死锁:
  T1: UPDATE id=1, UPDATE id=2 (都按 1→2 顺序)
  T2: UPDATE id=1, UPDATE id=2 (都按 1→2 顺序)
  → T1 先拿到 id=1，T2 等 → T1 完成，T2 执行 → 无死锁
  结果: 无死锁
```

**关键代码逻辑**：
```python
# 1. 建表 accounts，插 id=1, id=2
# 2. 不同顺序：T1 先1后2，T2 先2后1 → 死锁
# 3. 固定顺序：T1 和 T2 都先1后2 → 无死锁
# 4. 对比
```

### 10.9 isolation_levels.py 详解

**用途**：对比四级隔离级别在相同场景下的不同行为。

**用法**：
```bash
python isolation_levels.py
```

**预期输出**：
```
=== 四级隔离级别对比 ===

场景：T2 读两次，中间 T1 修改并提交

[Read Uncommitted] (SQLite 降级为 RC)
  第一次读: 100, 第二次读: 200 → 不可重复读

[Read Committed]
  第一次读: 100, 第二次读: 200 → 不可重复读

[Repeatable Read]
  第一次读: 100, 第二次读: 100 → 一致（快照隔离）

[Serializable] (SQLite 实为 SI)
  第一次读: 100, 第二次读: 100 → 一致

总结：
  RC 以下：可能脏读、不可重复读、幻读
  RR/SI：防脏读、不可重复读、幻读，但不防写偏斜
  Serializable：防所有异常
```

**关键代码逻辑**：
```python
# 1. 建表插数据
# 2. 对每种隔离级别：T2 读 → T1 改并提交 → T2 再读
# 3. 记录每种级别的行为
# 4. 汇总对比
```

### 10.10 典型输出汇总

```
=== 不可重复读演示 ===

[Read Committed] 复现不可重复读:
  T2 第一次读: balance = 100
  T1 修改并提交: balance = 200
  T2 第二次读: balance = 200  ← 变了！不可重复读
  结果: 发生不可重复读

[Repeatable Read] 防止不可重复读:
  T2 第一次读: balance = 100
  T1 修改并提交: balance = 200
  T2 第二次读: balance = 100  ← 不变，快照隔离
  结果: 未发生不可重复读
```

---

## 11. 与 miniDB 的 2PL + MVCC 对照

### 11.1 miniDB 的并发控制（阶段1 章6）

阶段1 的 miniDB 实现了两种并发控制：

#### 11.1.1 两阶段锁（2PL）

```
2PL 协议：
  增长阶段：事务只能加锁，不能解锁
  收缩阶段：事务只能解锁，不能加锁
  ← 严格 2PL：X 锁持有到事务结束

锁相容矩阵：
         S(共享)   X(排他)
  S        ✓        ✗
  X        ✗        ✗
```

| 特性 | miniDB 2PL | PostgreSQL |
|---|---|---|
| 锁粒度 | 行级 | 行级 + 表级 + 谓词 |
| 死锁检测 | 等待图 + 超时 | 等待图周期检测 |
| 隔离级别 | 严格 2PL = Serializable | 可配置四级 |
| 性能 | 低（锁冲突多） | SSI 更高效 |

#### 11.1.2 MVCC（多版本并发控制）

```
miniDB MVCC：
  每行带 xmin（创建事务ID）、xmax（删除事务ID）
  读事务看快照，不加锁
  写事务创建新版本

  优点：读不阻塞写，写不阻塞读
  缺点：需要垃圾回收（VACUUM）
```

### 11.2 对照表

| 维度 | miniDB | SQLite | PostgreSQL |
|---|---|---|---|
| **默认隔离** | Serializable（2PL） | Serializable（journal） / SI（WAL） | Read Committed |
| **MVCC** | 简单版本链 | WAL 快照 | xmin/xmax + 快照 |
| **死锁处理** | 超时 + abort | 不会死锁 | 等待图检测 |
| **可串行化** | 2PL 严格 | 全库锁 | SSI |
| **垃圾回收** | 简单回收 | auto-checkpoint | autovacuum |
| **锁粒度** | 行 | 库/行 | 行/表/谓词 |
| **写偏斜防护** | 2PL 防住 | 全库锁防住 | SSI 防住 |
| **读不阻塞写** | MVCC 模式下 | WAL 模式下 | 始终 |
| **教学清晰度** | 高 | 中 | 低（实现复杂） |

### 11.3 为什么 miniDB 用 2PL 而不是 SSI

1. **教学目标**：2PL 概念清晰，SSI 实现复杂
2. **规模**：miniDB 是单机教学项目，2PL 足够
3. **对照**：2PL 是理解锁的基础，SSI 是优化
4. **代码量**：2PL 几百行，SSI 需要数千行
5. **调试**：2PL 行为可预测，SSI 的 rw 依赖检测难调试

### 11.4 实现差异图

```
miniDB 并发控制选择：
  ┌─────────────┐
  │  事务管理器  │
  └──────┬──────┘
         ├─→ 2PL（严格两阶段锁）
         │     ├─→ lock_manager: S/X 锁
         │     ├─→ deadlock: 等待图检测
         │     └─→ 隔离: Serializable
         │
         └─→ MVCC（多版本）
               ├─→ 每行 xmin/xmax
               ├─→ 快照读
               └─→ 垃圾回收

PostgreSQL 并发控制：
  ┌─────────────┐
  │  事务管理器  │
  └──────┬──────┘
         ├─→ MVCC（基础）
         │     ├─→ xmin/xmax + hint bits
         │     ├─→ 快照（Snapshot）
         │     └─→ SI（Snapshot Isolation）
         │
         └─→ SSI（Serializable 时）
               ├─→ SIREAD 锁
               ├─→ rw 依赖检测
               └─→ 危险结构 abort
```

### 11.5 从 miniDB 到 PostgreSQL 的演进

```
教学路线：
  miniDB 2PL（章6）        ← 基础：理解锁、死锁、可串行化
    ↓
  miniDB MVCC（章6）       ← 进阶：理解多版本、快照、读不阻塞写
    ↓
  SQLite WAL（章2）        ← 实战：SI 的简单实现
    ↓
  PostgreSQL MVCC+SI（章4）← 生产：SI 的工业实现
    ↓
  PostgreSQL SSI（章4）    ← 顶峰：真可串行化的工业实现
```

---

## 12. 习题

### 12.1 基础题

**1. ACID 辨析**：以下场景分别违反 ACID 的哪一条？
- (a) 转账扣款成功但加款失败，钱消失了
- (b) 两个并发转账后余额为负（无约束检查）
- (c) 提交后断电，重启发现数据没了
- (d) 查询过程中看到别人改了一半的数据

**参考答案**：
- (a) 违反 A（原子性）：扣款和加款应同时成功或同时失败
- (b) 违反 C（一致性）：余额为负违反 `balance >= 0` 约束
- (c) 违反 D（持久性）：提交后不应丢失
- (d) 违反 I（隔离性）：看到了别人的中间状态（脏读）

**2. 隔离级别匹配**：给以下场景选择最合适的隔离级别，说明理由：
- (a) 银行账户余额查询（只读报表）
- (b) 在线购物库存扣减
- (c) 医院排班系统（至少留1人值班）
- (d) 统计网站日活用户数（允许近似）

**参考答案**：
- (a) Repeatable Read：报表需要一致快照，不需要写
- (b) Read Committed + 乐观锁：短事务，冲突率可控
- (c) Serializable：写偏斜敏感，必须防住
- (d) Read Committed：近似统计，容忍不可重复读

**3. 异常辨析**：判断以下描述是脏读、不可重复读、幻读还是写偏斜：
- (a) T1 读到 T2 未提交的数据
- (b) T1 两次查同一行，值不同
- (c) T1 两次范围查，行数不同
- (d) T1 和 T2 各自下班，结果无人值班

**参考答案**：
- (a) 脏读
- (b) 不可重复读
- (c) 幻读
- (d) 写偏斜

### 12.2 实践题

**4. 复现验证**：运行 `write_skew.py`，观察 Repeatable Read 下写偏斜发生。然后修改脚本，用 `SELECT ... FOR UPDATE` 防止写偏斜，验证约束不被破坏。

**操作步骤**：
1. `cd phase2/04-transaction/concurrency_demos`
2. `python write_skew.py` → 观察写偏斜发生（count=0）
3. 修改脚本：在 SELECT 后加 `FOR UPDATE`
4. 重新运行 → 观察 count=1，写偏斜被防止

**5. 死锁排查**：运行 `deadlock.py`，观察死锁错误。然后：
- (a) 修改加锁顺序，使两个事务按同一顺序加锁，验证死锁消失
- (b) 用 `PRAGMA busy_timeout` 设置超时，观察行为变化

**操作步骤**：
1. `python deadlock.py` → 观察死锁错误
2. 修改 T2 的加锁顺序与 T1 一致（都先 id=1 后 id=2）
3. 重新运行 → 无死锁
4. 加 `PRAGMA busy_timeout=5000` → 观察等待行为

**6. 乐观锁实现**：参考"乐观锁实战"代码，实现一个并发库存扣减场景：
- (a) 10 个线程同时扣减同一商品库存
- (b) 用乐观锁（版本号）实现，统计成功/重试/失败次数
- (c) 对比悲观锁（`SELECT FOR UPDATE`）的吞吐量

**预期结果**：
```
乐观锁：成功 10, 重试 N, 失败 0, 耗时 X ms
悲观锁：成功 10, 重试 0, 失败 0, 耗时 Y ms
通常 X < Y（乐观锁并发度高）
```

### 12.3 思考题

**7. SI vs SSI**：为什么 Snapshot Isolation 能防止幻读却不能防止写偏斜？从"检测的冲突类型"角度解释。

**参考答案**：
- SI 通过快照防止幻读：新插入行的 xmin > 快照时间戳，不可见
- SI 只检测 write-write 冲突，写偏斜中两事务写不同行，无 ww 冲突，SI 放行
- SSI 额外检测 read-write 依赖，发现 rw 依赖环时 abort，从而防住写偏斜

**8. 隔离级别选择困境**：某系统有如下需求——高并发（QPS 10k）、读多写少、偶尔写偏斜可接受。应该选什么隔离级别？如果写偏斜不可接受呢？

**参考答案**：
- 写偏斜可接受：Read Committed（最高并发）+ 应用层校验
- 写偏斜不可接受：Serializable + 重试机制（牺牲一些并发换正确性）
- 折中：Repeatable Read + 关键路径用 SELECT FOR UPDATE

**9. miniDB 改进**：阶段1 的 miniDB 用 2PL 实现可串行化。如果要改成 MVCC + SI：
- (a) 需要给 tuple 结构加什么字段？
- (b) 可见性判断逻辑怎么写？
- (c) 如何处理写偏斜（是否要实现 SSI）？

**参考答案**：
- (a) 加 xmin（创建事务ID）、xmax（删除事务ID）、cmin/cmax（命令ID）
- (b) 可见性：xmin 已提交且 < 快照，且（xmax 未设置或未提交或 > 快照）
- (c) 教学项目可不实现 SSI，文档说明"SI 不防写偏斜，需 Serializable 或显式锁"

**10. 分布式事务**：本章讨论单机事务。如果两个事务分别操作不同数据库（跨库转账），ACID 还能保证吗？需要引入什么机制？（提示：两阶段提交 2PC、Saga）

**参考答案**：
- 单机 ACID 无法直接保证跨库
- 2PC（两阶段提交）：协调者问所有参与者"能提交吗"，都同意后统一提交，但阻塞、性能差
- Saga：把长事务拆成一系列小事务，每个有补偿操作，失败时按反序补偿，最终一致
- TCC（Try-Confirm-Cancel）：业务层两阶段，Try 预留，Confirm 确认，Cancel 取消

**11. 丢失更新**：以下场景是否会发生丢失更新？在什么隔离级别下？如何防止？
```
T1: READ x=100, 计算x+1=101, WRITE x=101
T2: READ x=100, 计算x+1=101, WRITE x=101
结果：x=101，但应该是 102（两次 +1）
```

**参考答案**：
- 在 RC 下会发生（读-改-写非原子）
- 在 RR/SI 下：first-commit-wins，后提交者 abort，需重试
- 防止：用 `UPDATE x = x + 1`（原子操作）、乐观锁、悲观锁、或 Serializable

**12. 隔离级别与性能**：设计一个实验，测量同一工作负载在不同隔离级别下的吞吐量，并解释结果。

**参考答案**：
- 实验：N 个线程并发执行读-改-写事务，统计 TPS
- 预期：RC > RR > Serializable（并发度递减）
- 解释：隔离越强，冲突检测/等待越多，吞吐越低
- 代码：参考 `isolation_levels.py`，加计时和统计

---

## 小结

| 要点 | 内容 |
|---|---|
| **ACID** | A(WAL+undo) C(约束) I(隔离) D(fsync) |
| **A 原子性** | 全做或全不做，WAL + Undo 实现 |
| **C 一致性** | 数据始终合法，依赖约束检查 |
| **I 隔离性** | 并发互不干扰，分级隔离权衡 |
| **D 持久性** | 提交不丢，WAL + fsync 实现 |
| **四级隔离** | RU < RC < RR < Serializable，能力递增、并发递减 |
| **四种异常** | 脏读 < 不可重复读 < 幻读 < 写偏斜，严重递增 |
| **脏读** | 读未提交，RC 即可防 |
| **不可重复读** | 同行两次读不同，RR 可防 |
| **幻读** | 范围查行数变化，SI 可防 |
| **写偏斜** | 各自合法合起来违约，仅 Serializable 可防 |
| **PG 的 RR** | 实为 Snapshot Isolation，防幻读但不防写偏斜 |
| **SSI** | PG Serializable 用 SSI 检测 rw 依赖环，真可串行化 |
| **死锁四条件** | 互斥+持有等待+不可抢占+循环等待 |
| **死锁避免** | 固定加锁顺序破坏循环等待 |
| **乐观/悲观** | 冲突少用乐观（版本号/CAS），冲突多用悲观（FOR UPDATE） |
| **miniDB** | 2PL 教学清晰，MVCC 对照真实数据库 |
| **脚本复现** | 6 个 Python 脚本覆盖所有异常和隔离级别 |

> 下一章 [章5：连接池与后端集成](05-connection-pool.md) 将讨论事务在连接池和 ORM 中的边界问题。
