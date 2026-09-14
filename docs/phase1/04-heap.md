# 章4：堆表存储

> 表和索引分开存储。堆表存数据，索引指向数据。本章实现完整的表存储层。

## 表与索引分离

```
堆表（Heap）                    B+Tree 索引
┌─────────────────┐            ┌─────────────────┐
│ Page 0          │            │ key=1 → RID(0,0) │
│  tuple(Alice)   │  ←── 回表  │ key=2 → RID(0,1) │
│  tuple(Bob)     │            │ key=3 → RID(1,0) │
│ Page 1          │            └─────────────────┘
│  tuple(Carol)   │
└─────────────────┘
```

**为什么分开？**

1. 一个表可有多个索引，数据存堆表只一份
2. 索引只存 `键→RID`，体积小，驻留内存概率高
3. 堆表按插入顺序存储，顺序扫描高效
4. 更新非索引列时，索引不需要维护

## Tuple 物理布局

```
┌──────────┬──────────────────────────┐
│ NULL bm  │ col0 │ col1 │ col2 │ ... │
└──────────┴──────────────────────────┘
```

- **NULL bitmap**：每列 1 bit，1=NULL，0=非NULL
- **固定列**：按 schema 类型依次排列

```c
schema_t *s = make_user_schema();
// cols: id(INT32), age(INT32), score(FLOAT)
// null_bm: 1 byte (3 bits used)
// data: 4 + 4 + 4 = 12 bytes
// total: 13 bytes per tuple
```

## Schema

```c
schema_t schema;
schema_init(&schema, "users");
schema_add_col(&schema, "id", COL_INT32, false);    // 主键，非空
schema_add_col(&schema, "age", COL_INT32, true);     // 可空
schema_add_col(&schema, "score", COL_FLOAT, true);   // 可空
```

## Heap（堆表）

堆表复用章1的 slotted page + 章2的 Buffer Pool：

```c
heap_t *heap = heap_create(bp, &schema);

// 插入
tuple_t *t = tuple_create(&schema);
tuple_set_int32(t, 0, 42);
rid_t rid = heap_insert(heap, t);  // 返回 (page_id, slot_id)

// 按 RID 取
tuple_t *fetched = heap_fetch(heap, rid);

// 顺序扫描（全表）
heap_scan_t *scan = heap_scan_open(heap);
while (heap_scan_next(scan, &rid, t)) {
    printf("id=%d\n", tuple_get_int32(t, 0));
}
heap_scan_close(scan);
```

**页链表**：堆表的页通过 `next_page_id` 连接，顺序扫描沿链表遍历。

## Table（表 + 索引协同）

```c
table_t *table = table_create(bp, &schema, 0);  // pk_col=0

// 插入：自动维护索引
table_insert(table, t);  // heap_insert + btree_insert

// 按主键查找：索引扫描 → 回表
tuple_t *found = table_find(table, 42);
// 1. btree_find(42) → RID
// 2. heap_fetch(RID) → tuple

// 范围查询
table_scan_t *scan = table_scan_open(table, 10, 50);
while (table_scan_next(scan, t)) { ... }
```

## 回表（Index Lookup → Heap Fetch）

```
table_find(pk=42):
  1. btree_find(42) → RID(3, 7)     // 索引查找
  2. heap_fetch(RID(3, 7)) → tuple   // 回表取完整数据
```

**为什么需要回表？** 索引只存键和 RID，不存完整 tuple。找到 RID 后还要去堆表取数据。

**覆盖索引**：如果查询的列都在索引中，不需要回表（章8 优化器会讲）。

## 顺序扫描 vs 索引扫描

| 方式 | 适用场景 | 代价 |
|---|---|---|
| 顺序扫描 | 无索引、或扫描大部分数据 | O(n)，读所有页 |
| 索引扫描 | 有索引、选择性高 | O(log n + k)，k=结果数 |

选择率 < 10% 时索引扫描通常更快，否则顺序扫描更优（章8 代价模型）。

## 设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 字段类型 | 固定长度（int32/int64/float） | 教学简化，文档说明变长扩展 |
| NULL 处理 | bitmap | 紧凑存储 |
| 堆表页组织 | 链表（next_page） | 顺序扫描 |
| 索引 | B+Tree（章3） | 复用已有实现 |
| 主键 | 单列 int32 | 教学简化 |

## 习题

1. 如果要支持 VARCHAR（变长字符串），tuple 布局怎么调整？
2. 一个表有 3 个索引，插入一个 tuple 需要几次写操作？
3. 覆盖索引为什么不需要回表？如何判断查询是否可以用覆盖索引？
4. 堆表的页链表是单向的，如何支持反向扫描？
5. 如果 tuple 太大放不下一页，怎么处理？（提示：TOAST / 溢出页）

---

上一章：[章3 B+Tree 索引](03-btree.md) | 下一章：[章5 WAL 与崩溃恢复](05-wal-recovery.md)