# 章6：事务与并发控制

> 多个事务同时读写同一数据时，如何保证 ACID？本章实现 2PL 和 MVCC 两种方案。

## ACID

| 性质 | 含义 | 保证机制 |
|---|---|---|
| **A**tomicity | 全做或全不做 | WAL + undo |
| **C**onsistency | 数据不矛盾 | 由 A/I/D 共同保证 |
| **I**solation | 事务互不干扰 | 2PL 或 MVCC |
| **D**urability | 提交后不丢 | WAL redo（章5） |

## 两种并发控制

### 2PL（两阶段锁）

```
事务 T1:                    事务 T2:
  lock_X(A)                   lock_S(A) ← 等待 T1 释放
  write(A)                    
  unlock(A)                   read(A) ← 现在 OK
```

- **S 锁（共享）**：读锁，多个事务可同时持有
- **X 锁（排他）**：写锁，独占
- **两阶段**：增长阶段（只加锁）→ 收缩阶段（只解锁）
- **问题**：死锁（T1 锁 A 等 B，T2 锁 B 等 A）

### MVCC（多版本并发控制）

```
事务 T1 (snapshot={1}):      事务 T2:
  read(A)                      write(A)
  看到 A 版本1                 创建 A 版本2
  → 可见 ✓                    → A 版本1.xmax = T2
  读不阻塞写！✓               → A 版本2.xmin = T2
```

- 每行数据有多个版本
- 每个事务看到一个一致的快照
- **读不阻塞写，写不阻塞读**
- PostgreSQL/MySQL InnoDB 都用 MVCC

## MVCC 版本可见性

每个 tuple 带 `mvcc_header_t`：

```c
typedef struct {
    txn_id_t xmin;  // 创建该版本的事务
    txn_id_t xmax;  // 删除该版本的事务（0 = 未删除）
} mvcc_header_t;
```

事务 T（快照 S）看到版本 V 的条件：

```
1. V.xmin == T → 自己创建的，可见（除非自己也删了）
2. V.xmin 不在 S 中 → 创建者未提交，不可见
3. V.xmax == T → 自己删除的，不可见
4. V.xmax 在 S 中 → 已被提交事务删除，不可见
5. 否则 → 可见
```

## 事务管理器

```c
txn_id_t t1 = txn_begin(mgr);       // 写 WAL BEGIN
// ... 操作 ...
txn_commit(mgr, t1);                // 写 WAL COMMIT
// 或
txn_abort(mgr, t1);                 // 写 WAL ABORT + undo
```

## 锁管理器

```c
lm_lock(lm, txn, rid, LOCK_SHARED);     // S 锁
lm_lock(lm, txn, rid, LOCK_EXCLUSIVE);  // X 锁
lm_unlock_all(lm, txn);                 // 事务结束释放所有锁
```

锁兼容矩阵：

| | S | X |
|---|---|---|
| **S** | ✓ | ✗ |
| **X** | ✗ | ✗ |

## MVCC 示例：读不阻塞写

```c
txn_id_t t1 = txn_begin(mgr);
txn_commit(mgr, t1);

txn_id_t reader = txn_begin(mgr);
txn_snapshot_t snap;
txn_get_snapshot(mgr, &snap);

txn_id_t writer = txn_begin(mgr);

// 旧版本：xmin=t1, xmax=0（未删除）
mvcc_header_t old = { .xmin = t1, .xmax = MVCC_NOT_DELETED };

// reader 看到旧版本
assert(mvcc_visible(&old, &snap, reader));  // ✓

// writer 删除旧版本，创建新版本
old.xmax = writer;
mvcc_header_t new = { .xmin = writer, .xmax = MVCC_NOT_DELETED };

// reader 仍然看到旧版本（读不阻塞写！）
assert(mvcc_visible(&old, &snap, reader));   // ✓
assert(!mvcc_visible(&new, &snap, reader));  // reader 看不到新版本

// writer 提交后，新事务能看到新版本
txn_commit(mgr, writer);
txn_snapshot_t snap2;
txn_get_snapshot(mgr, &snap2);
assert(!mvcc_visible(&old, &snap2, reader));  // 旧版本不可见了
assert(mvcc_visible(&new, &snap2, reader));   // 新版本可见
```

## 隔离级别

| 级别 | 防止 | 实现方式 |
|---|---|---|
| Read Uncommitted | 无 | 不加锁 |
| Read Committed | 脏读 | 每次读取最新快照 |
| **Repeatable Read** | 脏读、不可重复读 | **事务开始时取快照（本章）** |
| Serializable | 脏读、不可重复读、幻读 | 快照 + 谓词锁 |

本章实现 **快照隔离**（Repeatable Read）。

## 写偏斜（Write Skew）

快照隔离不能防止的异常：

```
T1: 读 A=on, B=on → 设置 A=off
T2: 读 A=on, B=on → 设置 B=off
结果: A=off, B=off（违反约束 "A 和 B 至少一个 on"）
```

可串行化需要额外机制（SSI - Serializable Snapshot Isolation）。

## 设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 并发控制 | 2PL + MVCC 都实现 | 对比教学 |
| MVCC 隔离级别 | 快照隔离 | PostgreSQL 默认 |
| 锁管理器 | 哈希表 + 冲突检测 | 单线程简化 |
| 死锁检测 | 简化 | 单线程无真正死锁 |
| 事务数 | 固定 1024 | 教学简化 |

## 习题

1. 2PL 中，为什么"两阶段"（先全加锁再全解锁）能保证可串行化？
2. MVCC 中，如果从不清理旧版本，会有什么问题？（提示：表膨胀 / VACUUM）
3. 快照隔离下，写偏斜为什么可能发生？SSI 如何防止？
4. 如果事务 T1 读到 T2 未提交的数据（脏读），可能产生什么问题？
5. MySQL InnoDB 的 MVCC 和 PostgreSQL 的 MVCC 实现有什么区别？

---

上一章：[章5 WAL 与崩溃恢复](05-wal-recovery.md) | 下一章：[章7 SQL 解析](07-sql-parser.md)