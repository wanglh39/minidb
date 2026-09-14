# 章4：事务与隔离级别实战

> 事务是数据库区别于文件系统的核心特征。本章从 ACID 出发，逐一复现脏读、不可重复读、幻读、写偏斜四种并发异常，对比四级隔离级别的能力边界，并用 Python + SQLite/PostgreSQL 实地验证。最后回到阶段1的 miniDB，对照 2PL + MVCC 的实现选择。

## ACID 四性质回顾

| 性质 | 全称 | 含义 | miniDB 实现 | SQLite 实现 | PostgreSQL 实现 |
|---|---|---|---|---|---|
| **A** | Atomicity 原子性 | 事务内所有操作要么全做要么全不做 | WAL + undo | Rollback Journal / WAL | WAL + undo（abort 时） |
| **C** | Consistency 一致性 | 事务前后满足完整性约束 | 约束检查 | 约束 + 触发器 | 约束 + 触发器 + CHECK |
| **I** | Isolation 隔离性 | 并发事务互不干扰 | 2PL / MVCC | WAL + busy handler | MVCC + Snapshot |
| **D** | Durability 持久性 | 提交后即使崩溃也不丢 | WAL fsync | WAL fsync | WAL fsync + sync rep |

> **关键认识**：C 是应用层约束，A/I/D 是数据库机制。A 和 I 是数据库最核心的两个保证，D 依赖存储 fsync，C 依赖正确建模。

### 原子性实现：WAL + Undo

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

### 隔离性实现：从串行化到弱隔离

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

---

## 四级隔离级别

### SQL 标准定义的四级

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

### 各级别语义

#### Read Uncommitted（读未提交）

允许读到其他事务**未提交**的数据。几乎没有数据库真正实现这个级别（PG 会降级为 RC），因为它破坏太大。

#### Read Committed（读已提交）

- 每条语句执行时获取**新的快照**
- 只能看到已提交的数据
- 同一事务内两次相同查询可能得到不同结果（不可重复读）

```sql
-- PostgreSQL Read Committed 行为
BEGIN ISOLATION LEVEL READ COMMITTED;
  SELECT balance FROM accounts WHERE id = 1;  -- 100
  -- [另一事务把 balance 改成 200 并提交]
  SELECT balance FROM accounts WHERE id = 1;  -- 200  ← 变了！
COMMIT;
```

#### Repeatable Read（可重复读）

- 事务开始时获取**一次快照**，整个事务用同一快照
- 同一事务内两次相同查询结果一致
- SQL 标准允许幻读，但 PG/SI 不允许

```sql
-- PostgreSQL Repeatable Read（实为 Snapshot Isolation）
BEGIN ISOLATION LEVEL REPEATABLE READ;
  SELECT balance FROM accounts WHERE id = 1;  -- 100
  -- [另一事务把 balance 改成 200 并提交]
  SELECT balance FROM accounts WHERE id = 1;  -- 100  ← 不变
COMMIT;
```

#### Serializable（可串行化）

- 保证并发执行结果等价于某个串行执行
- PostgreSQL 用 SSI 算法，检测到冲突时抛 `could not serialize access due to read/write dependencies` 错误
- 代价：可能 abort 重试

```sql
-- PostgreSQL Serializable
BEGIN ISOLATION LEVEL SERIALIZABLE;
  -- ... 读写操作 ...
COMMIT;  -- 可能抛 serialization_failure，需重试
```

---

## 并发异常详解与复现

### 1. 脏读（Dirty Read）

**定义**：事务 T2 读到了事务 T1 **未提交**的数据，若 T1 随后回滚，T2 就读到了根本不存在的值。

**时序图**：

```
时间 →
T1:  BEGIN     UPDATE x=2          ROLLBACK
                    ↓
T2:           BEGIN  READ x(=2!)   COMMIT
                       ↑ 脏读！读到了未提交的 2
```

**危害**：基于脏数据做决策，导致逻辑错误。例如转账中读到中间态余额。

**复现条件**：需要 Read Uncommitted 隔离级别。SQLite 和 PostgreSQL 都不支持脏读（PG 的 RU 降级为 RC），因此**无法在真实数据库复现**，只能用模拟说明。

**防止**：使用 Read Committed 或更高级别。

```python
# 见 concurrency_demos/dirty_read.py
# SQLite 无法真正脏读，脚本演示"如果允许脏读会发生什么"
# 并对比 Read Committed 下确实不会脏读
```

### 2. 不可重复读（Non-Repeatable Read）

**定义**：事务 T2 内，同一查询执行两次，结果不同，因为 T1 在中间**修改并提交**了已有行。

**时序图**：

