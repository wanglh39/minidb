# 章5：WAL 与崩溃恢复

> 断电后数据库怎么恢复？WAL（预写日志）是答案。所有修改先写日志再改数据，崩溃后重做日志。

## 核心原则：先写日志，再改数据

```
修改数据页时的顺序：
  1. 把"要做什么修改"写入 WAL 日志
  2. fsync 日志到磁盘          ← 必须真正落盘！
  3. 修改 Buffer Pool 中的数据页  ← 可以延迟刷盘
```

**为什么这样安全？**

- 日志已落盘 → 即使数据页没刷盘，日志里有记录
- 崩溃后读日志 → 重做（redo）所有修改 → 数据恢复

## ARIES 算法

```
崩溃
  ↓
1. Analysis  — 读日志，确定哪些事务需要恢复
2. Redo      — 重做所有已提交事务的修改    ← 本章实现
3. Undo      — 回滚所有未提交事务的修改    ← 章6 实现
```

## 日志记录格式

```
┌────────┬────────┬──────┬──────────────────────┐
│ LSN    │ txn_id │ type │ data (变长)          │
│ 8B     │ 4B     │ 1B   │                      │
└────────┴────────┴──────┴──────────────────────┘
```

| type | 说明 | data |
|---|---|---|
| BEGIN | 事务开始 | 无 |
| COMMIT | 事务提交 | 无 |
| UPDATE | 修改数据 | page_id, offset, length, old_data, new_data |
| ABORT | 事务中止 | 无 |
| CHECKPOINT | 检查点 | max_lsn |

**LSN（Log Sequence Number）**：日志序号，用文件偏移量作为 LSN，单调递增。

## UPDATE 记录

```
┌────────┬────────┬────────┬──────────┬──────────┐
│ page_id│ offset │ length │ old_data │ new_data │
│ 4B     │ 2B     │ 2B     │ len B    │ len B    │
└────────┴────────┴────────┴──────────┴──────────┘
```

- **new_data**：用于 redo（重做修改）
- **old_data**：用于 undo（回滚修改，章6 实现）

## Redo 流程

```c
void recovery_redo(buffer_pool_t *bp, wal_t *wal) {
    // 第一遍：收集已提交的事务
    bool committed[1024] = {false};
    for each record in WAL:
        if record.type == COMMIT:
            committed[record.txn_id] = true;

    // 第二遍：重做已提交事务的 UPDATE
    for each record in WAL:
        if record.type == UPDATE && committed[record.txn_id]:
            page = bp_fetch_page(bp, record.page_id);
            memcpy(page->data + record.offset, record.new_data, record.length);
            bp_unpin_page(bp, record.page_id, true);  // dirty
}
```

**为什么只 redo 已提交事务？** 未提交事务的修改不应该可见，需要 undo（章6）。

## 崩溃恢复示例

```
正常流程：
  1. wal_begin(txn=1)
  2. 修改 page 0 的 offset 20：old=[0,0,0,0] new=[1,2,3,4]
     wal_update(txn=1, page=0, offset=20, ...)
  3. wal_commit(txn=1)
  4. 修改 Buffer Pool 中的 page 0（但没刷盘）
  5. 💥 断电

恢复流程：
  1. 重新打开数据库
  2. recovery_redo(bp, wal)
  3. 读 WAL → 发现 txn=1 已提交
  4. 重做 UPDATE → 把 [1,2,3,4] 写回 page 0 的 offset 20
  5. 数据恢复！
```

## fsync 的重要性

```c
lsn_t wal_commit(wal_t *wal, txn_id_t txn_id) {
    // 写日志记录
    ...
    fflush(wal->fp);  // 刷到 OS
    // 真实数据库还要 fsync(fileno(fp)) 确保写到磁盘
    return lsn;
}
```

!!! warning "fflush ≠ fsync"
    `fflush` 只把数据从用户空间缓冲刷到 OS 内核缓冲。
    `fsync` 才真正把数据写到磁盘。
    如果只 fflush 没 fsync，断电时 OS 缓冲的日志也会丢失。
    本章用 fflush 简化，真实数据库必须 fsync。

## Checkpoint

定期做 checkpoint 缩短恢复时间：

```
1. 把所有 dirty 页刷到磁盘
2. 在 WAL 写 CHECKPOINT 记录，记录当前 LSN
```

恢复时从最后一个 checkpoint 开始重做，不用从头重做整个 WAL。

## 设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| LSN | 文件偏移量 | 天然单调递增，无需额外维护 |
| 日志格式 | 大端 + 定长头 | 跨平台一致，解析简单 |
| redo 范围 | 只重做已提交事务 | 未提交事务需 undo（章6） |
| fsync | fflush 简化 | 教学清晰，文档说明真实需求 |
| checkpoint | 只记录 LSN | 简化，真实DB还刷脏页 |

## 习题

1. 如果 WAL 写了一半就断电，恢复时会怎样？如何检测不完整的记录？
2. redo 操作为什么必须是幂等的（重复执行结果一样）？
3. 如果不先写日志直接改数据页，什么场景下会丢数据？
4. checkpoint 时: 为什么要先刷脏页再写 checkpoint 记录？反过来行不行？
5. 组提交（group commit）是什么？为什么能提高性能？

---

上一章：[章4 堆表存储](04-heap.md) | 下一章：[章6 事务与并发控制](06-txn-mvcc.md)