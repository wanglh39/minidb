# 章3：B+Tree 索引

> 索引是数据库加速查询的核心。本章从 BST 演进到 B+Tree，理解为什么数据库都选 B+Tree。

## 从 BST 到 B+Tree

### 二叉搜索树（BST）

```
        50
       /  \
      30   70
     / \   / \
    20 40 60 80
```

- 树高 O(log n)，100万数据 ≈ 20 层
- **问题**：每层一次磁盘 IO，20 次 IO 太慢

### B-Tree（多路搜索树）

让每个节点存多个键，降低树高：

```
        [30 | 50 | 70]
       /    |    |    \
    [10,20] [40] [60] [80,90]
```

- m 阶 B-Tree，树高 O(log_m n)
- 100万数据，m=200 → 3 层 → 3 次 IO
- **问题**：内节点也存数据，扇出降低

### B+Tree

```
        [30 | 50 | 70]          ← 内节点只存键
       /    |    |    \
    [10,20]→[30,40]→[50,60]→[70,80,90]  ← 叶子存数据，链表连接
```

三个关键优势：

1. **内节点只存键** → 扇出更大 → 树更矮 → IO 更少
2. **数据全在叶子** → 范围扫描只需遍历叶子链表
3. **每个节点一页** → 复用章1/章2 的 page + Buffer Pool

## 节点布局

一个 page = 一个 B+Tree 节点（`page_type = PAGE_TYPE_INDEX`）：

```
[0..19]   page header (章1)
[20]      node_type (0=internal, 1=leaf)
[21..22]  num_keys
[23..26]  parent_page_id
[27..30]  next_leaf_page_id (叶子链表)
[31..]    keys[32]
          内节点: children[33]  (每个 4B)
          叶子:   rids[32]     (每个 8B = page_id + slot_id)
```

**RID（Record ID）**：指向堆表中的 tuple 位置（章4 实现）。

```c
typedef struct {
    page_id_t page_id;
    slot_id_t slot_id;
} rid_t;
```

## 插入与分裂

插入是最核心的操作。当节点满了（> MAX_KEYS），需要分裂：

```
插入前（叶子满了，MAX_KEYS=4）：
[10 | 20 | 30 | 40 | 50]  ← 插入 25

插入后：
[10 | 20 | 25 | 30 | 40 | 50]  ← 超过容量

分裂：
左叶子: [10 | 20 | 25]
右叶子: [30 | 40 | 50]
        ↓
父节点: [30]  ← 右叶子第一个键上推
       /    \
  [10,20,25] [30,40,50]
```

如果父节点也满了，递归分裂。根节点分裂时创建新根，树高+1。

```c
bool btree_insert(btree_t *tree, btree_key_t key, rid_t rid) {
    // 1. 从根找到目标叶子
    // 2. 在叶子中找插入位置（二分查找）
    // 3. 如果键已存在，返回 false
    // 4. 插入键值
    // 5. 如果满了，分裂 → 中间键推到父节点
    // 6. 父节点也满了 → 递归分裂
}
```

## 查找

```c
bool btree_find(btree_t *tree, btree_key_t key, rid_t *rid) {
    // 1. 从根开始
    // 2. 内节点：二分查找选子节点
    // 3. 叶子节点：二分查找找键
}
```

内节点选子节点的规则：
- `child[0]` 指向 key < keys[0] 的子树
- `child[i]` 指向 keys[i-1] <= key < keys[i] 的子树
- `child[n]` 指向 key >= keys[n-1] 的子树

## 范围查询

B+Tree 相比 B-Tree 最大的优势：叶子节点用链表连接。

```c
btree_cursor_t *cur = btree_range_open(tree, 250, 450);
btree_key_t key;
rid_t rid;
while (btree_range_next(cur, &key, &rid)) {
    printf("key=%d -> page=%u slot=%u\n", key, rid.page_id, rid.slot_id);
}
btree_range_close(cur);
```

工作原理：
1. 找到 >= start 的第一个叶子节点
2. 沿 `next_leaf` 链表遍历
3. 遇到 > end 的键时停止

```
[10,20,30] → [40,50,60] → [70,80,90] → ...
                ↑
           从这里开始扫描 250~450
```

## 删除

本章实现简化删除：从叶子移除键值，移动后续键填补空位。

```c
bool btree_delete(btree_t *tree, btree_key_t key) {
    // 1. 找到叶子
    // 2. 找到键，移除，移动后续键
    // 3. 不做合并/借位（简化）
}
```

!!! note "完整删除策略"
    真实 B+Tree 删除时，如果节点键数 < MIN_KEYS：
    1. 尝试从兄弟节点借一个键
    2. 借不了则合并兄弟节点
    3. 合并可能递归影响父节点

    本章简化以控制代码量，文档完整说明策略。

## 阶数选择

| 参数 | 值 | 说明 |
|---|---|---|
| MAX_KEYS | 32 | 节点最多 32 个键 |
| MIN_KEYS | 16 | 节点最少 16 个键（根除外） |

固定阶数简化教学。真实数据库根据页大小动态计算：
- 内节点容量 = (PAGE_SIZE - header) / (key_size + child_size)
- 叶子容量 = (PAGE_SIZE - header) / (key_size + rid_size)

## 使用示例

```c
pager_t *pager = pager_open("index.db");
buffer_pool_t *bp = bp_create(pager, 64, REPLACER_LRU);
btree_t *tree = btree_create(bp);

// 插入索引项
for (int i = 1; i <= 1000; i++) {
    rid_t rid = { i / 10, i % 10 };
    btree_insert(tree, i, rid);
}

// 点查
rid_t rid;
if (btree_find(tree, 42, &rid)) {
    printf("找到: page=%u slot=%u\n", rid.page_id, rid.slot_id);
}

// 范围查
btree_cursor_t *cur = btree_range_open(tree, 100, 200);
btree_key_t key;
while (btree_range_next(cur, &key, &rid)) {
    printf("%d -> (%u, %u)\n", key, rid.page_id, rid.slot_id);
}
btree_range_close(cur);
```

## 设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 键类型 | int32_t | 教学简化，章7 可扩展 |
| 阶数 | 固定 32 | 教学清晰，真实DB动态计算 |
| 删除 | 简化（不合并） | 控制代码量，文档说明完整策略 |
| 节点存储 | 复用 page_t | 一节点一页，复用 Buffer Pool |
| 叶子链表 | next_leaf 指针 | 支持高效范围扫描 |

## 习题

1. 如果键类型改为变长字符串，节点布局需要怎么调整？
2. B+Tree 的根节点分裂时，树高增加。这对查询性能有什么影响？
3. 如果不做叶子链表，范围查询还能实现吗？性能如何？
4. 重复键如何处理？（提示：键+RID 组合唯一，或溢出页）
5. 如何实现"前缀压缩"来增加内节点的扇出？

---

上一章：[章2 Buffer Pool](02-buffer-pool.md) | 下一章：[章4 堆表存储](04-heap.md)