```
时间 →
T1:  BEGIN          UPDATE x=2; COMMIT
                        ↓
T2:  BEGIN  READ x(=1)        READ x(=2!)  COMMIT
                              ↑ 不可重复读！同一事务内 x 变了
```

**与脏读区别**：脏读读未提交，不可重复读读已提交。区别在于"提交与否"。

**复现条件**：Read Committed 级别。

**防止**：Repeatable Read 或更高级别。

```python
# 见 concurrency_demos/non_repeatable_read.py
# 在 Read Committed 下复现，在 Repeatable Read 下防止
```

### 3. 幻读（Phantom Read）

**定义**：事务 T2 内，同一**范围查询**执行两次，结果集行数不同，因为 T1 在中间**插入并提交**了新行（满足范围条件）。

**时序图**：

```
时间 →
T1:  BEGIN          INSERT (x=5, 满足范围); COMMIT
                        ↓
T2:  BEGIN  COUNT(WHERE x>0)=100   COUNT(WHERE x>0)=101!  COMMIT
                                   ↑ 幻读！多了一行"幻影"
```

**与不可重复读区别**：
- 不可重复读：已有行的值被修改
- 幻读：新行被插入（或旧行被删除），导致结果集成员变化

**SQL 标准下的复现**：Repeatable Read 级别（标准允许）。

**PostgreSQL 实际行为**：PG 的 RR 是 Snapshot Isolation，**不会**幻读。要在 PG 复现幻读需用 Read Committed。

**防止**：
- SQL 标准：Serializable
- PostgreSQL：Repeatable Read（因 SI）即可
- 也可用谓词锁（predicate lock）或 `SELECT ... FOR UPDATE` 加范围锁

```python
# 见 concurrency_demos/phantom_read.py
# 在 Read Committed 下复现幻读
# 在 Repeatable Read（SQLite/PG SI）下防止
```

### 4. 写偏斜（Write Skew）

**定义**：两个事务各自读取重叠的数据集，各自修改**不相交**的子集，单独看都合法，合起来破坏全局约束。Snapshot Isolation 无法防止。

**经典案例**：医院排班，至少留1人值班。Alice 和 Bob 都在值班，各自查询"当前值班人数 ≥ 2"，然后各自把自己改成下班。单独看合法（查询时确实 ≥2），合起来无人值班。

**时序图**：

```
时间 →
T1(Alice):  BEGIN  READ count(on_call)=2  UPDATE Alice=off  COMMIT
T2(Bob):    BEGIN  READ count(on_call)=2  UPDATE Bob=off    COMMIT

约束：count(on_call) >= 1
初始：Alice=on, Bob=on  → count=2
结果：Alice=off, Bob=off → count=0  ← 违约！
```

**为什么 Snapshot Isolation 防不住**：
- SI 检查的是 **write-write 冲突**（两个事务写同一行）
- 写偏斜中 T1 写 Alice、T2 写 Bob，**没有写写冲突**，SI 放行
- 但存在 **rw 依赖**：T1 读 Bob 的状态决定写 Alice，T2 读 Alice 的状态决定写 Bob，形成危险结构

**复现条件**：Repeatable Read / Snapshot Isolation。

**防止**：
- Serializable（PG SSI 会检测并 abort）
- 悲观锁：`SELECT ... FOR UPDATE` 锁住读到的行
- 乐观锁：约束检查 + 重试

```python
# 见 concurrency_demos/write_skew.py
# 在 Repeatable Read 下复现写偏斜
# 在 Serializable 下复现被阻止（SQLite 不支持真 SSI，需 PG）
# 用 SELECT FOR UPDATE 演示悲观锁防止
```

### 异常关系总结

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

## PostgreSQL 的 Snapshot Isolation

### 快照隔离原理

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

### Snapshot Isolation 能防什么

| 异常 | SI 能否防止 | 原因 |
|---|---|---|
| 脏读 | ✓ | 只看已提交 |
| 不可重复读 | ✓ | 整个事务用同一快照 |
| 幻读 | ✓ | 新插入行 xmin > 快照，不可见 |
| 写偏斜 | ✗ | 无写写冲突，SI 检测不到 |
| 丢失更新 | ✓ | First-commit-wins，后提交者 abort |

### SSI（Serializable Snapshot Isolation）

PostgreSQL 9.1+ 用 SSI 实现真可串行化。核心是检测 **rw 依赖环**。

```
写偏斜的危险结构：
  T1 --rw--> T2 --rw--> T1  （形成环）

SSI 检测：
  每个 SIREAD 锁记录"谁读了什么"
  写操作检查是否与已有 SIREAD 形成冲突
  检测到环就 abort 其中一个事务
```

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

### SQLite 的隔离级别

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

---

## 死锁产生与排查

### 死锁定义

两个或多个事务互相等待对方持有的锁，形成循环依赖，永远无法推进。

