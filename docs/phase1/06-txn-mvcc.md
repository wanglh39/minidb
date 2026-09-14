# 章6：事务与并发控制

> 多个事务同时读写同一数据时，如何保证 ACID？本章实现 2PL（两阶段锁）和 MVCC（多版本并发控制）两种方案，并详细对比它们的优缺点。
>
> **本章目标读者**：第一次接触事务和并发控制的初学者。我们会从最简单的"转账"例子讲起，逐步引出 ACID、并发问题、锁、多版本等概念，最后逐行解读 minidb 的实现代码。
>
> **本章阅读路线**：
> 1. 先理解"为什么需要事务"——没有事务会出什么问题
> 2. 再看"并发问题"——多个事务同时跑会出什么乱子
> 3. 然后学习两种解决方案：2PL 和 MVCC
> 4. 最后看代码实现，把理论和代码对应起来

---

## 目录

- [1. 为什么需要事务](#1-为什么需要事务)
  - [1.1 转账问题：没有事务会怎样](#11-转账问题没有事务会怎样)
  - [1.2 ACID 四大性质详解](#12-acid-四大性质详解)
  - [1.3 现实类比](#13-现实类比)
- [2. 并发问题](#2-并发问题)
  - [2.1 脏读](#21-脏读)
  - [2.2 不可重复读](#22-不可重复读)
  - [2.3 幻读](#23-幻读)
  - [2.4 丢失更新](#24-丢失更新)
  - [2.5 写偏斜](#25-写偏斜)
  - [2.6 并发问题复现场景汇总表](#26-并发问题复现场景汇总表)
- [3. 2PL 详解](#3-2pl-详解)
  - [3.1 共享锁与排他锁](#31-共享锁与排他锁)
  - [3.2 两阶段的含义](#32-两阶段的含义)
  - [3.3 死锁的产生](#33-死锁的产生)
  - [3.4 死锁的检测](#34-死锁的检测)
- [4. MVCC 详解](#4-mvcc-详解)
  - [4.1 多版本概念](#41-多版本概念)
  - [4.2 xmin 与 xmax 的含义](#42-xmin-与-xmax-的含义)
  - [4.3 快照隔离原理](#43-快照隔离原理)
  - [4.4 读不阻塞写，写不阻塞读](#44-读不阻塞写写不阻塞读)
- [5. MVCC vs 2PL](#5-mvcc-vs-2pl)
- [6. 隔离级别](#6-隔离级别)
- [7. 死锁处理](#7-死锁处理)
  - [7.1 死锁检测算法](#71-死锁检测算法)
  - [7.2 死锁预防策略](#72-死锁预防策略)
- [8. 代码逐行解读](#8-代码逐行解读)
  - [8.1 事务管理器 transaction.c](#81-事务管理器-transactionc)
  - [8.2 锁管理器 lock_manager.c](#82-锁管理器-lock_managerc)
  - [8.3 MVCC 可见性判断 mvcc.c](#83-mvcc-可见性判断-mvccc)
- [9. 可见性判断](#9-可见性判断)
  - [9.1 完整规则](#91-完整规则)
  - [9.2 五个具体例子](#92-五个具体例子)
- [10. 与真实数据库对比](#10-与真实数据库对比)
- [11. 习题](#11-习题)

---

## 1. 为什么需要事务

### 1.1 转账问题：没有事务会怎样

假设银行数据库里有两张表：

```
账户表 accounts:
+----+--------+---------+
| id | name   | balance |
+----+--------+---------+
|  1 | Alice  |     100 |
|  2 | Bob    |     100 |
+----+--------+---------+
```

Alice 想给 Bob 转 50 元。这个操作分两步：

```
步骤1: UPDATE accounts SET balance = balance - 50 WHERE id = 1;  -- Alice 减 50
步骤2: UPDATE accounts SET balance = balance + 50 WHERE id = 2;  -- Bob 加 50
```

如果没有事务，会发生什么可怕的事情？

**场景一：中途崩溃**

```
时间线:
  t1: 执行步骤1，Alice 余额变成 50
  t2: 数据库崩溃！步骤2 没来得及执行
  t3: 重启后数据库状态: Alice=50, Bob=100

结果: 50 元凭空消失了！银行要赔钱。
```

时序图：

```
   客户端              数据库              磁盘
      |                  |                  |
      |--- 转账 50 ----->|                  |
      |                  |--- Alice=50 ---->|  ← 写入磁盘
      |                  |                  |
      |                  |  💥 停电！       |
      |                  |  ✗ 没来得及写 Bob |
      |                  |                  |
      |                  |  重启后          |
      |                  |  Alice=50        |
      |                  |  Bob=100         |
      |                  |  总额从 200 变 150|
```

**场景二：并发冲突**

假设 Alice 同时给 Bob 转 50，又给 Charlie 转 50（Alice 只有 100 元）：

```
时间线:
  事务A: 转 50 给 Bob
  事务B: 转 50 给 Charlie

  t1: 事务A 读到 Alice 余额 = 100
  t2: 事务B 读到 Alice 余额 = 100  ← 读到了旧值！
  t3: 事务A 写入 Alice 余额 = 100 - 50 = 50
  t4: 事务B 写入 Alice 余额 = 100 - 50 = 50  ← 覆盖了事务A的结果
  t5: 事务A 写入 Bob 余额 = 150
  t6: 事务B 写入 Charlie 余额 = 150

结果: Alice=50, Bob=150, Charlie=150
      总额从 200 变成 350，凭空多出 150 元！
```

时序图：

```
  事务A (转给Bob)        事务B (转给Charlie)      数据库
      |                       |                     |
      |--- 读 Alice --------->|                     |  Alice=100
      |                       |--- 读 Alice ------->|  Alice=100
      |<-- 100 ---------------|                     |
      |                       |<-- 100 -------------|
      |                       |                     |
      |--- 写 Alice=50 ------>|                     |
      |                       |--- 写 Alice=50 ---->|  ← 覆盖！
      |                       |                     |
      |--- 写 Bob=150 ------->|                     |
      |                       |--- 写 Charlie=150->|
      |                       |                     |
      |                       |    Alice=50         |
      |                       |    Bob=150          |
      |                       |    Charlie=150      |
      |                       |    总额=350 💥      |
```

**事务要解决的就是这些问题**：把多个操作"打包"成一个不可分割的单元，要么全部成功，要么全部失败回滚。

### 1.2 ACID 四大性质详解

事务有四个基本性质，首字母缩写为 **ACID**：

#### A — Atomicity（原子性）

**定义**：事务中的所有操作要么全部成功，要么全部失败回滚，不存在"做了一半"的中间状态。

**通俗解释**：原子性就像"要么全做，要么不做"。转账时，扣钱和加钱是一个原子操作，不能只扣钱不加钱。

**保证机制**：WAL（预写日志）+ undo log
- 执行前先写日志，记录"我要做什么"
- 如果中途崩溃，重启时根据日志回滚未完成的事务
- 详见[章5 WAL 与崩溃恢复](05-wal-recovery.md)

**反例（没有原子性）**：

```
BEGIN;
  UPDATE accounts SET balance = balance - 50 WHERE id = 1;  -- 成功
  UPDATE accounts SET balance = balance + 50 WHERE id = 2;  -- 失败（比如约束冲突）
-- 如果没有原子性，第一条会保留，第二条回滚 → 钱没了
END;
```

#### C — Consistency（一致性）

**定义**：事务执行前后，数据库必须从一个一致状态转移到另一个一致状态。所谓"一致"是指满足所有预定义的约束（主键、外键、CHECK 约束、业务规则等）。

**通俗解释**：一致性就是"数据不能自相矛盾"。比如"所有账户余额之和不变"就是一个一致性约束，转账前后总额必须相等。

**保证机制**：由 A、I、D 共同保证
- 原子性保证不会停在中间状态
- 隔离性保证并发事务不会互相破坏
- 持久性保证提交后不会丢失

**注意**：一致性是"用户定义的"，数据库只提供工具（约束、触发器），具体什么算"一致"由应用层决定。

**例子**：

```
一致性约束: "所有账户余额之和 = 1000"

转账前: Alice=600, Bob=400, 总和=1000 ✓
转账后: Alice=550, Bob=450, 总和=1000 ✓  ← 一致性保持

如果发生丢失更新:
转账后: Alice=550, Bob=400, 总和=950  ✗  ← 一致性被破坏！
```

#### I — Isolation（隔离性）

**定义**：并发执行的多个事务之间互不干扰，效果等同于"一个一个串行执行"。

**通俗解释**：隔离性就像"每个事务在自己的小房间里工作，看不到别人正在改的数据"。

**保证机制**：2PL（锁）或 MVCC（多版本）

**问题**：完全隔离性能太差，所以实际数据库提供多个"隔离级别"，在性能和正确性之间权衡。详见第6节。

**反例（没有隔离性）**：见 1.1 的"场景二：并发冲突"。

#### D — Durability（持久性）

**定义**：事务一旦提交，对数据的修改就是永久的，即使系统崩溃也不会丢失。

**通俗解释**：持久性就是"提交了就板上钉钉"。你在 ATM 机存了钱，机器突然断电，钱不能丢。

**保证机制**：WAL redo log
- 提交时先把修改写入日志（顺序写，很快）
- 日志写入成功就算"提交成功"
- 数据页可以稍后再写入磁盘（异步刷盘）
- 崩溃后重启，重放日志把未落盘的修改重新应用

**反例（没有持久性）**：

```
BEGIN;
  INSERT INTO orders VALUES (1, 'iPhone', 9999);
COMMIT;  -- 返回"成功"
-- 此时突然断电
-- 重启后 order 表空空如也 → 客户付了钱却没订单！
```

#### ACID 总览表

| 性质 | 含义 | 通俗说法 | 保证机制 | 没有它会怎样 |
|---|---|---|---|---|
| **A**tomicity | 全做或全不做 | 要么成功要么回滚 | WAL + undo | 转账扣了钱没加上 |
| **C**onsistency | 数据不矛盾 | 规则不被破坏 | 由 A/I/D 共同保证 | 余额总和变了 |
| **I**solation | 事务互不干扰 | 各干各的 | 2PL 或 MVCC | 并发转账多出钱 |
| **D**urability | 提交后不丢 | 板上钉钉 | WAL redo | 提交后断电数据丢 |

### 1.3 现实类比

把事务类比为"签合同"：

| ACID | 签合同类比 |
|---|---|
| **A** 原子性 | 合同要么双方都签字生效，要么作废，不存在"只有一方签字"的中间状态 |
| **C** 一致性 | 合同内容必须合法，不能违反法律（约束） |
| **I** 隔离性 | 两个人同时和同一家公司签合同，互相不影响对方合同的内容 |
| **D** 持久性 | 合同签完存档后，即使办公室着火，合同也不能丢（要有备份） |

再比如"网购下单"：

```
一个订单事务包含:
  1. 扣减库存
  2. 扣减用户余额
  3. 创建订单记录
  4. 生成物流单

原子性: 这4步要么全成功，要么全回滚（不能扣了钱没下订单）
一致性: 库存不能变成负数，余额不能变成负数
隔离性: 两个人同时买最后一件商品，只有一个人能成功
持久性: 下单成功后，即使服务器崩溃，订单也不能丢
```

---

## 2. 并发问题

当多个事务同时读写同一份数据时，如果没有隔离机制，会出现以下几种"异常"。

### 2.1 脏读

**定义**：事务 T2 读到了事务 T1 **未提交**的数据。如果 T1 随后回滚，T2 读到的就是"脏"数据（根本不存在的数据）。

**时序图**：

```
  事务 T1                  事务 T2              数据库
     |                       |                    |
     |--- 写 X=100 --------->|                    |  X=100 (未提交)
     |                       |--- 读 X ---------->|
     |                       |<-- 100 ------------|  ← 脏读！
     |                       |                    |
     |--- ROLLBACK --------->|                    |  X 恢复为旧值
     |                       |                    |
     |                       |   T2 拿着 100 做决策
     |                       |   但 100 根本不存在！💥
```

**现实例子**：

```
T1: 给员工涨工资，先涨到 20000，但发现搞错人了，回滚
T2: 在 T1 涨工资后、回滚前，读到工资=20000，据此计算了年终奖
结果: 年终奖按 20000 算了，但工资其实没涨 → 财务对不上
```

### 2.2 不可重复读

**定义**：事务 T2 两次读取同一行数据，结果不一样。因为中间 T1 修改并提交了这行。

**注意**：和脏读的区别是——脏读读到的是**未提交**的数据，不可重复读读到的是**已提交**的数据。

**时序图**：

```
  事务 T1                  事务 T2              数据库
     |                       |                    |
     |                       |--- 读 X --------->|
     |                       |<-- 100 ------------|  第一次读
     |                       |                    |
     |--- 写 X=200 --------->|                    |
     |--- COMMIT ----------->|                    |  X=200 (已提交)
     |                       |                    |
     |                       |--- 读 X --------->|
     |                       |<-- 200 ------------|  第二次读，变了！
     |                       |                    |
     |                       |   同一事务两次读结果不同 💥
```

**现实例子**：

```
T2: 银行对账，先读 Alice 余额=100，记下来
T1: Alice 存入 100，提交
T2: 再读 Alice 余额=200，和第一次对不上 → 对账失败
```

### 2.3 幻读

**定义**：事务 T2 两次执行同一个查询（特别是带 WHERE 条件的查询），结果集的**行数**不一样。因为中间 T1 插入或删除了满足条件的行。

**注意**：和不可重复读的区别是——不可重复读是**同一行的值变了**，幻读是**行的数量变了**（多了或少了"幻影"行）。

**时序图**：

```
  事务 T1                  事务 T2              数据库
     |                       |                    |
     |                       |--- SELECT -------->|
     |                       |    WHERE age>18   |
     |                       |<-- 3 行 -----------|  第一次查到3行
     |                       |                    |
     |--- INSERT 一行 ------->|                   |
     |    (age=20)            |                   |
     |--- COMMIT ----------->|                    |
     |                       |                    |
     |                       |--- SELECT -------->|
     |                       |    WHERE age>18   |
     |                       |<-- 4 行 -----------|  第二次查到4行！
     |                       |                    |
     |                       |   多了一行"幻影" 💥|
```

**现实例子**：

```
T2: 统计成年用户数量，第一次查到 1000 人
T1: 注册了一个新成年用户，提交
T2: 再查，变成 1001 人 → 统计结果不一致
```

### 2.4 丢失更新

**定义**：两个事务都读取了同一行，然后都基于读到的值进行更新，后提交的事务会覆盖先提交的结果，导致先提交的更新"丢失"。

**时序图**：

```
  事务 T1                  事务 T2              数据库
     |                       |                    |
     |--- 读 X=100 --------->|                    |
     |                       |--- 读 X=100 ------>|
     |                       |                    |
     |--- 写 X=100+1=101 --->|                    |
     |--- COMMIT ----------->|                    |  X=101
     |                       |                    |
     |                       |--- 写 X=100+1=101->|  ← 应该是 102！
     |                       |--- COMMIT ------->|  X=101
     |                       |                    |
     |                       |   T1 的更新丢失 💥 |
```

**现实例子**：

```
两个用户同时给帖子点赞:
  帖子当前点赞数 = 100
  T1: 读到 100，写 101
  T2: 读到 100，写 101  ← 应该是 102
结果: 两个赞只算了一个
```

### 2.5 写偏斜

**定义**：两个事务读取了重叠的数据集，然后各自修改不相交的子集，结果违反了某种约束。

**时序图**：

```
约束: "至少有一个医生值班"（on_call 医生数 >= 1）

  事务 T1                  事务 T2              数据库
     |                       |                    |
     |--- 查值班医生数 ----->|                    |
     |<-- 2 (Alice,Bob) -----|                    |
     |                       |--- 查值班医生数 -->|
     |                       |<-- 2 (Alice,Bob) --|
     |                       |                    |
     |--- Alice 下班 -------->|                   |
     |    (on_call[Alice]=0)  |                   |
     |--- COMMIT ----------->|                    |
     |                       |                    |
     |                       |--- Bob 下班 ------>|
     |                       |    (on_call[Bob]=0)|
     |                       |--- COMMIT ------->|
     |                       |                    |
     |                       |   没人值班了！💥   |
     |                       |   (Alice=0,Bob=0) |
```

**注意**：写偏斜在快照隔离下仍然可能发生，需要更强的隔离级别（可串行化）才能防止。

### 2.6 并发问题复现场景汇总表

| 问题 | 描述 | 触发条件 | 危害 | 哪些隔离级别能防止 |
|---|---|---|---|---|
| **脏读** | 读到未提交的数据 | T2 读 T1 未提交的修改 | 读到根本不存在的数据 | Read Committed 及以上 |
| **不可重复读** | 同一行两次读结果不同 | T1 修改并提交，T2 两次读 | 同一事务内数据不一致 | Repeatable Read 及以上 |
| **幻读** | 同一查询两次结果行数不同 | T1 插入/删除并提交，T2 两次查 | 统计结果不一致 | Serializable |
| **丢失更新** | 后提交覆盖先提交 | T1、T2 都读后都写 | 更新丢失 | Repeatable Read + 行锁，或 Serializable |
| **写偏斜** | 各改不相交子集，违反约束 | T1、T2 读重叠集，各改一部分 | 约束被破坏 | Serializable (SSI) |

**复现场景详细表**：

| 问题 | 初始状态 | T1 操作 | T2 操作 | 结果 | 正确结果 |
|---|---|---|---|---|---|
| 脏读 | X=100 | 写 X=200, 回滚 | 读 X | T2 读到 200 | T2 应读到 100 |
| 不可重复读 | X=100 | 写 X=200, 提交 | 读 X 两次 | T2 读到 100, 200 | T2 应两次都读到 100 |
| 幻读 | 表中有3行 age>18 | 插入1行 age=20, 提交 | 查 age>18 两次 | T2 查到 3行, 4行 | T2 应两次都查到 3行 |
| 丢失更新 | X=100 | 读X, 写X+1, 提交 | 读X, 写X+1, 提交 | X=101 | X=102 |
| 写偏斜 | A=on, B=on | 读A,B; 写A=off | 读A,B; 写B=off | A=off,B=off | 应至少一个 on |

---

## 3. 2PL 详解

### 3.1 共享锁与排他锁

2PL（Two-Phase Locking，两阶段锁）是最经典的并发控制方法。它用两种锁来保护数据：

**共享锁（S 锁，Shared Lock）**：
- 也叫"读锁"
- 多个事务可以同时持有同一数据的 S 锁
- 用途：读数据时加 S 锁，防止别人写

**排他锁（X 锁，Exclusive Lock）**：
- 也叫"写锁"
- 同一时刻只有一个事务能持有 X 锁
- 用途：写数据时加 X 锁，防止别人读和写

**锁兼容矩阵**：

| 请求\持有 | S | X |
|---|---|---|
| **S** | ✓ 兼容 | ✗ 冲突 |
| **X** | ✗ 冲突 | ✗ 冲突 |

**解读**：
- 两个 S 锁兼容：两个人可以同时读
- S 和 X 冲突：一个人在写，另一个人不能读（否则可能读到未提交的数据）
- 两个 X 冲突：两个人不能同时写

**时序图：S 锁兼容**：

```
  事务 T1                  事务 T2              锁管理器
     |                       |                    |
     |--- lock_S(A) -------->|                    |  T1 持有 S 锁
     |<-- OK ----------------|                    |
     |                       |--- lock_S(A) ----->|  T2 也请求 S 锁
     |                       |<-- OK -------------|  兼容，允许！
     |                       |                    |
     |--- read(A) ---------->|                    |
     |                       |--- read(A) ------->|  两人同时读
     |                       |                    |
     |--- unlock(A) -------->|                    |
     |                       |--- unlock(A) ---->|
```

**时序图：S 与 X 冲突**：

```
  事务 T1                  事务 T2              锁管理器
     |                       |                    |
     |--- lock_X(A) -------->|                    |  T1 持有 X 锁
     |<-- OK ----------------|                    |
     |                       |--- lock_S(A) ----->|  T2 请求 S 锁
     |                       |    ⏳ 等待...      |  冲突！T2 阻塞
     |--- write(A) --------->|                    |
     |--- unlock(A) -------->|                    |  T1 释放 X 锁
     |                       |<-- OK -------------|  现在 T2 获得 S 锁
     |                       |--- read(A) ------->|
     |                       |--- unlock(A) ---->|
```

### 3.2 两阶段的含义

"两阶段"指的是事务的执行分为两个阶段：

**阶段1：增长阶段（Growing Phase）**
- 事务只能加锁，不能解锁
- 每访问一个数据项就加相应的锁

**阶段2：收缩阶段（Shrinking Phase）**
- 事务只能解锁，不能加新锁
- 逐步释放所有锁

**图示**：

```
            加锁数
              ^
              |          ┌─── 顶峰
              |         ╱     ╲
              |        ╱       ╲  收缩阶段
              |  增长 ╱         ╲  (只解锁)
              |  阶段╱           ╲
              |  (只  ╱           ╲
              |  加锁)╱             ╲
              |      ╱               ╲
              |     ╱                 ╲
              |    ╱                   ╲
              |───╱─────────────────────╲──→ 时间
                  ↑                     ↑
                开始                  结束
              加第一把锁          释放所有锁
```

**为什么必须"两阶段"？**

如果事务在释放一把锁之后又去加新锁，就可能破坏可串行性：

```
反例：不遵守两阶段
  T1: lock_X(A), write(A), unlock(A), lock_X(B), write(B), unlock(B)
  T2: lock_X(B), write(B), unlock(B), lock_X(A), write(A), unlock(A)

  T1 释放 A 后又去锁 B，T2 释放 B 后又去锁 A
  执行顺序可能不可串行化！
```

**严格两阶段锁（S2PL）**：
- 实际数据库通常用 S2PL
- 区别：所有 X 锁直到事务提交才释放（不是操作完就释放）
- 好处：保证严格性（其他事务看不到未提交的数据）

**强两阶段锁（SS2PL）**：
- 所有锁（S 和 X）都直到提交才释放
- minidb 采用这种方式（见 `lm_unlock_all` 在事务结束时调用）

### 3.3 死锁的产生

**死锁定义**：两个或多个事务互相等待对方释放锁，形成循环依赖，永远无法继续。

**经典死锁场景**：

```
  事务 T1                  事务 T2
     |                       |
     |--- lock_X(A) -------->|  T1 持有 A
     |                       |--- lock_X(B) ----->|  T2 持有 B
     |                       |                    |
     |--- lock_X(B) -------->|  T1 等 T2 释放 B  |
     |    ⏳ 等待...          |--- lock_X(A) ---->|  T2 等 T1 释放 A
     |                       |    ⏳ 等待...      |
     |                       |                    |
     |                       |   互相等待，死锁！💀
```

**等待图**：

```
    T1 ────等待──── T2
    T2 ────等待──── T1

    形成环: T1 → T2 → T1  →  死锁！
```

**死锁产生的四个必要条件**（操作系统经典理论）：

| 条件 | 说明 | 在 2PL 中的体现 |
|---|---|---|
| 互斥 | 资源同一时刻只能被一个进程使用 | X 锁独占 |
| 占有并等待 | 持有资源的同时可以请求新资源 | 持有 A 的同时请求 B |
| 不可剥夺 | 不能强行夺走别人持有的资源 | 锁不能被抢走 |
| 循环等待 | 形成等待环 | T1 等 T2，T2 等 T1 |

### 3.4 死锁的检测

**方法一：等待图（Wait-For Graph）**

构建一个有向图：
- 节点 = 事务
- 边 Ti → Tj 表示 Ti 在等待 Tj 释放锁

**检测规则**：图中存在环 ⟺ 发生死锁

```
等待图:
    T1 ──→ T2 ──→ T3 ──→ T1   ← 有环，死锁！
    
    检测方法: 深度优先搜索(DFS) 找环
    时间复杂度: O(V+E), V=事务数, E=等待边数
```

**方法二：超时检测**

给每个锁请求设置超时时间，超过一定时间还没拿到锁就认为死锁。

```
  T1 请求锁 B，等待...
  超时时间 5 秒
  5 秒后还没拿到 → 判定死锁，回滚 T1
```

**优缺点对比**：

| 方法 | 优点 | 缺点 |
|---|---|---|
| 等待图 | 精确，无误判 | 维护图开销大 |
| 超时 | 简单，开销小 | 不精确，可能误判或漏判 |

---

## 4. MVCC 详解

### 4.1 多版本概念

MVCC（Multi-Version Concurrency Control，多版本并发控制）的核心思想：

**对同一行数据保留多个历史版本，每个事务根据自己的"快照"看到合适的版本。**

**类比**：就像 Git 的分支——每个人在自己的分支上工作，看到的是自己分支的代码，不影响别人。提交时再合并。

**图示**：

```
  行 A 的版本链:
  
  时间 →
  
  t1:  A=100  (版本1, xmin=T1)
       ↓
  t2:  T2 修改 A=200
       A=100  (版本1, xmin=T1, xmax=T2)  ← 被T2删除
       A=200  (版本2, xmin=T2)            ← 新版本
            ↓
  t3:  T3 修改 A=300
       A=100  (版本1, xmin=T1, xmax=T2)
       A=200  (版本2, xmin=T2, xmax=T3)
       A=300  (版本3, xmin=T3)
  
  不同事务看到不同版本:
    事务在 t1 开始的快照 → 看到 A=100
    事务在 t2 开始的快照 → 看到 A=200
    事务在 t3 开始的快照 → 看到 A=300
```

### 4.2 xmin 与 xmax 的含义

每个数据版本（tuple）都带一个 MVCC 头部：

```c
typedef struct {
    txn_id_t xmin;  // 创建该版本的事务ID
    txn_id_t xmax;  // 删除该版本的事务ID（0 = 未删除）
} mvcc_header_t;
```

**xmin（创建事务）**：
- 记录是哪个事务创建了这个版本
- INSERT 时：xmin = 当前事务
- UPDATE 时：新版本的 xmin = 当前事务

**xmax（删除事务）**：
- 记录是哪个事务删除（或覆盖）了这个版本
- DELETE 时：xmax = 当前事务
- UPDATE 时：旧版本的 xmax = 当前事务
- 0（MVCC_NOT_DELETED）表示这个版本还没被删除

**版本生命周期图**：

```
  INSERT (事务T1):
  ┌──────────────────┐
  │ data=100         │
  │ xmin=T1          │  ← 创建者
  │ xmax=0           │  ← 未删除
  └──────────────────┘

  UPDATE (事务T2, 把100改成200):
  ┌──────────────────┐     ┌──────────────────┐
  │ data=100         │     │ data=200         │
  │ xmin=T1          │     │ xmin=T2          │  ← 新版本
  │ xmax=T2          │ ──→ │ xmax=0           │
  └──────────────────┘     └──────────────────┘
    旧版本被标记删除         新版本

  DELETE (事务T3):
  ┌──────────────────┐
  │ data=200         │
  │ xmin=T2          │
  │ xmax=T3          │  ← 被T3删除
  └──────────────────┘
```

### 4.3 快照隔离原理

**快照隔离（Snapshot Isolation, SI）**：

每个事务在开始时获取一个"快照"，记录此刻"哪些事务已经提交"。事务执行期间只能看到快照中已提交事务的修改。

**快照的内容**：

```c
typedef struct {
    txn_id_t txn_ids[SNAPSHOT_CAPACITY];  // 已提交事务ID列表
    int count;                            // 数量
} txn_snapshot_t;
```

**取快照时机**：

```
  时间轴:
  T1开始 ── T1提交 ── T2开始 ── T3开始 ── T3提交 ── T2提交
                              ↑
                          T3在这里取快照
                          快照 = {T1}  (只有T1已提交)
                          T3 看不到 T2 的修改（T2还没提交）
```

**快照隔离的可见性规则**：

事务 T（持有快照 S）能看到版本 V 当且仅当：
1. V 的创建者（xmin）已经提交且在快照中（或就是 T 自己）
2. V 的删除者（xmax）未提交或不在快照中（或就是 T 自己）

**图示**：

```
  事务时间线:
  
  T1: ──────●提交──────────────────────────  (创建版本V1)
  T2: ──────────────●提交───────────────  (创建版本V2)
  T3: ────────────────────●(开始)────●提交  (读操作)
                          ↑
                     T3 的快照 = {T1, T2}
                     
  版本 V1 (xmin=T1, xmax=0):
    T1 在快照中 ✓ → 创建者已提交
    xmax=0 → 未被删除
    → T3 能看到 V1 ✓
  
  版本 V2 (xmin=T2, xmax=0):
    T2 在快照中 ✓ → 创建者已提交
    xmax=0 → 未被删除
    → T3 能看到 V2 ✓
  
  如果有版本 V3 (xmin=T4, xmax=0), T4 在 T3 开始后才开始:
    T4 不在快照中 ✗ → 创建者未提交
    → T3 看不到 V3 ✗
```

### 4.4 读不阻塞写，写不阻塞读

这是 MVCC 相比 2PL 最大的优势。

**2PL 下：读阻塞写**

```
  事务 T1 (读)             事务 T2 (写)          锁管理器
     |                       |                    |
     |--- lock_S(A) -------->|                    |  T1 持有 S 锁
     |--- read(A) ---------->|                    |
     |                       |--- lock_X(A) ----->|  T2 请求 X 锁
     |                       |    ⏳ 阻塞！       |  S 和 X 冲突
     |                       |                    |
     |  T1 慢慢读...         |  T2 一直等...      |
     |--- unlock(A) -------->|                    |
     |                       |<-- OK -------------|  T1 释放后 T2 才能继续
```

**MVCC 下：读不阻塞写**

```
  事务 T1 (读)             事务 T2 (写)          版本链
     |                       |                    |
     |--- 取快照 S={...} --->|                    |
     |                       |                    |  A=100 (xmin=T0)
     |--- read(A) ---------->|                    |
     |<-- 100 (旧版本) ------|                    |
     |                       |--- write(A)=200 -->|
     |                       |                    |  A=100 (xmax=T2) ← 标记删除
     |                       |                    |  A=200 (xmin=T2) ← 新版本
     |                       |                    |
     |  T1 不受影响！        |  T2 不用等！       |
     |  仍然看到 A=100       |                    |
     |                       |--- commit ------->|
     |                       |                    |
     |  T1 仍然看到 A=100    |                    |  ← 直到 T1 结束
     |  (快照没变)           |                    |
```

**性能对比**：

| 场景 | 2PL | MVCC |
|---|---|---|
| 读-读 | 不阻塞 ✓ | 不阻塞 ✓ |
| 读-写 | **阻塞** ✗ | **不阻塞** ✓ |
| 写-读 | **阻塞** ✗ | **不阻塞** ✓ |
| 写-写 | 阻塞 ✗ | 阻塞 ✗ |

**结论**：MVCC 下只有"写-写"会冲突，读操作完全不阻塞任何操作。对于"读多写少"的应用（大多数 Web 应用），MVCC 性能远超 2PL。

---

## 5. MVCC vs 2PL

### 优缺点对比

| 维度 | 2PL | MVCC |
|---|---|---|
| **读-写冲突** | 阻塞 | 不阻塞 |
| **写-读冲突** | 阻塞 | 不阻塞 |
| **实现复杂度** | 较简单 | 较复杂（版本管理） |
| **存储开销** | 低（只有锁表） | 高（保留多版本） |
| **回滚开销** | 低（解锁即可） | 中（标记版本无效） |
| **死锁** | 会死锁 | 不会死锁（但写-写可能冲突） |
| **空间膨胀** | 无 | 有（需要 VACUUM 清理） |
| **隔离级别** | 可到 Serializable | 通常到 Repeatable Read |
| **适合场景** | 写多读少 | 读多写少 |
| **真实数据库** | 较少单独使用 | PostgreSQL, MySQL InnoDB, Oracle |

### 性能对比图

```
  吞吐量
    ^
    |        MVCC ─────────────────
    |       ╱
    |      ╱
    |     ╱   2PL ─────────────
    |    ╱   ╱
    |   ╱   ╱
    |  ╱   ╱
    | ╱   ╱
    |╱   ╱
    |── ╱──────────────────────→ 读操作比例
        ↑
       读写各半
    
    读比例越高，MVCC 优势越明显
    写比例高时，两者差距小（写-写都冲突）
```

### 何时用哪个？

| 场景 | 推荐 | 理由 |
|---|---|---|
| Web 应用（读多写少） | MVCC | 读不阻塞写，并发性能好 |
| OLTP（事务处理） | MVCC | 现代数据库标配 |
| OLAP（分析查询） | MVCC | 长查询不阻塞写入 |
| 高冲突写入 | 2PL 或 MVCC+锁 | 写-写冲突时锁更直接 |
| 教学目的 | 两者都实现 | 对比理解（minidb 的选择） |

---

## 6. 隔离级别

SQL 标准定义了四个隔离级别，从弱到强：

### 四级隔离详解

#### 1. Read Uncommitted（读未提交）

```
  最低级别，几乎不加锁
  允许脏读
  实际中很少使用
```

#### 2. Read Committed（读已提交）

```
  每次读取都获取最新已提交的数据
  防止脏读
  但允许不可重复读
  PostgreSQL、Oracle 默认级别
```

#### 3. Repeatable Read（可重复读）

```
  事务开始时取快照，整个事务用同一快照
  防止脏读、不可重复读
  但可能允许幻读（SQL标准说允许，但很多实现实际防止了）
  MySQL InnoDB 默认级别
```

#### 4. Serializable（可串行化）

```
  最高级别，效果等同于事务串行执行
  防止所有异常
  性能开销最大
  PostgreSQL 通过 SSI 实现
```

### 隔离级别 vs 防止的问题

| 隔离级别 | 脏读 | 不可重复读 | 幻读 | 丢失更新 | 写偏斜 |
|---|---|---|---|---|---|
| Read Uncommitted | ✗ 可能 | ✗ 可能 | ✗ 可能 | ✗ 可能 | ✗ 可能 |
| Read Committed | ✓ 防止 | ✗ 可能 | ✗ 可能 | ✗ 可能 | ✗ 可能 |
| Repeatable Read | ✓ 防止 | ✓ 防止 | ✗ 可能* | ✓ 防止 | ✗ 可能 |
| Serializable | ✓ 防止 | ✓ 防止 | ✓ 防止 | ✓ 防止 | ✓ 防止 |

> *注：SQL 标准说 Repeatable Read 允许幻读，但 MySQL InnoDB 的 RR 实际上通过 Next-Key Lock 防止了幻读。PostgreSQL 的 RR（实际是 Snapshot Isolation）不能防止幻读。

### 隔离级别 vs 实现方式

| 隔离级别 | 2PL 实现方式 | MVCC 实现方式 |
|---|---|---|
| Read Uncommitted | 不加锁 | 不取快照，读最新版本 |
| Read Committed | 短锁（读完即释放） | 每条语句取新快照 |
| Repeatable Read | 长锁（事务结束才释放） | 事务开始取快照，全程不变 |
| Serializable | 长锁 + 谓词锁 | 快照 + SSI（可串行化快照隔离） |

### minidb 的选择

minidb 的 MVCC 实现的是 **快照隔离（Snapshot Isolation）**，大致对应 Repeatable Read：

```c
// transaction.c: txn_get_snapshot()
// 在事务开始时取快照，记录所有已提交的事务ID
void txn_get_snapshot(transaction_manager_t *mgr, txn_snapshot_t *snap) {
    snap->count = 0;
    for (txn_id_t i = 1; i < mgr->next_txn_id && snap->count < SNAPSHOT_CAPACITY; i++) {
        if (i < MAX_TXNS && mgr->txns[i].status == TXN_COMMITTED) {
            snap->txn_ids[snap->count++] = i;
        }
    }
}
```

### 各数据库默认隔离级别

| 数据库 | 默认隔离级别 | 实现方式 |
|---|---|---|
| PostgreSQL | Read Committed | MVCC |
| MySQL InnoDB | Repeatable Read | MVCC + Next-Key Lock |
| Oracle | Read Committed | MVCC |
| SQL Server | Read Committed | 2PL（默认）/ MVCC（可选） |
| SQLite | Serializable | 串行执行 |
| **minidb** | **Repeatable Read (SI)** | **MVCC** |

---

## 7. 死锁处理

### 7.1 死锁检测算法

#### 算法一：等待图 + 环检测

**步骤**：
1. 维护一个等待图：节点是事务，边是"等待"关系
2. 每次有事务等待锁时，添加一条边
3. 检测图中是否有环
4. 有环则选择一个事务作为"牺牲品"，回滚它

**伪代码**：

```
function detect_deadlock():
    graph = build_wait_for_graph()  // 构建等待图
    cycle = find_cycle(graph)       // DFS 找环
    if cycle != null:
        victim = choose_victim(cycle)  // 选择回滚哪个事务
        abort(victim)                  // 回滚牺牲品
        return true
    return false

function find_cycle(graph):
    for each node in graph:
        if dfs(node, visited, path):
            return path  // 返回环上的节点
    return null

function dfs(node, visited, path):
    if node in path:
        return path[path.index(node):]  // 找到环
    if node in visited:
        return null
    visited.add(node)
    path.add(node)
    for each neighbor in graph[node]:
        result = dfs(neighbor, visited, path)
        if result != null:
            return result
    path.remove(node)
    return null
```

**例子**：

```
  等待图:
    T1 → T2  (T1 等 T2 释放锁)
    T2 → T3  (T2 等 T3 释放锁)
    T3 → T1  (T3 等 T1 释放锁)
    
  DFS 从 T1 开始:
    T1 → T2 → T3 → T1  ← 回到 T1，找到环！
    环: [T1, T2, T3]
    
  选择牺牲品: 通常选回滚代价最小的事务
    - 做的操作最少
    - 持有锁最少
    - 事务ID最大（年轻的事务回滚）
  
  回滚 T3 (假设选它)
    T3 释放所有锁
    T2 获得锁，继续执行
    T1 继续等待 T2
```

#### 算法二：超时检测

**步骤**：
1. 给每个锁请求设置超时时间（如 5 秒）
2. 超时后判定为死锁
3. 回滚超时的事务

**伪代码**：

```
function lock_with_timeout(txn, rid, mode, timeout=5s):
    start = now()
    while has_conflict(rid, txn, mode):
        if now() - start > timeout:
            abort(txn)  // 超时，回滚
            raise DeadlockError
        sleep(100ms)  // 稍等再试
    acquire_lock(rid, txn, mode)
```

#### 算法三：Wound-Wait（伤害-等待）

一种死锁预防策略，基于时间戳：

```
  规则:
    老事务请求年轻事务持有的锁 → 抢走（wound，伤害年轻事务，让它回滚）
    年轻事务请求老事务持有的锁 → 等待（wait）
```

#### 算法四：Wait-Die（等待-死亡）

另一种基于时间戳的预防策略：

```
  规则:
    老事务请求年轻事务持有的锁 → 等待（wait）
    年轻事务请求老事务持有的锁 → 回滚自己（die，死亡）
```

### 7.2 死锁预防策略

| 策略 | 思想 | 优点 | 缺点 |
|---|---|---|---|
| 等待图检测 | 检测环，回滚牺牲品 | 精确 | 维护图开销 |
| 超时检测 | 超时即回滚 | 简单 | 不精确 |
| Wound-Wait | 老事务抢锁 | 无死锁 | 年轻事务可能饿死 |
| Wait-Die | 年轻事务让步 | 无死锁 | 年轻事务可能饿死 |
| 资源排序 | 按固定顺序加锁 | 简单有效 | 需要预先知道访问顺序 |

**资源排序法**：

```
  规则: 所有事务必须按资源ID升序加锁
  
  T1 要锁 A 和 B (A < B):
    lock(A), lock(B)  ← 先A后B
  
  T2 要锁 A 和 B:
    lock(A), lock(B)  ← 也是先A后B
  
  不会死锁！因为不可能出现 T1 锁 A 等 B，T2 锁 B 等 A 的情况
  (T2 也会先锁 A，如果 A 被 T1 持有，T2 会等，不会去锁 B)
```

### minidb 的死锁处理

minidb 是教学项目，简化了死锁处理：

```c
// lock_manager.c
bool lm_has_deadlock(lock_manager_t *lm) {
    (void)lm;
    return false;  // 简化：假设单线程，不检测死锁
}
```

**为什么简化？**
- minidb 的锁管理器是单线程的
- 实际死锁检测需要等待图，复杂度较高
- 教学重点在 MVCC，不在死锁

---

## 8. 代码逐行解读

### 8.1 事务管理器 transaction.c

#### 数据结构

```c
// transaction.h
typedef uint32_t txn_id_t;           // 事务ID类型（32位无符号整数）
#define INVALID_TXN_ID ((txn_id_t)0) // 0 表示无效事务ID

typedef enum {
    TXN_ACTIVE = 0,      // 活跃中（正在执行）
    TXN_COMMITTED = 1,   // 已提交
    TXN_ABORTED = 2,     // 已回滚
} txn_status_t;

typedef struct {
    txn_id_t id;              // 事务ID
    txn_status_t status;      // 当前状态
    lsn_t begin_lsn;          // BEGIN 日志的 LSN
    lsn_t commit_lsn;         // COMMIT 日志的 LSN
} txn_info_t;

#define MAX_TXNS 1024              // 最多 1024 个事务
#define SNAPSHOT_CAPACITY 256      // 快照最多记录 256 个已提交事务

typedef struct {
    txn_id_t txn_ids[SNAPSHOT_CAPACITY]; // 已提交事务ID数组
    int count;                           // 数量
} txn_snapshot_t;

struct transaction_manager {
    wal_t *wal;                    // WAL 日志指针
    txn_id_t next_txn_id;          // 下一个事务ID（递增）
    txn_info_t txns[MAX_TXNS];     // 事务信息表（按ID索引）
};
```

#### txn_mgr_create — 创建事务管理器

```c
// transaction.c:11-16
transaction_manager_t *txn_mgr_create(wal_t *wal) {
    transaction_manager_t *mgr = calloc(1, sizeof(transaction_manager_t));
    //  calloc 分配内存并清零
    //  sizeof(transaction_manager_t) 包含 wal 指针、next_txn_id、txns 数组
    
    mgr->wal = wal;
    //  关联 WAL 日志，事务操作会写日志
    
    mgr->next_txn_id = 1;
    //  事务ID从1开始（0是INVALID_TXN_ID）
    
    return mgr;
}
```

#### txn_begin — 开始事务

```c
// transaction.c:22-33
txn_id_t txn_begin(transaction_manager_t *mgr) {
    txn_id_t id = mgr->next_txn_id++;
    //  分配新事务ID，next_txn_id 递增
    //  例如: 第一次调用 id=1, 第二次 id=2, ...
    
    lsn_t lsn = wal_begin(mgr->wal, id);
    //  写 WAL BEGIN 日志记录
    //  这是持久性的要求: 事务开始前先记日志
    
    if (id < MAX_TXNS) {
        //  检查没超出上限（1024）
        mgr->txns[id].id = id;
        mgr->txns[id].status = TXN_ACTIVE;
        //  标记为活跃状态
        
        mgr->txns[id].begin_lsn = lsn;
        //  记录 BEGIN 日志的 LSN，用于崩溃恢复
        
        mgr->txns[id].commit_lsn = INVALID_LSN;
        //  还没提交，commit_lsn 设为无效
    }
    return id;
    //  返回事务ID给调用者
}
```

**时序图**：

```
  调用者              txn_begin()            WAL
     |                    |                   |
     |--- txn_begin ----->|                   |
     |                    |--- wal_begin ---->|
     |                    |<-- lsn=100 -------|  (日志位置)
     |                    |                   |
     |                    | txns[1].status    |
     |                    | = TXN_ACTIVE      |
     |                    |                   |
     |<-- id=1 -----------|                   |
     |                    |                   |
```

#### txn_commit — 提交事务

```c
// transaction.c:35-43
bool txn_commit(transaction_manager_t *mgr, txn_id_t txn_id) {
    if (txn_id == INVALID_TXN_ID || txn_id >= MAX_TXNS) return false;
    //  参数检查: 事务ID不能是0，不能超过上限
    
    if (mgr->txns[txn_id].status != TXN_ACTIVE) return false;
    //  状态检查: 只有活跃的事务才能提交
    //  已经提交或回滚的事务不能再次提交
    
    lsn_t lsn = wal_commit(mgr->wal, txn_id);
    //  写 WAL COMMIT 日志记录
    //  关键: 日志写入成功，事务就算"提交成功"
    //  即使数据页还没刷盘，崩溃后也能用日志恢复
    
    mgr->txns[txn_id].status = TXN_COMMITTED;
    //  更新状态为已提交
    
    mgr->txns[txn_id].commit_lsn = lsn;
    //  记录 COMMIT 日志的 LSN
    
    return true;
    //  返回成功
}
```

**注意**：`wal_commit` 写入日志后，即使数据库立即崩溃，重启时也能通过 redo log 恢复这个事务的修改。这就是持久性（D）的保证。

#### txn_abort — 回滚事务

```c
// transaction.c:45-52
bool txn_abort(transaction_manager_t *mgr, txn_id_t txn_id) {
    if (txn_id == INVALID_TXN_ID || txn_id >= MAX_TXNS) return false;
    //  参数检查
    
    if (mgr->txns[txn_id].status != TXN_ACTIVE) return false;
    //  状态检查: 只有活跃的事务才能回滚
    
    wal_abort(mgr->wal, txn_id);
    //  写 WAL ABORT 日志记录
    //  崩溃恢复时会用 undo log 回滚这个事务的修改
    
    mgr->txns[txn_id].status = TXN_ABORTED;
    //  更新状态为已回滚
    
    return true;
}
```

#### txn_get_snapshot — 获取快照

```c
// transaction.c:67-74
void txn_get_snapshot(transaction_manager_t *mgr, txn_snapshot_t *snap) {
    snap->count = 0;
    //  初始化快照: 清空计数
    
    for (txn_id_t i = 1; i < mgr->next_txn_id && snap->count < SNAPSHOT_CAPACITY; i++) {
        //  遍历所有已分配的事务ID (从1到next_txn_id-1)
        //  同时检查快照容量没满
        
        if (i < MAX_TXNS && mgr->txns[i].status == TXN_COMMITTED) {
            //  只收集"已提交"的事务
            //  活跃和已回滚的事务不放入快照
            
            snap->txn_ids[snap->count++] = i;
            //  加入快照列表
        }
    }
}
```

**这段代码的含义**：

```
  当前事务管理器状态:
    txns[1] = COMMITTED  ← 已提交，加入快照
    txns[2] = ACTIVE     ← 活跃，不加入
    txns[3] = COMMITTED  ← 已提交，加入快照
    txns[4] = ABORTED    ← 已回滚，不加入
    txns[5] = ACTIVE     ← 活跃，不加入
    next_txn_id = 6
  
  获取快照结果:
    snap.txn_ids = [1, 3]
    snap.count = 2
  
  含义: 快照中只有 T1 和 T3 的修改是可见的
        T2、T4、T5 的修改不可见
```

#### txn_snapshot_contains — 检查事务是否在快照中

```c
// transaction.c:76-80
bool txn_snapshot_contains(const txn_snapshot_t *snap, txn_id_t txn_id) {
    for (int i = 0; i < snap->count; i++) {
        //  线性搜索快照数组
        
        if (snap->txn_ids[i] == txn_id) return true;
        //  找到了，返回 true
    }
    return false;
    //  没找到，返回 false
}
```

**性能说明**：这是线性搜索，O(n) 复杂度。生产数据库会用哈希表或位图来加速。minidb 为了简单用了线性搜索。

### 8.2 锁管理器 lock_manager.c

#### 数据结构

```c
// lock_manager.c
#define LM_TABLE_SIZE 2048  // 锁表大小（哈希表桶数）

typedef struct {
    rid_t rid;          // 被锁定的行ID (page_id + slot_id)
    txn_id_t txn;       // 持有锁的事务ID
    lock_mode_t mode;   // 锁模式 (S 或 X)
    bool used;          // 此槽位是否被使用
} lock_entry_t;

struct lock_manager {
    lock_entry_t table[LM_TABLE_SIZE];  // 哈希表
    int num_locks;                       // 当前锁总数
};
```

#### rid_hash — 行ID哈希函数

```c
// lock_manager.c:19-22
static uint32_t rid_hash(rid_t rid) {
    uint32_t h = rid.page_id * 31u + rid.slot_id;
    //  简单哈希: page_id 乘以 31 再加 slot_id
    //  31 是质数，分布比较均匀
    
    return h % LM_TABLE_SIZE;
    //  取模映射到哈希表范围 [0, 2047]
}
```

#### find_lock — 查找锁

```c
// lock_manager.c:24-36
static lock_entry_t *find_lock(lock_manager_t *lm, rid_t rid, txn_id_t txn) {
    uint32_t h = rid_hash(rid);
    //  计算哈希起始位置
    
    for (int i = 0; i < LM_TABLE_SIZE; i++) {
        int idx = (h + i) % LM_TABLE_SIZE;
        //  线性探测: 从哈希位置开始逐个查找
        
        if (!lm->table[idx].used) return NULL;
        //  遇到空槽位，说明没找到（假设没有删除标记）
        
        if (lm->table[idx].rid.page_id == rid.page_id &&
            lm->table[idx].rid.slot_id == rid.slot_id &&
            lm->table[idx].txn == txn) {
            //  行ID和事务ID都匹配
            return &lm->table[idx];
        }
    }
    return NULL;
    //  遍历完整个表都没找到
}
```

#### has_conflict — 检查锁冲突

```c
// lock_manager.c:38-53
static bool has_conflict(lock_manager_t *lm, rid_t rid, txn_id_t txn, lock_mode_t mode) {
    uint32_t h = rid_hash(rid);
    
    for (int i = 0; i < LM_TABLE_SIZE; i++) {
        int idx = (h + i) % LM_TABLE_SIZE;
        if (!lm->table[idx].used) return false;
        //  空槽位，没有冲突
        
        lock_entry_t *e = &lm->table[idx];
        if (e->rid.page_id != rid.page_id || e->rid.slot_id != rid.slot_id) continue;
        //  不是同一行，跳过
        
        if (e->txn == txn) continue;
        //  同一事务持有的锁，不冲突（自己不和自己冲突）
        
        if (e->mode == LOCK_EXCLUSIVE || mode == LOCK_EXCLUSIVE) {
            return true;
            //  根据锁兼容矩阵:
            //  已有 X 锁 → 任何请求都冲突
            //  请求 X 锁 → 任何持有都冲突
            //  只有 S-S 不冲突，但上面两个条件已经覆盖了所有冲突情况
        }
    }
    return false;
}
```

**锁兼容矩阵的实现**：

```
  代码: if (e->mode == LOCK_EXCLUSIVE || mode == LOCK_EXCLUSIVE) return true;
  
  等价于:
    已有\请求    S       X
      S         不冲突   冲突     ← e->mode==S, mode==X → 第二个条件成立
      X         冲突     冲突     ← e->mode==X → 第一个条件成立
    
  只有 S-S 不触发任何条件 → 不冲突 ✓
```

#### lm_lock — 加锁

```c
// lock_manager.c:64-88
bool lm_lock(lock_manager_t *lm, txn_id_t txn, rid_t rid, lock_mode_t mode) {
    lock_entry_t *existing = find_lock(lm, rid, txn);
    if (existing) {
        //  同一事务已经持有这行的锁
        
        if (mode == LOCK_EXCLUSIVE) existing->mode = LOCK_EXCLUSIVE;
        //  如果请求的是 X 锁，升级已有锁为 X
        //  (S 锁升级为 X 锁，但不处理升级冲突——简化)
        
        return true;
        //  已持有锁，直接成功
    }
    
    if (has_conflict(lm, rid, txn, mode)) {
        return false;
        //  有冲突，加锁失败
        //  注意: minidb 直接返回 false，不阻塞等待
        //  生产数据库会让事务阻塞等待，直到锁释放
    }
    
    //  无冲突，插入新锁
    uint32_t h = rid_hash(rid);
    for (int i = 0; i < LM_TABLE_SIZE; i++) {
        int idx = (h + i) % LM_TABLE_SIZE;
        if (!lm->table[idx].used) {
            //  找到空槽位
            lm->table[idx].rid = rid;
            lm->table[idx].txn = txn;
            lm->table[idx].mode = mode;
            lm->table[idx].used = true;
            lm->num_locks++;
            return true;
        }
    }
    return false;
    //  哈希表满了，加锁失败
}
```

#### lm_unlock_all — 释放事务的所有锁

```c
// lock_manager.c:98-105
void lm_unlock_all(lock_manager_t *lm, txn_id_t txn) {
    for (int i = 0; i < LM_TABLE_SIZE; i++) {
        //  遍历整个锁表
        
        if (lm->table[i].used && lm->table[i].txn == txn) {
            //  找到该事务持有的锁
            
            lm->table[i].used = false;
            //  标记为未使用（释放）
            
            lm->num_locks--;
            //  锁计数减一
        }
    }
}
```

**使用场景**：事务提交或回滚时调用，释放该事务持有的所有锁。这就是 SS2PL（强两阶段锁）——所有锁在事务结束时才释放。

### 8.3 MVCC 可见性判断 mvcc.c

#### mvcc_init — 初始化版本

```c
// mvcc.c:3-6
void mvcc_init(mvcc_header_t *h, txn_id_t xmin) {
    h->xmin = xmin;
    //  设置创建者事务ID
    
    h->xmax = MVCC_NOT_DELETED;
    //  xmax = 0，表示未被删除
}
```

#### mvcc_visible — 可见性判断（核心函数）

```c
// mvcc.c:8-29
bool mvcc_visible(const mvcc_header_t *h,
                  const txn_snapshot_t *snapshot,
                  txn_id_t current_txn) {
    //  h: 版本的 MVCC 头部 (xmin, xmax)
    //  snapshot: 当前事务的快照（已提交事务列表）
    //  current_txn: 当前事务ID
    
    // === 情况1: 自己创建的版本 ===
    if (h->xmin == current_txn) {
        //  这个版本是当前事务自己创建的
        
        if (h->xmax == current_txn) return false;
        //  自己又删了它 → 不可见
        
        if (h->xmax != MVCC_NOT_DELETED && txn_snapshot_contains(snapshot, h->xmax)) {
            return false;
            //  被某个已提交事务删了 → 不可见
        }
        return true;
        //  否则可见（自己创建且没被删除）
    }
    
    // === 情况2: 别人创建的版本 ===
    if (!txn_snapshot_contains(snapshot, h->xmin)) {
        return false;
        //  创建者不在快照中（未提交或已回滚）→ 不可见
        //  这防止了脏读
    }
    
    // === 检查是否被删除 ===
    if (h->xmax != MVCC_NOT_DELETED) {
        //  有人删除了这个版本
        
        if (h->xmax == current_txn) return false;
        //  自己删的 → 不可见
        
        if (txn_snapshot_contains(snapshot, h->xmax)) return false;
        //  删除者已提交 → 不可见
    }
    
    return true;
    //  创建者已提交，且未被已提交事务删除 → 可见
}
```

**这段代码的逻辑流程图**：

```
                    开始
                      |
              xmin == 当前事务?
                /         \
             是            否
              |             |
        xmax == 当前事务?   xmin 在快照中?
          /      \          /       \
         是       否        否       是
          |        |         |        |
       不可见   xmax已提交   不可见   xmax != 0?
               且在快照中?           /       \
                /      \           是        否
               是       否          |         |
                |        |       xmax==当前?  可见
             不可见     可见      /     \
                        是      否
                        |        |
                     不可见   xmax在快照中?
                              /       \
                             是        否
                              |         |
                           不可见      可见
```

#### mvcc_deleted — 判断版本是否已删除

```c
// mvcc.c:31-37
bool mvcc_deleted(const mvcc_header_t *h,
                  const txn_snapshot_t *snapshot,
                  txn_id_t current_txn) {
    if (h->xmax == MVCC_NOT_DELETED) return false;
    //  xmax=0，从未被删除
    
    if (h->xmax == current_txn) return true;
    //  自己删的 → 已删除
    
    if (txn_snapshot_contains(snapshot, h->xmax)) return true;
    //  删除者已提交 → 已删除
    
    return false;
    //  删除者未提交或已回滚 → 实际未删除
}
```

---

## 9. 可见性判断

### 9.1 完整规则

事务 T（持有快照 S）看到版本 V（xmin=Xc, xmax=Xd）的完整判断规则：

| 规则 | 条件 | 结果 | 说明 |
|---|---|---|---|
| R1 | Xc == T 且 Xd == T | 不可见 | 自己创建又自己删除 |
| R2 | Xc == T 且 Xd == 0 | 可见 | 自己创建，未删除 |
| R3 | Xc == T 且 Xd 在 S 中 | 不可见 | 自己创建，被已提交事务删除 |
| R4 | Xc == T 且 Xd 不在 S 中且 Xd != 0 | 可见 | 自己创建，被未提交事务删除（删除还没生效） |
| R5 | Xc 不在 S 中 | 不可见 | 创建者未提交（防脏读） |
| R6 | Xc 在 S 中 且 Xd == 0 | 可见 | 创建者已提交，未被删除 |
| R7 | Xc 在 S 中 且 Xd == T | 不可见 | 创建者已提交，自己删除了 |
| R8 | Xc 在 S 中 且 Xd 在 S 中 | 不可见 | 创建者已提交，删除者已提交 |
| R9 | Xc 在 S 中 且 Xd != 0 且 Xd 不在 S 中 | 可见 | 创建者已提交，删除者未提交（删除还没生效） |

### 9.2 五个具体例子

#### 例子1：正常读取

```
场景: T3 读取 T1 创建并提交的数据

事务时间线:
  T1: ──创建版本V──提交──
  T2: ──────────活跃────  (T2 还没提交)
  T3: ────────开始──读V──  (T3 的快照 = {T1})

版本 V: xmin=T1, xmax=0

判断:
  Xc(T1) != T3 → 不是自己创建的
  T1 在快照中 → 创建者已提交 ✓
  Xd=0 → 未被删除 ✓
  → 可见 ✓

结果: T3 看到 V
```

#### 例子2：防止脏读

```
场景: T2 试图读取 T1 未提交的数据

事务时间线:
  T1: ──创建版本V──（还没提交）
  T2: ────────开始──读V──  (T2 的快照 = {}，T1 还没提交)

版本 V: xmin=T1, xmax=0

判断:
  Xc(T1) != T2 → 不是自己创建的
  T1 不在快照中 → 创建者未提交 ✗
  → 不可见 ✗

结果: T2 看不到 V，防止了脏读！
```

#### 例子3：读不阻塞写

```
场景: T1 读旧版本，T2 创建新版本，T1 不受影响

事务时间线:
  T0: ──创建V1(xmin=T0)──提交──
  T1: ────────────开始──读──  (T1 快照 = {T0})
  T2: ────────────────写──提交──
  
  T2 写操作:
    V1.xmax = T2  (标记旧版本被删除)
    V2.xmin = T2  (创建新版本)

T1 读时的判断:
  V1: xmin=T0, xmax=T2
    Xc(T0) != T1
    T0 在快照中 ✓
    Xd(T2) != 0, T2 != T1
    T2 不在快照中（T2 还没提交）→ 删除未生效
    → V1 可见 ✓
  
  V2: xmin=T2, xmax=0
    Xc(T2) != T1
    T2 不在快照中 → 创建者未提交
    → V2 不可见 ✗

结果: T1 看到 V1（旧版本），看不到 V2（新版本）
      读不阻塞写！
```

#### 例子4：提交后可见

```
场景: T2 提交后，新事务能看到新版本

事务时间线:
  T0: ──创建V1──提交──
  T2: ──写V2──提交──
  T3: ────────开始──读──  (T3 快照 = {T0, T2})

V1: xmin=T0, xmax=T2
  T0 在快照中 ✓
  T2 在快照中 → 删除者已提交
  → V1 不可见 ✗

V2: xmin=T2, xmax=0
  T2 在快照中 ✓
  xmax=0 → 未删除
  → V2 可见 ✓

结果: T3 看到 V2（新版本），看不到 V1（旧版本）
```

#### 例子5：自己创建自己删除

```
场景: T1 创建一行然后删除

事务时间线:
  T1: ──创建V──删除V──

V: xmin=T1, xmax=T1

判断 (T1 自己看):
  Xc(T1) == T1 → 自己创建的
  Xd(T1) == T1 → 自己删除的
  → 不可见 ✗

结果: T1 看不到自己创建又删除的行
```

### 可见性判断汇总表

| 例子 | xmin | xmax | 快照 | 当前事务 | 可见？ | 原因 |
|---|---|---|---|---|---|---|
| 正常读取 | T1 | 0 | {T1} | T3 | ✓ | 创建者已提交，未删除 |
| 防脏读 | T1 | 0 | {} | T2 | ✗ | 创建者未提交 |
| 读不阻塞写 | T0 | T2 | {T0} | T1 | ✓ | 删除者未提交，删除未生效 |
| 提交后可见 | T2 | 0 | {T0,T2} | T3 | ✓ | 创建者已提交，未删除 |
| 自创自删 | T1 | T1 | {} | T1 | ✗ | 自己创建又自己删除 |

---

## 10. 与真实数据库对比

### minidb vs PostgreSQL

| 特性 | minidb | PostgreSQL |
|---|---|---|
| MVCC 实现 | 简单版本链 | Undo log 在单独表 |
| 版本存储 | 行内多版本 | Undo log (8.x+), 旧版本在 undo 表空间 |
| xmin/xmax | 32位事务ID | 32位事务ID + 命令ID |
| 快照表示 | 已提交事务列表 | xmin/xmax + 运行中事务列表 |
| 可见性判断 | 线性搜索快照 | 位图+哈希，高效 |
| VACUUM | 无（教学简化） | 自动 VACUUM 清理旧版本 |
| 事务ID回卷 | 不处理 | 32位回卷，需要冻结 |
| 隔离级别 | 快照隔离 | RC(默认), RR, Serializable |
| 死锁检测 | 简化（无） | 等待图 + 超时 |
| 锁管理器 | 哈希表，单线程 | 多级锁表，支持多种锁 |

### minidb vs MySQL InnoDB

| 特性 | minidb | MySQL InnoDB |
|---|---|---|
| MVCC 实现 | xmin/xmax | 隐藏列 DB_TRX_ID, DB_ROLL_PTR |
| 回滚段 | 无 | undo log 链 |
| 聚簇索引 | 无 | 有（按主键组织） |
| Next-Key Lock | 无 | 有（防止幻读） |
| 隔离级别 | 快照隔离 | RR(默认), RC, Serializable |
| 死锁检测 | 无 | 等待图检测 |
| 间隙锁 | 无 | 有 |

### 版本链对比图

**minidb 的版本链**：

```
  页面内:
  ┌─────────────────────────┐
  │ Slot 0: data=100        │
  │   xmin=T1, xmax=T2     │  ← 旧版本（被T2删除）
  ├─────────────────────────┤
  │ Slot 1: data=200        │
  │   xmin=T2, xmax=T3     │  ← 中间版本（被T3删除）
  ├─────────────────────────┤
  │ Slot 2: data=300        │
  │   xmin=T3, xmax=0       │  ← 当前版本
  └─────────────────────────┘
  
  所有版本都在同一页面，通过 xmax 链接
  查找时遍历所有版本，判断可见性
```

**PostgreSQL 的版本链**：

```
  页面内:
  ┌─────────────────────────┐
  │ Tuple 1: data=300       │
  │   xmin=T3, xmax=0       │  ← 当前版本
  │   t_ctid → 自己          │
  └─────────────────────────┘
  
  旧版本通过 t_ctid 指向新版本:
  Tuple 0 (旧) → t_ctid → Tuple 1 (新)
  
  VACUUM 清理: 删除对所有活跃事务都不可见的旧版本
```

**MySQL InnoDB 的版本链**：

```
  聚簇索引记录:
  ┌─────────────────────────────────┐
  │ data=300                        │
  │ DB_TRX_ID=T3                    │  ← 创建事务
  │ DB_ROLL_PTR → undo log          │  ← 指向 undo
  └─────────────────────────────────┘
  
  Undo log (回滚段):
  ┌─────────────────────────────────┐
  │ undo record: data=200, trx=T2  │
  │ DB_ROLL_PTR → 更早的 undo       │
  ├─────────────────────────────────┤
  │ undo record: data=100, trx=T1  │
  │ DB_ROLL_PTR → NULL              │
  └─────────────────────────────────┘
  
  旧版本在 undo log 中，不在数据页
  查找时沿 DB_ROLL_PTR 链遍历 undo log
```

### 为什么 minidb 简化了这些？

| 简化项 | 原因 | 真实数据库为什么要做 |
|---|---|---|
| 无 VACUUM | 教学简化 | 旧版本会占空间，需要清理 |
| 无事务ID回卷 | 教学简化 | 32位ID会用完，需要冻结 |
| 无间隙锁 | 教学简化 | 防止幻读需要锁住"间隙" |
| 单线程锁管理器 | 教学简化 | 多核需要并发安全的锁表 |
| 线性搜索快照 | 教学简化 | 大量事务需要高效查找 |
| 无死锁检测 | 教学简化 | 并发锁需要检测死锁 |

---

## 11. 习题

### 基础题

**习题1**：用自己的话解释 ACID 四个性质，每个给一个现实生活的例子。

**习题2**：以下操作序列会产生什么并发问题？

```
T1: read(X)     // 读到 X=100
T2: write(X=200) // 修改 X
T2: commit()
T1: read(X)     // 读到 X=200
T1: commit()
```

**习题3**：2PL 中，为什么"两阶段"（先全加锁再全解锁）能保证可串行化？提示：思考如果违反两阶段会怎样。

**习题4**：画出以下场景的时序图，判断是否死锁：

```
T1: lock_X(A), lock_X(B)
T2: lock_X(B), lock_X(A)
```

### 进阶题

**习题5**：MVCC 中，如果从不清理旧版本，会有什么问题？提示：表膨胀 / VACUUM。

**习题6**：快照隔离下，写偏斜为什么可能发生？SSI（可串行化快照隔离）如何防止？提示：思考"读-写依赖"。

**习题7**：如果事务 T1 读到 T2 未提交的数据（脏读），可能产生什么问题？举一个具体的财务例子。

**习题8**：MySQL InnoDB 的 MVCC 和 PostgreSQL 的 MVCC 实现有什么区别？提示：版本存储位置、回滚方式。

### 代码题

**习题9**：阅读 `mvcc_visible` 函数，回答：如果 `h->xmin == current_txn` 且 `h->xmax == 0`，返回什么？为什么？

**习题10**：阅读 `has_conflict` 函数，解释为什么 `e->txn == txn` 时要 `continue`（跳过）。

**习题11**：阅读 `txn_get_snapshot` 函数，如果当前有 300 个已提交事务，但 `SNAPSHOT_CAPACITY=256`，会发生什么？如何改进？

**习题12**：以下代码模拟了一个转账场景，找出问题并修复：

```c
txn_id_t t = txn_begin(mgr);
// 转账：Alice 给 Bob 转 50
update_balance(db, 1, -50);  // Alice 减 50
// 如果这里崩溃了怎么办？
update_balance(db, 2, +50);  // Bob 加 50
txn_commit(mgr, t);
```

提示：思考 WAL 的作用，以及 crash recovery 会怎么处理。

### 思考题

**习题13**：为什么几乎所有现代数据库都选择 MVCC 而不是纯 2PL？什么场景下 2PL 可能更好？

**习题14**：minidb 的 `lm_has_deadlock` 直接返回 false。如果要实现真正的死锁检测，需要哪些数据结构？画出等待图的数据结构设计。

**习题15**：对比以下两个场景的吞吐量，解释为什么：

```
场景A: 1000 个只读事务并发
场景B: 500 个只读 + 500 个读写事务并发

分别用 2PL 和 MVCC，哪个吞吐量更高？为什么？
```

### 习题参考答案

**习题2 答案**：不可重复读。T1 两次读 X 结果不同（100 和 200），因为 T2 在中间修改并提交了。注意这不是脏读，因为 T2 已经提交了。

**习题9 答案**：返回 `true`（可见）。因为 `h->xmin == current_txn` 表示这个版本是当前事务自己创建的，`h->xmax == 0` 表示未被删除，所以自己能看到自己创建的、未删除的版本。

**习题10 答案**：同一事务持有的锁不算冲突。一个事务可以自己持有 S 锁再请求 X 锁（锁升级），这不是冲突。如果不算跳过，会导致事务和自己死锁。

**习题11 答案**：快照只记录前 256 个已提交事务，后面的 44 个不会被记录。这会导致这 44 个事务的修改对当前事务不可见（即使它们已提交），是 bug。改进方法：用位图代替数组，或动态扩容，或像 PostgreSQL 那样记录 xmin/xmax 范围而非完整列表。

---

## 附录A：minidb 事务 API 速查

```c
// 创建/销毁事务管理器
transaction_manager_t *txn_mgr_create(wal_t *wal);
void txn_mgr_destroy(transaction_manager_t *mgr);

// 事务生命周期
txn_id_t txn_begin(transaction_manager_t *mgr);        // 开始事务
bool     txn_commit(transaction_manager_t *mgr, txn_id_t id);  // 提交
bool     txn_abort(transaction_manager_t *mgr, txn_id_t id);   // 回滚

// 事务状态查询
txn_status_t txn_status(transaction_manager_t *mgr, txn_id_t id);
bool txn_is_active(transaction_manager_t *mgr, txn_id_t id);
bool txn_is_committed(transaction_manager_t *mgr, txn_id_t id);

// 快照
void txn_get_snapshot(transaction_manager_t *mgr, txn_snapshot_t *snap);
bool txn_snapshot_contains(const txn_snapshot_t *snap, txn_id_t id);

// 锁管理器
lock_manager_t *lm_create(void);
void lm_destroy(lock_manager_t *lm);
bool lm_lock(lock_manager_t *lm, txn_id_t txn, rid_t rid, lock_mode_t mode);
bool lm_unlock(lock_manager_t *lm, txn_id_t txn, rid_t rid);
void lm_unlock_all(lock_manager_t *lm, txn_id_t txn);
bool lm_has_deadlock(lock_manager_t *lm);
int  lm_num_locks(lock_manager_t *lm);

// MVCC
void mvcc_init(mvcc_header_t *h, txn_id_t xmin);
bool mvcc_visible(const mvcc_header_t *h, const txn_snapshot_t *snap, txn_id_t cur);
bool mvcc_deleted(const mvcc_header_t *h, const txn_snapshot_t *snap, txn_id_t cur);
```

## 附录B：使用示例

```c
// 示例1: 简单事务
transaction_manager_t *mgr = txn_mgr_create(wal);

txn_id_t t = txn_begin(mgr);
// ... 执行操作 ...
if (success) {
    txn_commit(mgr, t);   // 提交
} else {
    txn_abort(mgr, t);    // 回滚
}

// 示例2: MVCC 读取
txn_id_t reader = txn_begin(mgr);
txn_snapshot_t snap;
txn_get_snapshot(mgr, &snap);

mvcc_header_t h = { .xmin = some_txn, .xmax = 0 };
if (mvcc_visible(&h, &snap, reader)) {
    // 这个版本对 reader 可见
}
txn_commit(mgr, reader);

// 示例3: 加锁
lock_manager_t *lm = lm_create();
rid_t rid = { .page_id = 1, .slot_id = 0 };

if (lm_lock(lm, t, rid, LOCK_EXCLUSIVE)) {
    // 加锁成功，可以修改
    // ... write ...
} else {
    // 加锁失败（有冲突）
}
lm_unlock_all(lm, t);  // 事务结束释放所有锁
```

## 附录C：设计决策汇总

| 决策 | 选择 | 理由 |
|---|---|---|
| 并发控制 | 2PL + MVCC 都实现 | 对比教学 |
| MVCC 隔离级别 | 快照隔离 | PostgreSQL 默认 |
| 锁管理器 | 哈希表 + 冲突检测 | 单线程简化 |
| 死锁检测 | 简化（返回 false） | 单线程无真正死锁 |
| 事务数 | 固定 1024 | 教学简化 |
| 快照容量 | 固定 256 | 教学简化 |
| 锁表大小 | 固定 2048 | 教学简化 |
| 版本清理 | 无 VACUUM | 教学简化 |
| 锁释放时机 | 事务结束（SS2PL） | 保证严格性 |

## 附录D：关键公式与不变量

### MVCC 可见性不变量

```
对于任意版本 V (xmin=Xc, xmax=Xd) 和事务 T (快照 S):
  
  visible(V, S, T) ⟺
    (Xc == T ∧ Xd != T ∧ ¬(Xd != 0 ∧ Xd ∈ S))
    ∨
    (Xc != T ∧ Xc ∈ S ∧ ¬(Xd != 0 ∧ (Xd == T ∨ Xd ∈ S)))
```

### 2PL 可串行化定理

```
如果所有事务都遵守两阶段锁协议:
  1. 增长阶段只加锁
  2. 收缩阶段只解锁
  3. 加锁和解锁严格分两个阶段

则这些事务的任何并发执行都是可串行化的。
(即: 等价于某种串行执行顺序)
```

### 快照隔离的一致性

```
快照隔离保证:
  1. 读一致性: 同一事务内多次读同一数据，结果相同
  2. 无脏读: 只看到已提交的数据
  3. 读不阻塞写: 读操作不阻塞写操作

快照隔离不保证:
  1. 可串行化: 写偏斜可能发生
  2. 无幻读: 新插入的行可能被看到
```

### 锁兼容矩阵形式化

```
compatible(请求模式 m1, 持有模式 m2) ⟺
  ¬(m1 == X ∨ m2 == X)
  
等价于:
  m1 == S ∧ m2 == S  (只有 S-S 兼容)
```

---

## 附录E：术语表

| 术语 | 英文 | 解释 |
|---|---|---|
| 事务 | Transaction | 一组原子性操作 |
| ACID | ACID | 原子性、一致性、隔离性、持久性 |
| 脏读 | Dirty Read | 读到未提交的数据 |
| 不可重复读 | Non-repeatable Read | 同行两次读结果不同 |
| 幻读 | Phantom Read | 同查询两次行数不同 |
| 丢失更新 | Lost Update | 后提交覆盖先提交 |
| 写偏斜 | Write Skew | 各改不相交子集，违反约束 |
| 共享锁 | Shared Lock (S) | 读锁，可共享 |
| 排他锁 | Exclusive Lock (X) | 写锁，独占 |
| 两阶段锁 | 2PL | 先全加锁再全解锁 |
| 死锁 | Deadlock | 循环等待 |
| 多版本并发控制 | MVCC | 保留多版本实现并发 |
| 快照隔离 | Snapshot Isolation | 事务开始时取快照 |
| 隔离级别 | Isolation Level | 并发隔离的强度 |
| 可串行化 | Serializable | 等同串行执行 |
| VACUUM | VACUUM | 清理旧版本 |
| WAL | Write-Ahead Log | 预写日志 |
| xmin | xmin | 创建版本的事务ID |
| xmax | xmax | 删除版本的事务ID |
| 等待图 | Wait-For Graph | 检测死锁的有向图 |
| SSI | Serializable SI | 可串行化快照隔离 |

---

## 附录F：常见问题 FAQ

**Q1: minidb 为什么同时实现 2PL 和 MVCC？**

A: 为了教学对比。2PL 是经典方法，MVCC 是现代方法，两者对比能帮助理解为什么现代数据库都选 MVCC。

**Q2: minidb 的 MVCC 为什么不做 VACUUM？**

A: VACUUM 涉及复杂的版本清理逻辑（判断哪些版本对所有活跃事务都不可见），教学项目简化了。生产环境必须做，否则表会膨胀。

**Q3: 快照隔离和 Repeatable Read 什么关系？**

A: 快照隔离是 Repeatable Read 的一种实现方式。SQL 标准的 RR 定义比较模糊，快照隔离比标准 RR 稍强（防止了丢失更新），但比 Serializable 弱（不能防止写偏斜）。

**Q4: 为什么 minidb 的锁管理器不阻塞等待？**

A: `lm_lock` 冲突时直接返回 false，而不是阻塞。这是因为 minidb 是单线程的，没有真正的并发等待。生产数据库会让事务睡眠等待，直到锁释放。

**Q5: xmin 和 xmax 为什么用事务ID而不是时间戳？**

A: 事务ID是单调递增的，比时间戳更可靠（系统时钟可能回拨）。而且事务ID可以紧凑地存储在版本头部（4字节），时间戳需要8字节。

**Q6: 死锁为什么只发生在 2PL，不在 MVCC？**

A: MVCC 的读操作不获取锁（用快照），所以不会形成读-写等待环。但 MVCC 的写-写仍然可能冲突（两个事务同时修改同一行），通常用 First-Updater-Wins 规则处理，不会死锁。

---

## 附录G：扩展阅读

| 主题 | 推荐资料 |
|---|---|
| 2PL 理论 | 《Transaction Processing》Jim Gray |
| MVCC 实现 | PostgreSQL 文档 "MVCC" 章节 |
| 快照隔离 | Berenson et al. "A Critique of ANSI SQL Isolation Levels" |
| SSI | Cahill et al. "Serializable Isolation for Snapshot Transactions" |
| 死锁检测 | 《Database System Concepts》Silberschatz, 第16章 |
| MySQL InnoDB | MySQL 文档 "InnoDB Multi-Versioning" |
| Oracle MVCC | Oracle 文档 "Data Concurrency and Consistency" |

---

## 附录H：本章关键代码文件

| 文件 | 行数 | 功能 |
|---|---|---|
| `phase1/src/txn/transaction.h` | 48 | 事务管理器接口 |
| `phase1/src/txn/transaction.c` | 81 | 事务管理器实现 |
| `phase1/src/txn/lock_manager.h` | 25 | 锁管理器接口 |
| `phase1/src/txn/lock_manager.c` | 114 | 锁管理器实现（2PL） |
| `phase1/src/txn/mvcc.h` | 26 | MVCC 接口 |
| `phase1/src/txn/mvcc.c` | 38 | MVCC 可见性判断 |
| **合计** | **332** | |

---

## 附录I：时序图汇总

### I.1 转账原子性

```
  客户端              数据库                WAL
     |                  |                   |
     |--- BEGIN ------->|                   |
     |                  |--- BEGIN ------->|
     |                  |<-- lsn=100 ------|
     |                  |                   |
     |--- 扣 Alice ---->|                   |
     |                  |--- UPDATE ------>|
     |                  |<-- lsn=101 ------|
     |                  |                   |
     |--- 加 Bob ------>|                   |
     |                  |--- UPDATE ------>|
     |                  |<-- lsn=102 ------|
     |                  |                   |
     |--- COMMIT ------>|                   |
     |                  |--- COMMIT ------>|
     |                  |<-- lsn=103 ------|
     |                  |                   |
     |                  |  💥 崩溃          |
     |                  |                   |
     |                  |  重启             |
     |                  |  重放 lsn 100-103 |
     |                  |  事务完整恢复 ✓   |
```

### I.2 2PL 死锁

```
  T1                    T2                   锁管理器
     |                    |                      |
     |--- lock_X(A) ----->|                      |  T1 持有 A
     |<-- OK -------------|                      |
     |                    |--- lock_X(B) ------->|  T2 持有 B
     |                    |<-- OK ---------------|
     |                    |                      |
     |--- lock_X(B) ----->|                      |  T1 等 B
     |    ⏳               |--- lock_X(A) ------->|  T2 等 A
     |    ⏳               |    ⏳                 |
     |    ⏳               |    ⏳                 |
     |    ⏳               |    ⏳   💀 死锁       |
```

### I.3 MVCC 读不阻塞写

```
  T1 (reader)          T2 (writer)          版本链
     |                    |                    |
     |--- 取快照 S={T0} ->|                    |  A=100 (xmin=T0, xmax=0)
     |                    |                    |
     |--- read(A) ------->|                    |
     |<-- 100 ------------|                    |  T1 看到 A=100
     |                    |                    |
     |                    |--- write(A)=200 -->|
     |                    |                    |  A=100 (xmax=T2) ← 旧版本标记删除
     |                    |                    |  A=200 (xmin=T2) ← 新版本
     |                    |                    |
     |                    |--- commit ------->|
     |                    |                    |
     |--- read(A) ------->|                    |
     |<-- 100 ------------|                    |  T1 仍看到 A=100（快照没变）
     |                    |                    |
     |--- commit ------->|                    |
     |                    |                    |
     |                    |    T3 开始          |
     |                    |    快照 S={T0,T2}   |
     |                    |    read(A) = 200    |  T3 看到 A=200
```

### I.4 快照隔离防止不可重复读

```
  T1 (长事务)          T2 (短事务)          数据库
     |                    |                    |
     |--- 取快照 S={} --->|                    |  X=100
     |                    |                    |
     |--- read(X) ------->|                    |
     |<-- 100 ------------|                    |  第一次读
     |                    |                    |
     |                    |--- write(X=200) -->|
     |                    |--- commit ------->|  X=200 (已提交)
     |                    |                    |
     |--- read(X) ------->|                    |
     |<-- 100 ------------|                    |  第二次读，还是100！
     |                    |                    |  (快照没变，看不到T2的修改)
     |                    |                    |
     |  两次读结果相同 ✓  |                    |  ← 不可重复读被防止
```

### I.5 快照隔离不能防止写偏斜

```
  T1                    T2                   数据库
     |                    |                    |  A=on, B=on
     |                    |                    |  约束: A和B至少一个on
     |--- 取快照 S1 ------>|                    |
     |                    |--- 取快照 S2 ----->|
     |                    |                    |
     |--- read(A,B) ----->|                    |
     |<-- A=on, B=on -----|                    |
     |                    |--- read(A,B) ---->|
     |                    |<-- A=on, B=on ----|
     |                    |                    |
     |  (都认为对方是on)  |  (都认为对方是on) |
     |                    |                    |
     |--- write(A=off) -->|                    |
     |--- commit ------->|                    |  A=off, B=on
     |                    |                    |
     |                    |--- write(B=off) ->|
     |                    |--- commit ------->|  A=off, B=off 💥
     |                    |                    |
     |                    |    约束被违反！    |
     |                    |    没人值班了      |
```

---

## 附录J：本章总结

### 核心概念回顾

1. **事务**：保证 ACID 的操作单元
2. **ACID**：原子性、一致性、隔离性、持久性
3. **并发问题**：脏读、不可重复读、幻读、丢失更新、写偏斜
4. **2PL**：用锁实现隔离，读阻塞写，可能死锁
5. **MVCC**：用多版本实现隔离，读不阻塞写，无死锁
6. **快照隔离**：事务开始取快照，对应 Repeatable Read
7. **隔离级别**：RU < RC < RR < Serializable，越强越安全但越慢
8. **死锁**：循环等待，用等待图检测或超时检测

### 一句话总结

> **2PL 是"悲观"的——先锁住再操作，怕冲突；MVCC 是"乐观"的——先操作再说，用版本隔离冲突。现代数据库偏爱乐观。**

---

上一章：[章5 WAL 与崩溃恢复](05-wal-recovery.md) | 下一章：[章7 SQL 解析](07-sql-parser.md)