**时序图**：

```
时间 →
T1:  LOCK(A)          LOCK(B) [等待T2]
                          ↓
T2:          LOCK(B)          LOCK(A) [等待T1]
                              ↓
                    死锁！T1等T2的B，T2等T1的A
```

### 死锁的必要条件（ Coffman 四条件）

1. **互斥**：资源同一时刻只能被一个事务持有
2. **持有并等待**：持有部分锁的同时请求新锁
3. **不可抢占**：不能强行夺走别人的锁
4. **循环等待**：存在事务的环形等待链

### 数据库的死锁处理

| 数据库 | 检测方式 | 解决方式 |
|---|---|---|
| **PostgreSQL** | 等待图周期检测（默认 1s） | abort 代价最小的事务，抛 `deadlock_detected` |
| **SQLite** | 不会死锁（全库写锁） | — |
| **miniDB** | 超时 + 等待图 | abort 或等待超时 |

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
```

### 死锁避免策略

1. **固定加锁顺序**：所有事务按同一顺序访问资源
2. **一次性加锁**：事务开始时锁定所有需要的资源
3. **短事务**：减少持锁时间
4. **乐观并发控制**：读时不加锁，提交时检测冲突

```python
# 见 concurrency_demos/deadlock.py
# 复现 T1 先锁A后锁B、T2 先锁B后锁A 的死锁
# 演示固定加锁顺序避免死锁
```

---

## 乐观锁 vs 悲观锁

### 悲观锁（Pessimistic Locking）

**假设**：冲突很可能发生，先锁住再操作。

```sql
-- SELECT ... FOR UPDATE 悲观锁
BEGIN;
SELECT balance FROM accounts WHERE id = 1 FOR UPDATE;  -- 加行锁
-- 其他事务修改 id=1 会阻塞
UPDATE accounts SET balance = balance - 100 WHERE id = 1;
COMMIT;  -- 释放锁
```

| 优点 | 缺点 |
|---|---|
| 确保成功 | 降低并发 |
| 逻辑简单 | 可能死锁 |
| 适合写多 | 持锁时间长 |

### 乐观锁（Optimistic Locking）

**假设**：冲突很少，先操作，提交时检测冲突。

```sql
-- 方式1：版本号
BEGIN;
SELECT balance, version FROM accounts WHERE id = 1;  -- balance=100, version=5
-- 应用层计算
UPDATE accounts SET balance = 50, version = version + 1
  WHERE id = 1 AND version = 5;  -- 0 行受影响说明被别人改了
COMMIT;

-- 方式2：CAS（Compare And Swap）
UPDATE accounts SET balance = 50
  WHERE id = 1 AND balance = 100;  -- 0 行受影响说明 balance 已变
```

| 优点 | 缺点 |
|---|---|
| 高并发 | 冲突需重试 |
| 无死锁 | 适合读多 |
| 持锁时间短 | 需要版本字段 |

### 选择依据

```
冲突率低 + 读多写少  →  乐观锁
冲突率高 + 写多       →  悲观锁
长事务 + 复杂校验     →  悲观锁
短事务 + 简单更新     →  乐观锁
```

### 乐观锁实战：库存扣减

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

---

## 与 miniDB 的 2PL + MVCC 对照

### miniDB 的并发控制（阶段1 章6）

阶段1 的 miniDB 实现了两种并发控制：

#### 1. 两阶段锁（2PL）

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

#### 2. MVCC（多版本并发控制）

```
miniDB MVCC：
  每行带 xmin（创建事务ID）、xmax（删除事务ID）
  读事务看快照，不加锁
  写事务创建新版本

  优点：读不阻塞写，写不阻塞读
  缺点：需要垃圾回收（VACUUM）
```

### 对照表

| 维度 | miniDB | SQLite | PostgreSQL |
|---|---|---|---|
| **默认隔离** | Serializable（2PL） | Serializable（journal） / SI（WAL） | Read Committed |
| **MVCC** | 简单版本链 | WAL 快照 | xmin/xmax + 快照 |
| **死锁处理** | 超时 + abort | 不会死锁 | 等待图检测 |
| **可串行化** | 2PL 严格 | 全库锁 | SSI |
| **垃圾回收** | 简单回收 | auto-checkpoint | autovacuum |
| **锁粒度** | 行 | 库/行 | 行/表/谓词 |

### 为什么 miniDB 用 2PL 而不是 SSI

1. **教学目标**：2PL 概念清晰，SSI 实现复杂
2. **规模**：miniDB 是单机教学项目，2PL 足够
3. **对照**：2PL 是理解锁的基础，SSI 是优化

### 实现差异图

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

---

## 实战：用 Python 复现所有异常

### 环境准备

```bash
# SQLite（Python 内置，无需安装）
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"

# PostgreSQL（可选，用于 SSI 演示）
docker run --name pg -e POSTGRES_PASSWORD=secret -p 5432:5432 -d postgres:16
pip install psycopg2-binary
```

### 脚本一览

| 脚本 | 演示内容 | 复现的异常 |
|---|---|---|
| `dirty_read.py` | 脏读模拟 + RC 防止 | 脏读 |
| `non_repeatable_read.py` | RC 复现 / RR 防止 | 不可重复读 |
| `phantom_read.py` | RC 复现 / RR(SI) 防止 | 幻读 |
| `write_skew.py` | RR 复现 / 悲观锁防止 | 写偏斜 |
| `deadlock.py` | 死锁复现 + 顺序加锁避免 | 死锁 |
| `isolation_levels.py` | 四级隔离对比 | 综合 |

### 运行

```bash
cd phase2/04-transaction/concurrency_demos
python dirty_read.py
python non_repeatable_read.py
python phantom_read.py
python write_skew.py
python deadlock.py
python isolation_levels.py
```

### 典型输出（non_repeatable_read.py）

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

## 习题

### 基础题

1. **ACID 辨析**：以下场景分别违反 ACID 的哪一条？
   - (a) 转账扣款成功但加款失败，钱消失了
   - (b) 两个并发转账后余额为负（无约束检查）
   - (c) 提交后断电，重启发现数据没了
   - (d) 查询过程中看到别人改了一半的数据

2. **隔离级别匹配**：给以下场景选择最合适的隔离级别，说明理由：
   - (a) 银行账户余额查询（只读报表）
   - (b) 在线购物库存扣减
   - (c) 医院排班系统（至少留1人值班）
   - (d) 统计网站日活用户数（允许近似）

3. **异常辨析**：判断以下描述是脏读、不可重复读、幻读还是写偏斜：
   - (a) T1 读到 T2 未提交的数据
   - (b) T1 两次查同一行，值不同
   - (c) T1 两次范围查，行数不同
   - (d) T1 和 T2 各自下班，结果无人值班

### 实践题

4. **复现验证**：运行 `write_skew.py`，观察 Repeatable Read 下写偏斜发生。然后修改脚本，用 `SELECT ... FOR UPDATE` 防止写偏斜，验证约束不被破坏。

5. **死锁排查**：运行 `deadlock.py`，观察死锁错误。然后：
   - (a) 修改加锁顺序，使两个事务按同一顺序加锁，验证死锁消失
   - (b) 用 `PRAGMA busy_timeout` 设置超时，观察行为变化

6. **乐观锁实现**：参考"乐观锁实战"代码，实现一个并发库存扣减场景：
   - (a) 10 个线程同时扣减同一商品库存
   - (b) 用乐观锁（版本号）实现，统计成功/重试/失败次数
   - (c) 对比悲观锁（`SELECT FOR UPDATE`）的吞吐量

### 思考题

7. **SI vs SSI**：为什么 Snapshot Isolation 能防止幻读却不能防止写偏斜？从"检测的冲突类型"角度解释。

8. **隔离级别选择困境**：某系统有如下需求——高并发（QPS 10k）、读多写少、偶尔写偏斜可接受。应该选什么隔离级别？如果写偏斜不可接受呢？

9. **miniDB 改进**：阶段1 的 miniDB 用 2PL 实现可串行化。如果要改成 MVCC + SI：
   - (a) 需要给 tuple 结构加什么字段？
   - (b) 可见性判断逻辑怎么写？
   - (c) 如何处理写偏斜（是否要实现 SSI）？

10. **分布式事务**：本章讨论单机事务。如果两个事务分别操作不同数据库（跨库转账），ACID 还能保证吗？需要引入什么机制？（提示：两阶段提交 2PC、Saga）

---

## 小结

| 要点 | 内容 |
|---|---|
| **ACID** | A(WAL+undo) C(约束) I(隔离) D(fsync) |
| **四级隔离** | RU < RC < RR < Serializable，能力递增、并发递减 |
| **四种异常** | 脏读 < 不可重复读 < 幻读 < 写偏斜，严重递增 |
| **PG 的 RR** | 实为 Snapshot Isolation，防幻读但不防写偏斜 |
| **SSI** | PG Serializable 用 SSI 检测 rw 依赖环，真可串行化 |
| **死锁** | 互斥+持有等待+不可抢占+循环等待；固定加锁顺序可避免 |
| **乐观/悲观** | 冲突少用乐观（版本号/CAS），冲突多用悲观（FOR UPDATE） |
| **miniDB** | 2PL 教学清晰，MVCC 对照真实数据库 |

> 下一章 [章5：连接池与后端集成](05-connection-pool.md) 将讨论事务在连接池和 ORM 中的边界问题。