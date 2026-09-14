# 章3：B+Tree 索引

> 索引是数据库加速查询的核心。本章从"为什么需要索引"出发，一步步从二叉搜索树（BST）演进到 B-Tree，再到 B+Tree，讲清楚每一步的动机和改进，最后逐行解读 miniDB 的 B+Tree 实现。
>
> 面向新手：假设你只会一点 C 语言，数据结构只听过数组、链表、二叉树。本章会用大量 ASCII 树形图、表格和类比把概念讲透。

---

## 目录

1. [为什么需要索引](#为什么需要索引)
2. [BST → B-Tree → B+Tree 演进](#bst--b-tree--btree-演进)
3. [B+Tree 详细结构](#btree-详细结构)
4. [插入与分裂](#插入与分裂)
5. [查找和范围查询](#查找和范围查询)
6. [删除（简化版）](#删除简化版)
7. [代码逐行解读](#代码逐行解读)
8. [B+Tree 在数据库中的角色](#btree-在数据库中的角色)
9. [与真实数据库对比](#与真实数据库对比)
10. [习题](#习题)

---

## 为什么需要索引

### 全表扫描有多慢

假设我们有一张用户表 `users`，存了 1000 万行数据，每行 200 字节。现在要执行：

```sql
SELECT * FROM users WHERE id = 1234567;
```

如果没有索引，数据库只能**全表扫描**（Table Scan）：从第 1 行开始，一行一行比较 `id`，直到找到为止。

| 数据量 | 每行字节 | 表总大小 | 最坏比较次数 | 假设每次比较 1 纳秒 | 总耗时 |
|---|---|---|---|---|---|
| 1 千行 | 200 B | 200 KB | 1 千 | 1 纳秒 | 1 纳秒 |
| 10 万行 | 200 B | 20 MB | 10 万 | 1 纳秒 | 0.1 毫秒 |
| 1000 万行 | 200 B | 2 GB | 1000 万 | 1 纳秒 | 10 秒 |
| 1 亿行 | 200 B | 20 GB | 1 亿 | 1 纳秒 | 100 秒 |

10 秒查一条记录，用户早就关掉 App 了。而且真实场景里磁盘 IO 才是瓶颈，一次磁盘随机读约 10 毫秒，1000 万行如果全在磁盘上，全表扫描可能要几分钟。

### 字典类比

索引的思路和查字典一模一样：

- **没有索引** = 一页一页翻字典找"鸿"字 → 翻完 1500 页才找到 → 全表扫描
- **有索引（部首目录）** = 先查部首目录"鸟"在第 1000 页 → 直接翻到 1000 页 → 索引加速

字典的部首目录、拼音目录、四角号码目录，本质上都是**索引**。一本字典可以有多套索引，对应数据库里一张表可以有多个索引。

```
字典结构                          数据库结构
┌──────────────┐                 ┌──────────────┐
│ 拼音目录      │  ← 索引1        │ idx_name      │  ← 索引1
│ a   ...  页1  │                 │ "Alice" → RID │
│ b   ...  页20 │                 │ "Bob"   → RID │
│ ...           │                 │ ...           │
├──────────────┤                 ├──────────────┤
│ 部首目录      │  ← 索引2        │ idx_age       │  ← 索引2
│ 鸟   ...  页N │                 │ 18 → RID      │
│ ...           │                 │ 25 → RID      │
├──────────────┤                 ├──────────────┤
│ 字典正文      │  ← 表数据       │ users 表      │  ← 表数据
│ 鸿  ...  释义 │                 │ (id, name...) │
└──────────────┘                 └──────────────┘
```

索引里存的是"关键字 → 位置"的映射。在数据库里，这个"位置"叫 **RID（Record ID）**，后面会讲。

### 索引的代价

索引不是免费的，天下没有白吃的午餐：

| 代价 | 说明 |
|---|---|
| 空间 | 索引本身要占磁盘空间，1000 万行的索引可能几百 MB |
| 写放大 | 每次 INSERT/UPDATE/DELETE 都要同步更新索引，写入变慢 |
| 维护 | 索引太多会拖慢写入、占用内存、增加优化器选择难度 |

所以索引是**用写换读**：写入变慢一点，查询快很多倍。读多写少的场景非常适合。

### 索引要满足什么

一个好的索引结构需要满足：

1. **点查询快**：`WHERE id = 123` 要快 → O(log n) 甚至更好
2. **范围查询快**：`WHERE id BETWEEN 100 AND 200` 要快 → 不能只支持点查
3. **有序输出**：`ORDER BY id` 要能利用索引顺序 → 避免额外排序
4. **磁盘友好**：每次查询尽量少读磁盘页 → 树要矮、节点要大
5. **插入/删除可控**：不能一次插入导致全树重构 → 局部调整

二叉搜索树满足 1，但不满足 4（树太高）。链表满足 3，但不满足 1。我们需要一个**综合最优**的结构——这就是 B+Tree。

---

## BST → B-Tree → B+Tree 演进

数据库索引不是一开始就用 B+Tree 的。它是从二叉搜索树一步步演进来的。理解这个演进，就理解了 B+Tree 为什么长这样。

### 第 0 步：二叉搜索树（BST）

二叉搜索树（Binary Search Tree）是最直观的索引结构：每个节点存一个键，左子树都小于它，右子树都大于它。

```
              50
            /    \
           /      \
         30        70
        /  \      /  \
      20    40  60    80
     /  \
   10    25
```

**查找 25 的过程**：

```
1. 从根 50 开始：25 < 50，往左走
2. 到 30：25 < 30，往左走
3. 到 20：25 > 20，往右走
4. 到 25：找到了！
```

**BST 的优点**：

- 结构简单，代码好写
- 查找、插入、删除都是 O(log n)（平衡的情况下）

**BST 的致命问题**：

| 问题 | 说明 |
|---|---|
| 树太高 | 100 万数据 ≈ 20 层，每层一次磁盘 IO → 20 次 IO |
| 容易退化 | 顺序插入会退化成链表，变成 O(n) |
| 不支持范围查询 | 要中序遍历，跨节点跳来跳去，磁盘 IO 爆炸 |

**退化演示**：依次插入 10, 20, 30, 40, 50

```
10                          10
  \                           \
   20          而不是           20
    \                           \
     30                          30
      \                           \
       40                          40
        \                           \
         50                          50

  退化成链表！查找 50 要 5 次比较，O(n)
```

即使是用 AVL 或红黑树保证平衡，树高依然是 O(log₂ n)，100 万数据还是 20 层。**树高 = 磁盘 IO 次数**，这才是核心矛盾。

### 第 1 步：B-Tree（多路搜索树）

BST 的问题在于**每个节点只有 2 个子节点**（二叉）。如果让每个节点有 200 个子节点呢？树高立刻从 20 降到 3。

**核心思想**：把"瘦高"的树压成"矮胖"的树。

B-Tree 是一棵**多路搜索树**，每个节点可以存多个键、有多个子节点。m 阶 B-Tree 每个节点最多 m 个子节点、m-1 个键。

```
                  [30 | 50 | 70]              ← 1 个节点存 3 个键，4 个子节点
                 /    |    |    \
            [10,20] [40] [60] [80,90]         ← 叶子节点也存数据
```

**查找 60 的过程**：

```
1. 根节点 [30 | 50 | 70]：
   - 60 > 50 且 60 < 70 → 走第 3 个子节点（70 左边）
2. 到 [60]：找到了！
```

**B-Tree 的改进**：

| 指标 | BST | B-Tree (m=200) |
|---|---|---|
| 100 万数据的树高 | 20 | 3 |
| 查找 IO 次数 | 20 | 3 |
| 节点大小 | 几十字节 | 一页（4KB） |

**B-Tree 的问题**：

```
B-Tree 节点布局：
┌─────────────────────────────────┐
│ key1 │ data1 │ key2 │ data2 │ ... │ child1 │ child2 │ ... │
└─────────────────────────────────┘
        ↑ 每个 key 都带着 data（行数据）
```

问题在于**内节点也存数据**：

1. 数据很大（一行 200 字节），一个 4KB 页存不下几个键
2. 扇出（子节点数）= (页大小 - header) / (key + data + child)，data 越大扇出越小
3. 扇出小 → 树又变高 → IO 又变多

**举个例子**：

| 场景 | key 大小 | data 大小 | 4KB 页能存多少键 | 扇出 | 100 万数据树高 |
|---|---|---|---|---|---|
| B-Tree（内节点存数据） | 4 B | 200 B | 4096 / (4+200+4) ≈ 20 | 21 | log₂₁(100万) ≈ 5 |
| B-Tree（内节点只存键） | 4 B | 0 B | 4096 / (4+4) = 512 | 513 | log₅₁₃(100万) ≈ 2 |

内节点不存数据，扇出从 21 飙到 513，树高从 5 降到 2！这就是 B+Tree 的核心动机。

### 第 2 步：B+Tree

B+Tree 在 B-Tree 基础上做了两个关键改动：

1. **内节点只存键，不存数据** → 扇出更大 → 树更矮
2. **数据全部存在叶子节点** → 叶子节点用链表串起来 → 范围查询超快

```
B-Tree:                          B+Tree:
      [30|50|70]                       [30|50|70]        ← 内节点只存键
     /   |   |   \                    /   |   |   \
  [10,20][40][60][80,90]          [10,20]→[30,40]→[50,60]→[70,80,90]
   ↑ 叶子存数据                       ↑ 叶子存数据，且用 → 链表连接
```

**B+Tree 的三大优势**：

| 优势 | 原因 | 带来的好处 |
|---|---|---|
| 内节点只存键 | 键很小，一页能存几百个 | 扇出大、树矮、IO 少 |
| 数据全在叶子 | 叶子是数据的唯一归宿 | 内节点纯路由，扇出最大化 |
| 叶子链表 | next_leaf 指针串联所有叶子 | 范围查询只需顺着链表走 |

**完整对比表**：

| 特性 | BST | B-Tree | B+Tree |
|---|---|---|---|
| 节点子节点数 | 2 | m | m（更大，因内节点不存数据） |
| 数据存放位置 | 所有节点 | 所有节点 | 仅叶子 |
| 树高（100万数据） | 20 | 3~5 | 2~3 |
| 点查询 IO | 20 | 3~5 | 2~3 |
| 范围查询 | 差（中序遍历） | 一般（要回溯） | 好（叶子链表） |
| 磁盘友好 | 差 | 好 | 很好 |

### 为什么数据库都选 B+Tree

主流数据库的索引结构：

| 数据库 | 索引结构 | 说明 |
|---|---|---|
| MySQL InnoDB | B+Tree | 经典实现，聚簇 + 二级索引 |
| PostgreSQL | B-Tree（变种） | 实际是 B+Tree 风格，叶子有链表 |
| SQLite | B-Tree | 内节点也存数据，但页大小 4KB |
| Oracle | B-Tree | B+Tree 变种 |
| SQL Server | B+Tree | 聚簇 + 非聚簇 |

它们都选 B+Tree（或其变种），因为 B+Tree 在**磁盘环境**下综合最优：

1. 树矮 → IO 少 → 查询快
2. 叶子链表 → 范围查询快
3. 节点大小 = 页大小 → 完美复用 Buffer Pool

> **关键洞察**：B+Tree 是为**磁盘**设计的结构。如果数据全在内存，红黑树、跳表甚至哈希表都可能更快（Redis 就用跳表）。但一旦涉及磁盘 IO，B+Tree 就是王者。

---

## B+Tree 详细结构

### 阶（Order）的概念

B+Tree 是"m 阶"的，m 决定了每个节点能存多少键：

- 每个节点**最多** m-1 个键
- 每个节点**最少** ⌈m/2⌉-1 个键（根除外）
- 每个内节点最多 m 个子节点

miniDB 选择 **MAX_KEYS = 32**，所以：

| 参数 | 值 | 含义 |
|---|---|---|
| BTREE_MAX_KEYS | 32 | 节点最多 32 个键 |
| BTREE_MIN_KEYS | 16 | 节点最少 16 个键（根除外） |
| 内节点子节点数 | 最多 33 | 键数 + 1 |
| 叶子节点 RID 数 | 最多 32 | 和键数一致 |

**树高估算**（每个内节点至少 16 个键，即至少 17 个子节点）：

| 数据量 | 树高（最坏） | 树高（最好，每节点满 32 键） |
|---|---|---|
| 1 千 | 2 | 2 |
| 10 万 | 3 | 3 |
| 100 万 | 4 | 3 |
| 1 亿 | 6 | 5 |

即使 1 亿数据，最多 6 次 IO，加上 Buffer Pool 命中率高，实际磁盘 IO 通常 1~2 次。

### 内部节点 vs 叶子节点

B+Tree 有两种节点，结构和职责完全不同：

```
内部节点（INTERNAL）                   叶子节点（LEAF）
┌────────────────────────────┐       ┌────────────────────────────┐
│ node_type = 0              │       │ node_type = 1              │
│ num_keys = 3               │       │ num_keys = 4               │
│ parent_page_id             │       │ parent_page_id             │
│ next_leaf = INVALID (不用) │       │ next_leaf = 下一叶子页号   │
│ keys:   [30 | 50 | 70]     │       │ keys:   [10 | 20 | 30 | 40]│
│ children: [P1|P2|P3|P4]    │       │ rids:   [R1| R2| R3| R4]   │
└────────────────────────────┘       └────────────────────────────┘
        ↓ 职责：路由                       ↓ 职责：存数据
        ↓ 只存键，不存 RID                  ↓ 存键 + RID
        ↓ children 指向子节点               ↓ next_leaf 指向兄弟叶子
```

**对比表**：

| 字段 | 内部节点 | 叶子节点 |
|---|---|---|
| node_type | 0 (BTREE_INTERNAL) | 1 (BTREE_LEAF) |
| keys | 路由键，用于找子节点 | 实际索引键 |
| children | 子节点页号（num_keys + 1 个） | 不用 |
| rids | 不用 | 指向表数据的 RID（num_keys 个） |
| next_leaf | 不用（INVALID_PAGE_ID） | 下一个叶子页号 |
| parent | 父节点页号 | 父节点页号 |

### 内部节点的路由规则

内节点的 keys 和 children 是怎么对应的？这是 B+Tree 最容易搞混的地方。

```
内节点：
        keys[0]=30   keys[1]=50   keys[2]=70
        /            |            |            \
   children[0]  children[1]  children[2]  children[3]
```

**路由规则**（查找 key 时走哪个子节点）：

| 条件 | 走哪个子节点 |
|---|---|
| key < keys[0] (即 key < 30) | children[0] |
| keys[0] ≤ key < keys[1] (即 30 ≤ key < 50) | children[1] |
| keys[1] ≤ key < keys[2] (即 50 ≤ key < 70) | children[2] |
| key ≥ keys[2] (即 key ≥ 70) | children[3] |

**通用公式**：

- `child[0]` 指向 key < keys[0] 的子树
- `child[i]` 指向 keys[i-1] ≤ key < keys[i] 的子树（1 ≤ i ≤ num_keys-1）
- `child[num_keys]` 指向 key ≥ keys[num_keys-1] 的子树

**例子**：查找 key=55

```
        [30 | 50 | 70]
        /    |    |    \
      ...   ...  ...   ...
                ↑
        50 ≤ 55 < 70，走 children[2]
```

代码里用 `node_find_key` 做二分查找，返回的就是该走的子节点下标。

### 为什么叶子用链表连接

这是 B+Tree 相比 B-Tree 最关键的改进。先看没有链表的情况：

**B-Tree 范围查询 30~60**（没有叶子链表）：

```
        [30 | 50 | 70]
       /    |    |    \
  [10,20] [40] [60] [80,90]
              ↑           ↑
         要查 40         要查 60
         但 40 和 60 在不同叶子
         怎么从 40 跳到 60？
         → 必须回到父节点，重新走 children[2]
         → 这叫"回溯"，效率低
```

**B+Tree 范围查询 30~60**（有叶子链表）：

```
        [30 | 50 | 70]
       /    |    |    \
  [10,20]→[30,40]→[50,60]→[70,80,90]
              ↑    ↑
         找到 30,40，顺着 next_leaf
         直接到 [50,60]，继续读 50,60
         → 不用回父节点！顺序读超快
```

**链表的好处**：

1. **范围查询 = 顺序扫描叶子链表**，不用在树上跳来跳去
2. **磁盘顺序读**比随机读快很多（机械硬盘差 100 倍，SSD 也差几倍）
3. **`ORDER BY` 直接用链表顺序**，不用额外排序

### 节点在磁盘上的布局

miniDB 里一个 B+Tree 节点 = 一个 page（4KB）。节点布局复用了章1 的 page header：

```
偏移        大小    字段              说明
[0..3]      4B     page_id           页号（章1）
[4]         1B     page_type         = PAGE_TYPE_INDEX（章1）
[5..7]      3B     (保留)            页头剩余
[8..19]     12B    (页头其他)        章1 的 page header 共 20 字节
─────────── 以上是 page header ───────────
[20]        1B     node_type         0=内部, 1=叶子
[21..22]    2B     num_keys          当前键数（大端）
[23..26]    4B     parent_page_id    父节点页号
[27..30]    4B     next_leaf_page_id 下一个叶子页号（叶子用）
[31..158]   128B   keys[32]          32 个键，每个 4B
[159..294]  136B   children[33]      内节点用：33 个子节点页号，每个 4B
   或
[159..414]  256B   rids[32]          叶子用：32 个 RID，每个 8B
```

**RID（Record ID）的结构**：

```c
typedef struct {
    page_id_t page_id;   // 4B：数据所在页号
    slot_id_t slot_id;   // 2B：页内槽号
} rid_t;                 // 共 6B（代码里按 8B 对齐）
```

RID 指向堆表（Heap）里的实际行数据。B+Tree 叶子存的是 `(key, RID)`，通过 RID 去 Heap 取行。这叫**非聚簇索引**，后面会详细讲。

**容量验算**：

| 字段 | 大小 | 说明 |
|---|---|---|
| page header | 20 B | 章1 |
| node header | 11 B | type + num_keys + parent + next_leaf |
| keys[32] | 128 B | 32 × 4 |
| children[33] | 132 B | 33 × 4（内节点） |
| rids[32] | 256 B | 32 × 8（叶子） |
| **内节点总计** | 20+11+128+132 = 291 B | 远小于 4KB，留有余量 |
| **叶子总计** | 20+11+128+256 = 415 B | 远小于 4KB |

miniDB 的 4KB 页对 32 阶来说绰绰有余。真实数据库会动态算阶数把页塞满，miniDB 固定 32 阶是为了教学清晰。

---

## 插入与分裂

插入是 B+Tree 最核心、最复杂的操作。理解了插入，B+Tree 就懂了一半。

### 插入的总流程

```
btree_insert(key, rid)
    │
    ▼
1. 从根节点找到 key 应该去的叶子节点
    │  （find_leaf：一路向下走内节点）
    ▼
2. 在叶子节点里二分找插入位置
    │  （node_find_key）
    ▼
3. 如果 key 已存在 → 返回 false（不存重复键）
    │
    ▼
4. 如果叶子没满 → 直接插入，结束
    │  （leaf_insert_at：移动后续键，腾位置）
    ▼
5. 如果叶子满了 → 先插入，再分裂
    │
    ▼
6. 分裂叶子：从中间切开，右半部分新建一个叶子
    │  中间键上推给父节点
    ▼
7. 父节点接收上推的键
    │  如果父节点也满了 → 递归分裂
    ▼
8. 如果根节点分裂 → 创建新根，树高 +1
```

### 逐步演示：从空树插入 1,2,3,4,5,6,7

为了看清分裂过程，我们假设 **MAX_KEYS = 3**（教学用，真实是 32）。MIN_KEYS = ⌈3/2⌉-1 = 1。

**初始**：空树，创建一个空叶子作为根。

```
[]   ← 根节点，也是叶子，num_keys=0
```

**插入 1**：

```
[1]   ← 直接放进叶子
```

**插入 2**：

```
[1 | 2]   ← 2 > 1，放右边
```

**插入 3**：

```
[1 | 2 | 3]   ← 满了（MAX_KEYS=3），但还能放
```

**插入 4**：先放进去，再分裂。

```
插入后：[1 | 2 | 3 | 4]   ← 超过容量（4 个键 > MAX_KEYS=3）

分裂（mid = 4/2 = 2）：
左叶子保留前 2 个：[1 | 2]
右叶子拿走后 2 个：[3 | 4]
上推键 = 右叶子第一个键 = 3

父节点（新根，内节点）：
        [3]
       /   \
  [1|2] → [3|4]    ← 叶子链表：[1,2] 的 next_leaf 指向 [3,4]
```

**插入 5**：

```
1. 找叶子：从根 [3] 开始，5 ≥ 3 → 走右子节点 [3|4]
2. [3|4] 没满，直接插入
3. 结果：
        [3]
       /   \
  [1|2] → [3|4|5]
```

**插入 6**：

```
1. 找叶子：6 ≥ 3 → 走 [3|4|5]
2. 插入后 [3|4|5|6] 超容量，分裂：
   左：[3|4]，右：[5|6]，上推键=5

3. 上推给父节点 [3]：
   父节点插入 5 → [3|5]，没满，结束

4. 结果：
        [3 | 5]
       /    |    \
  [1|2] → [3|4] → [5|6]
```

**插入 7**：

```
1. 找叶子：7 ≥ 5 → 走 [5|6]
2. [5|6] 没满，直接插入
3. 结果：
        [3 | 5]
       /    |    \
  [1|2] → [3|4] → [5|6|7]
```

**再插入 8**（演示父节点也分裂）：

```
1. 找叶子：8 ≥ 5 → 走 [5|6|7]
2. 插入后 [5|6|7|8] 超容量，分裂：
   左：[5|6]，右：[7|8]，上推键=7

3. 上推给父节点 [3|5]：
   父节点插入 7 → [3|5|7]，满了（MAX_KEYS=3）！

4. 父节点分裂（内节点分裂，mid=1）：
   上推键 = keys[1] = 5
   左内节点：[3]
   右内节点：[7]
   
   注意：内节点分裂时，中间键 5 被上推，不留在任何子节点！
   （叶子分裂时，右叶子第一个键既留在叶子又上推）

5. 创建新根 [5]：
            [5]            ← 新根
           /   \
      [3]       [7]        ← 内节点
      /  \      /  \
  [1|2]→[3|4] [5|6]→[7|8]  ← 叶子链表全连起来
```

注意叶子链表：`[1|2] → [3|4] → [5|6] → [7|8]`，即使父节点分裂了，叶子链表也要保持正确。

### 叶子分裂的详细步骤

```
分裂前（叶子已超容量，假设 MAX_KEYS=4，现在 5 个键）：
┌─────────────────────────┐
│ [10|20|25|30|40]        │  ← num_keys=5
│ parent=P0               │
│ next_leaf=P_next        │
└─────────────────────────┘

步骤1：算中点 mid = 5/2 = 2
步骤2：新建一个叶子页 P_new
步骤3：把 mid 之后的键搬到 P_new
        原叶子保留：[10|20]      （前 mid 个）
        新叶子得到：[25|30|40]   （后 n-mid 个）
步骤4：更新叶子链表
        P_new.next_leaf = 原叶子.next_leaf
        原叶子.next_leaf = P_new
步骤5：上推键 = P_new.keys[0] = 25

分裂后：
┌──────────────┐     ┌──────────────────┐
│ [10|20]      │ →   │ [25|30|40]       │
│ parent=P0    │     │ parent=P0        │
│ next_leaf=P_new   │ next_leaf=P_next │
└──────────────┘     └──────────────────┘
         ↑                    ↑
         左叶子               右叶子
         
上推给父节点：键 25，左子=P_left，右子=P_new
```

**关键细节**：叶子分裂时，上推的键 25 **仍然保留在右叶子**里。这是 B+Tree 和 B-Tree 的重要区别——B-Tree 分裂时中间键会被移走，B+Tree 叶子分裂时中间键只是复制上推。

### 内节点分裂的详细步骤

```
分裂前（内节点已超容量，5 个键，6 个子节点）：
┌──────────────────────────────────────┐
│ keys:    [10|20|30|40|50]            │
│ children: [C0|C1|C2|C3|C4|C5]        │
└──────────────────────────────────────┘

步骤1：算中点 mid = 5/2 = 2
步骤2：上推键 = keys[mid] = keys[2] = 30
步骤3：新建内节点 P_new
步骤4：把 mid 之后的键和子节点搬到 P_new
        原节点保留：keys=[10|20], children=[C0|C1|C2]
        新节点得到：keys=[40|50], children=[C3|C4|C5]
        注意：keys[mid]=30 被上推，不留在任何节点！
步骤5：更新 P_new 的所有子节点的 parent 指向 P_new

分裂后：
        上推键 = 30
        ┌───────────┐         ┌───────────┐
        │ keys:[10|20]│        │ keys:[40|50]│
        │ ch:[C0|C1|C2]│       │ ch:[C3|C4|C5]│
        └───────────┘         └───────────┘
              ↑                    ↑
          左内节点             右内节点
          
父节点接收：(30, 左=P_left, 右=P_new)
```

**叶子分裂 vs 内节点分裂的区别**：

| 方面 | 叶子分裂 | 内节点分裂 |
|---|---|---|
| 上推的键 | 右叶子第一个键的**副本** | 中间键**移走**（不留原节点） |
| 上推键还在不在数据里 | 在（右叶子保留） | 不在（被移走上推） |
| 子节点处理 | 无子节点 | 要更新子节点的 parent 指针 |
| 链表维护 | 要更新 next_leaf | 不涉及 |

这个区别在代码里体现为：`split_leaf` 上推 `node_get_key(new_leaf, 0)`（复制），`split_internal` 上推 `node_get_key(node, mid)` 并把 mid 之后的键搬走（移走）。

### 根节点分裂：树长高

根节点分裂是特殊情况：根没有父节点，所以要**创建新根**。

```
分裂前（根节点也是叶子，满了）：
[10|20|30|40]   ← 根，没有父节点

分裂后：
        [30]            ← 新建的根（内节点）
       /   \
  [10|20] → [30|40]    ← 原根分裂成两个叶子
```

**新根的特点**：

- node_type = BTREE_INTERNAL
- parent = INVALID_PAGE_ID（根没有父）
- num_keys = 1（第一次分裂时只有一个键）
- children[0] = 左子节点，children[1] = 右子节点

**树高变化**：每次根分裂，树高 +1。这是 B+Tree 唯一长高的方式。

```
树高 1（只有根叶子）：
[10|20|30]

根分裂 → 树高 2：
    [20]
   /   \
[10] → [20|30]

根再分裂 → 树高 3：
        [20|40]
       /   |   \
  [10]→[20|30]→[40|50]
```

---

## 查找和范围查询

### 点查找：btree_find

点查找就是从根走到叶子，在叶子里二分找键。

**查找 key=55 的过程**（假设树如下）：

```
            [30 | 60]              ← 第1层：根
           /    |    \
      [10|20] [40|50] [70|80|90]   ← 第2层：叶子
```

```
步骤1：从根 [30|60] 开始
        node_find_key([30|60], 55)
        → 55 ≥ 30 且 55 < 60 → 返回 idx=1
        → 走 children[1]

步骤2：到叶子 [40|50]
        node_find_key([40|50], 55)
        → 55 ≥ 50 → 返回 idx=2
        → idx=2 ≥ num_keys=2，没找到
        → 返回 false
```

**查找 key=40 的过程**：

```
步骤1：根 [30|60]
        40 ≥ 30 且 40 < 60 → idx=1 → 走 children[1]

步骤2：叶子 [40|50]
        node_find_key 返回 idx=0
        keys[0]=40 == 40 → 找到！
        返回 rids[0]
```

**点查找的复杂度**：

| 树高 h | 每层二分查找 | 总比较次数 | 磁盘 IO |
|---|---|---|---|
| 2 | log₂(32) = 5 | 10 | 2 |
| 3 | 5 | 15 | 3 |
| 4 | 5 | 20 | 4 |

100 万数据树高 3~4，磁盘 IO 3~4 次，加上 Buffer Pool 命中，实际磁盘 IO 通常 1 次。

### 范围查询：btree_range_open / next / close

范围查询是 B+Tree 的杀手锏。`WHERE key BETWEEN 100 AND 200` 的执行过程：

```
            [100 | 300 | 500]              ← 根
           /      |       |      \
     [10..90] [100..290] [300..490] [500..900]   ← 叶子链表
                    ↑
              从这里开始扫描
              顺着 next_leaf 一直走
              直到 key > 200 停止
```

**三步走**：

1. **`btree_range_open(start, end)`**：找到 ≥ start 的第一个叶子节点和位置，创建游标
2. **`btree_range_next()`**：每次返回一个 (key, rid)，跨叶子时走 next_leaf
3. **`btree_range_close()`**：释放游标

**游标（cursor）的结构**：

```c
struct btree_cursor {
    buffer_pool_t *bp;     // Buffer Pool 引用
    page_id_t leaf_pid;    // 当前叶子页号
    int idx;               // 当前叶子内的位置
    int num_keys;          // 当前叶子的键数
    btree_key_t end;       // 范围右端点
    bool done;             // 是否已扫描完
};
```

**逐步演示**：范围查询 40~80

```
树：
        [30 | 60]
       /    |    \
  [10|20]→[40|50]→[70|80|90]
              ↑
        从 [40|50] 的 idx=0 开始

第1次 next()：key=40, rid=R40  → 返回 40
第2次 next()：key=50, rid=R50  → 返回 50
第3次 next()：idx=2 ≥ num_keys=2
            → 走 next_leaf 到 [70|80|90]
            → idx=0, key=70 → 返回 70
第4次 next()：key=80 → 返回 80
第5次 next()：key=90 > end=80 → done=true，返回 false
```

**范围查询的复杂度**：

| 场景 | IO 次数 | 说明 |
|---|---|---|
| 找起点 | h（树高） | 从根走到叶子 |
| 扫描 k 个结果 | k / 叶子容量 | 顺着链表走，每跨一个叶子一次 IO |
| 总计 | h + k/32 | 通常 h=3，k=1000 → 3 + 32 = 35 次 IO |

对比全表扫描 1000 万数据要 1000 万次比较，范围查询 1000 个结果只要 35 次 IO，快了 28 万倍。

### 叶子链表的作用再强调

没有叶子链表，范围查询会怎样？

```
没有链表：要查 40~80
        [30 | 60]
       /    |    \
  [10|20] [40|50] [70|80|90]
              ↑        ↑
         扫完 [40|50] 要到 [70|80|90]
         但叶子之间没有指针！
         → 必须回到父节点 [30|60]
         → 重新走 children[2] 到 [70|80|90]
         → 这叫"回溯"，每次跨叶子都要回溯
         → 如果范围跨 100 个叶子，要回溯 100 次
         → 每次回溯要重新从根走，IO 爆炸
```

```
有链表：要查 40~80
  [40|50] → [70|80|90]
       ↑next_leaf
  扫完 [40|50]，直接顺着 next_leaf 到 [70|80|90]
  → 一次指针跳转，不用回溯
  → 跨 100 个叶子也只要 100 次顺序读
  → 顺序读还能预读（readahead），更快
```

这就是为什么所有主流数据库的 B+Tree 都有叶子链表。

---

## 删除（简化版）

### 为什么删除比插入复杂

插入只需要**分裂**（一分为二），是"加法"。删除可能需要**借位**（从兄弟借一个键）或**合并**（两个节点合一个），是"减法"，情况更多。

| 操作 | 插入 | 删除 |
|---|---|---|
| 触发条件 | 节点满了 | 节点太空了（< MIN_KEYS） |
| 处理方式 | 分裂（一分为二） | 借位 或 合并 |
| 借位 | 不需要 | 从左/右兄弟借一个键 |
| 合并 | 不需要 | 和左/右兄弟合并成一个 |
| 递归 | 父节点满了递归分裂 | 父节点键少了可能递归合并 |
| 树高变化 | 根分裂时 +1 | 根合并时 -1 |
| 情况数 | 2~3 种 | 5~6 种 |

**完整删除的几种情况**：

```
情况1：叶子键数 > MIN_KEYS
       → 直接删除，结束（最简单）

情况2：叶子键数 = MIN_KEYS，但兄弟有富余
       → 从兄弟借一个键
       → 更新父节点的分隔键

情况3：叶子键数 = MIN_KEYS，兄弟也没富余
       → 和兄弟合并
       → 父节点少一个键
       → 父节点可能也低于 MIN_KEYS → 递归

情况4：合并一直传到根，根只剩一个键
       → 删除根，树高 -1
```

**借位演示**（从右兄弟借）：

```
删除前（MIN_KEYS=2）：
        [30 | 50]
       /    |    \
  [10|20] [40] [60|70|80]
              ↑
         [40] 只有 1 个键 < MIN_KEYS=2
         右兄弟 [60|70|80] 有 3 个键 > MIN_KEYS

借位：把右兄弟第一个键 60 借过来
        [30 | 60]          ← 父节点 50 变成 60
       /    |    \
  [10|20] [40|50] [70|80]  ← 50 从父节点下来，60 从右兄弟上来
```

**合并演示**（兄弟也没富余）：

```
删除前（MIN_KEYS=2）：
        [30 | 50]
       /    |    \
  [10|20] [40] [60]      ← 要删 [40] 里的 40，且右兄弟 [60] 也没富余

合并：[40] 和 [60] 合并成 [60]，父节点删掉 50
        [30]
       /    \
  [10|20] [60]

如果父节点也低于 MIN_KEYS → 递归处理父节点
```

### miniDB 的简化删除策略

miniDB 为了控制代码量，实现了**简化删除**：只从叶子移除键，不做借位和合并。

```c
bool btree_delete(btree_t *tree, btree_key_t key) {
    // 1. 找到叶子
    page_id_t leaf_pid = find_leaf(tree->bp, tree->root_pid, key);
    page_t *leaf = bp_fetch_page(tree->bp, leaf_pid);

    // 2. 二分找到键
    int idx = node_find_key(leaf, key);
    uint16_t n = node_get_num_keys(leaf);

    // 3. 没找到 → 返回 false
    if (idx >= n || node_get_key(leaf, idx) != key) {
        bp_unpin_page(tree->bp, leaf_pid, false);
        return false;
    }

    // 4. 移除键：把后面的键往前移
    for (int i = idx; i < n - 1; i++) {
        node_set_key(leaf, i, node_get_key(leaf, i + 1));
        node_set_rid(leaf, i, node_get_rid(leaf, i + 1));
    }
    node_set_num_keys(leaf, n - 1);

    // 5. 不做合并/借位（简化！）
    bp_unpin_page(tree->bp, leaf_pid, true);
    return true;
}
```

**简化删除的后果**：

| 问题 | 影响 | 严重程度 |
|---|---|---|
| 节点可能低于 MIN_KEYS | 树可能不平衡 | 中 |
| 空间浪费 | 删除的键占的空间不回收 | 低 |
| 树高不会降低 | 只增不减 | 低 |
| 查询仍然正确 | 逻辑没问题，只是效率可能下降 | 低 |

**为什么可以接受**：

1. **教学优先**：完整删除代码量大，容易掩盖核心逻辑
2. **正确性不受影响**：查询、插入仍然正确
3. **性能可接受**：大多数工作负载读多写少，删除少
4. **真实数据库有后台清理**：PostgreSQL 有 VACUUM，MySQL 有页合并

> **真实数据库的做法**：InnoDB 在删除时如果页面空间使用率低于 MERGE_THRESHOLD（默认 50%），会尝试和兄弟页合并。PostgreSQL 的 B-Tree 删除是"懒惰删除"——标记删除，VACUUM 阶段真正清理。

### 偷懒删除策略的变种

除了 miniDB 这种"完全不合并"，还有几种偷懒策略：

| 策略 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| miniDB：完全不合并 | 删了就删了 | 代码最简单 | 树可能退化 |
| 标记删除 | 打 tombstone，不真删 | 删除 O(1) | 需要定期清理 |
| 惰性合并 | 删除时检查，但合并推迟 | 平衡好 | 实现复杂 |
| 完整删除 | 借位 + 合并 | 树始终平衡 | 代码最复杂 |

miniDB 选了第一种，教学清晰，文档说明完整策略。

---

## 代码逐行解读

现在逐行解读 miniDB 的 B+Tree 实现。代码分四个文件：

| 文件 | 行数 | 职责 |
|---|---|---|
| btree_node.h | 69 | 节点布局定义、访问函数声明 |
| btree_node.c | 105 | 节点字节级读写实现 |
| btree.h | 24 | B+Tree 对外接口声明 |
| btree.c | 328 | B+Tree 插入/查找/删除/范围查询实现 |

### btree_node.h：节点布局

```c
#define BTREE_MAX_KEYS 32
#define BTREE_MIN_KEYS 16
```

- `BTREE_MAX_KEYS = 32`：每个节点最多 32 个键。满了就要分裂。
- `BTREE_MIN_KEYS = 16`：每个节点最少 16 个键（根除外）。低于就要借位/合并（miniDB 简化版不做）。

```c
typedef int32_t btree_key_t;
```

键类型是 32 位有符号整数。教学简化，真实数据库支持变长键、复合键。

```c
typedef struct {
    page_id_t page_id;   // 4B：行数据所在的堆表页号
    slot_id_t slot_id;   // 2B：页内槽号
} rid_t;
```

RID 是"行数据的地址"。B+Tree 叶子存 `(key, rid)`，通过 rid 去 Heap 取实际行数据。

```c
typedef enum {
    BTREE_INTERNAL = 0,   // 内部节点（路由）
    BTREE_LEAF = 1,       // 叶子节点（存数据）
} node_type_t;
```

节点类型。0 和 1 的取值有讲究：叶子是 1，方便 `if (node_get_type(node))` 判断。

**偏移量定义**：

```c
#define NODE_OFF_TYPE      PAGE_HEADER_SIZE         // 20
#define NODE_OFF_NUM_KEYS  (PAGE_HEADER_SIZE + 1)   // 21
#define NODE_OFF_PARENT    (PAGE_HEADER_SIZE + 3)   // 23
#define NODE_OFF_NEXT_LEAF (PAGE_HEADER_SIZE + 7)   // 27
#define NODE_OFF_KEYS      (PAGE_HEADER_SIZE + 11)  // 31
```

这些宏定义了各字段在 page->data 里的字节偏移。注意 `NODE_OFF_NUM_KEYS = PAGE_HEADER_SIZE + 1` 而不是 `+ 1`，因为 type 占 1 字节，num_keys 从 21 开始占 2 字节，parent 从 23 开始占 4 字节，next_leaf 从 27 开始占 4 字节，keys 从 31 开始。

```c
#define NODE_OFF_CHILDREN (NODE_OFF_KEYS + BTREE_MAX_KEYS * KEY_SIZE)
#define NODE_OFF_RIDS     (NODE_OFF_KEYS + BTREE_MAX_KEYS * KEY_SIZE)
```

children 和 rids 从 keys 之后开始。注意它们偏移相同——因为内节点和叶子节点不会同时有 children 和 rids，共用同一段空间。

### btree_node.c：字节级读写

**大端序读写函数**：

```c
static uint16_t rd_u16(const uint8_t *p) {
    return (uint16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}

static uint32_t rd_u32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8)  |  (uint32_t)p[3];
}
```

为什么用大端序（高位字节在前）？

1. **跨平台一致**：不同 CPU 字节序不同（x86 小端，ARM 可配置），磁盘文件要统一
2. **调试友好**：hexdump 看文件时，大端序的数字肉眼可读
3. **网络序传统**：网络协议都用大端，数据库沿用

`wr_u16` / `wr_u32` 是对应的写入函数，把整数按大端序写进字节数组。

**节点初始化**：

```c
void node_init(page_t *page, node_type_t type, page_id_t parent) {
    memset(page->data + PAGE_HEADER_SIZE, 0, PAGE_SIZE - PAGE_HEADER_SIZE);
    page->data[4] = (uint8_t)PAGE_TYPE_INDEX;       // 标记为索引页
    page->data[NODE_OFF_TYPE] = (uint8_t)type;       // 节点类型
    wr_u16(page->data + NODE_OFF_NUM_KEYS, 0);       // 初始 0 个键
    wr_u32(page->data + NODE_OFF_PARENT, parent);    // 父节点
    wr_u32(page->data + NODE_OFF_NEXT_LEAF, INVALID_PAGE_ID);  // 无下一叶子
}
```

- 先把 page header 之后的部分全清零（keys、children/rids 都清零）
- 设置 page_type = INDEX（章1 的页类型）
- 设置节点类型、初始键数 0、父节点、next_leaf 无效

**键的读写**：

```c
btree_key_t node_get_key(const page_t *page, int idx) {
    return (btree_key_t)rd_u32(page->data + NODE_OFF_KEYS + idx * KEY_SIZE);
}

void node_set_key(page_t *page, int idx, btree_key_t key) {
    wr_u32(page->data + NODE_OFF_KEYS + idx * KEY_SIZE, (uint32_t)key);
}
```

第 idx 个键的偏移 = `NODE_OFF_KEYS + idx * 4`。读写就是在大端序和主机序之间转换。

**children 和 rids 类似**，只是大小不同（child 4B，rid 8B）。

**二分查找**：

```c
int node_find_key(const page_t *page, btree_key_t key) {
    uint16_t n = node_get_num_keys(page);
    int lo = 0, hi = n;
    while (lo < hi) {
        int mid = (lo + hi) / 2;
        if (node_get_key(page, mid) < key) lo = mid + 1;
        else hi = mid;
    }
    return lo;
}
```

这是** lower_bound** 二分查找：返回**第一个 ≥ key 的位置**。

| 场景 | 返回值 | 含义 |
|---|---|---|
| key 比所有键都小 | 0 | 应该插到最前面 / 走 child[0] |
| key 比所有键都大 | n | 应该插到最后面 / 走 child[n] |
| key 等于某个键 | 该键位置 | 找到了 |
| key 介于两键之间 | 后一个键位置 | 插入位置 / 走对应 child |

**这个函数同时服务于三种用途**：

1. **找插入位置**：返回值就是该插入的下标
2. **找子节点**：内节点查找时，返回值就是该走的 child 下标
3. **点查找**：叶子查找时，返回值就是可能匹配的位置，再检查 `keys[idx] == key`

**判断是否满**：

```c
bool node_is_full(const page_t *page) {
    return node_get_num_keys(page) >= BTREE_MAX_KEYS;
}
```

键数 ≥ 32 就是满。满了插入就要分裂。

### btree.h：对外接口

```c
btree_t *btree_create(buffer_pool_t *bp);              // 创建新树
btree_t *btree_open(buffer_pool_t *bp, page_id_t root_pid);  // 打开已有树
void     btree_destroy(btree_t *tree);                 // 销毁

bool btree_insert(btree_t *tree, btree_key_t key, rid_t rid);  // 插入
bool btree_find(btree_t *tree, btree_key_t key, rid_t *rid);   // 点查
bool btree_delete(btree_t *tree, btree_key_t key);             // 删除

btree_cursor_t *btree_range_open(btree_t *tree, btree_key_t start, btree_key_t end);
bool            btree_range_next(btree_cursor_t *cur, btree_key_t *key, rid_t *rid);
void            btree_range_close(btree_cursor_t *cur);

page_id_t btree_root_pid(btree_t *tree);  // 获取根页号
```

接口设计要点：

1. **不暴露 page_t**：外部只拿 `btree_t *`，不直接碰页，封装性好
2. **复用 Buffer Pool**：`btree_create(bp)` 传入 Buffer Pool，所有页操作走 bp
3. **游标模式**：范围查询用 open/next/close 三段式，避免一次返回大量数据

### btree.c：核心实现

**btree_t 结构**：

```c
struct btree {
    buffer_pool_t *bp;      // Buffer Pool 引用
    page_id_t root_pid;     // 根节点页号
};
```

就两个字段：Buffer Pool 和根页号。所有操作从 root_pid 开始。

**find_leaf：找目标叶子**：

```c
static page_id_t find_leaf(buffer_pool_t *bp, page_id_t root, btree_key_t key) {
    page_id_t cur = root;
    for (;;) {
        page_t *node = bp_fetch_page(bp, cur);
        if (node_get_type(node) == BTREE_LEAF) {
            bp_unpin_page(bp, cur, false);   // 叶子：unpin 并返回
            return cur;
        }
        int idx = node_find_key(node, key);  // 内节点：二分找子节点
        page_id_t child = node_get_child(node, idx);
        bp_unpin_page(bp, cur, false);       // unpin 当前节点
        cur = child;                         // 往下走
    }
}
```

这是 B+Tree 查找的"骨架"：从根开始，遇到内节点就二分找子节点往下走，遇到叶子就返回。

**注意 pin/unpin 配对**：每次 `bp_fetch_page` 后必须 `bp_unpin_page`，否则页会一直钉在内存里。这里用 `false` 表示没修改（脏标记=false）。

**leaf_insert_at：叶子内插入**：

```c
static void leaf_insert_at(page_t *leaf, int idx, btree_key_t key, rid_t rid) {
    uint16_t n = node_get_num_keys(leaf);
    for (int i = n; i > idx; i--) {           // 从后往前移
        node_set_key(leaf, i, node_get_key(leaf, i - 1));
        node_set_rid(leaf, i, node_get_rid(leaf, i - 1));
    }
    node_set_key(leaf, idx, key);             // 插入新键
    node_set_rid(leaf, idx, rid);
    node_set_num_keys(leaf, n + 1);           // 键数 +1
}
```

类似数组插入：把 idx 之后的键都往后移一位，腾出 idx 位置放新键。从后往前移是为了避免覆盖。

```
插入前：[10 | 20 | 30]  在 idx=1 插入 15
步骤1：i=3：keys[3]=keys[2]=30 → [10|20|30|30]
步骤2：i=2：keys[2]=keys[1]=20 → [10|20|20|30]
步骤3：keys[1]=15 → [10|15|20|30]
```

**internal_insert_at：内节点插入**：

```c
static void internal_insert_at(page_t *node, int idx, btree_key_t key,
                               page_id_t left_child, page_id_t right_child) {
    uint16_t n = node_get_num_keys(node);
    for (int i = n; i > idx; i--) {
        node_set_key(node, i, node_get_key(node, i - 1));
        node_set_child(node, i + 1, node_get_child(node, i));  // children 也移
    }
    node_set_key(node, idx, key);
    node_set_child(node, idx, left_child);
    node_set_child(node, idx + 1, right_child);
    node_set_num_keys(node, n + 1);
}
```

和 leaf_insert_at 类似，但多处理 children。注意 children 比 keys 多一个（n 个键对应 n+1 个子节点），所以移动时要小心。

**插入一个键 + 两个子节点**：分裂后上推一个键，这个键左边是原节点，右边是新节点。

**split_leaf：叶子分裂**：

```c
static btree_key_t split_leaf(buffer_pool_t *bp, page_id_t leaf_pid,
                              page_id_t *new_pid_out) {
    page_t *leaf = bp_fetch_page(bp, leaf_pid);
    uint16_t n = node_get_num_keys(leaf);
    int mid = n / 2;                              // 中点

    page_t *new_leaf;
    page_id_t new_pid = bp_new_page(bp, &new_leaf);  // 新建页
    node_init(new_leaf, BTREE_LEAF, node_get_parent(leaf));  // 初始化为叶子

    for (int i = mid; i < n; i++) {               // 搬后半部分到新叶子
        node_set_key(new_leaf, i - mid, node_get_key(leaf, i));
        node_set_rid(new_leaf, i - mid, node_get_rid(leaf, i));
    }
    node_set_num_keys(new_leaf, n - mid);         // 新叶子键数
    node_set_num_keys(leaf, mid);                 // 原叶子键数

    node_set_next_leaf(new_leaf, node_get_next_leaf(leaf));  // 链表插入
    node_set_next_leaf(leaf, new_pid);

    btree_key_t up_key = node_get_key(new_leaf, 0);  // 上推键=新叶子第一个键

    bp_unpin_page(bp, leaf_pid, true);            // 脏标记=true（修改了）
    bp_unpin_page(bp, new_pid, true);
    *new_pid_out = new_pid;
    return up_key;
}
```

**关键步骤**：

1. 算中点 `mid = n/2`
2. 新建一个叶子页
3. 把原叶子 `[mid..n)` 的键搬到新叶子
4. 原叶子保留 `[0..mid)` 的键
5. **更新叶子链表**：新叶子.next = 原叶子.next，原叶子.next = 新叶子
6. 上推键 = 新叶子第一个键（**注意是复制，原键还在新叶子里**）

**split_internal：内节点分裂**：

```c
static btree_key_t split_internal(buffer_pool_t *bp, page_id_t node_pid,
                                  page_id_t *new_pid_out) {
    page_t *node = bp_fetch_page(bp, node_pid);
    uint16_t n = node_get_num_keys(node);
    int mid = n / 2;

    page_t *new_node;
    page_id_t new_pid = bp_new_page(bp, &new_node);
    node_init(new_node, BTREE_INTERNAL, node_get_parent(node));

    btree_key_t up_key = node_get_key(node, mid);   // 上推键=中间键（移走！）

    for (int i = mid + 1; i < n; i++) {             // 搬 mid 之后的键
        node_set_key(new_node, i - mid - 1, node_get_key(node, i));
        node_set_child(new_node, i - mid - 1, node_get_child(node, i));
    }
    node_set_child(new_node, n - mid - 1, node_get_child(node, n));  // 最后一个 child
    node_set_num_keys(new_node, n - mid - 1);
    node_set_num_keys(node, mid);                   // 原节点保留前 mid 个

    for (int i = 0; i <= node_get_num_keys(new_node); i++) {  // 更新子节点 parent
        page_id_t cpid = node_get_child(new_node, i);
        page_t *child = bp_fetch_page(bp, cpid);
        node_set_parent(child, new_pid);
        bp_unpin_page(bp, cpid, true);
    }

    bp_unpin_page(bp, node_pid, true);
    bp_unpin_page(bp, new_pid, true);
    *new_pid_out = new_pid;
    return up_key;
}
```

**和叶子分裂的区别**：

1. 上推键是 `keys[mid]`，且**不留在任何子节点**（搬的时候从 mid+1 开始）
2. 要更新新节点所有子节点的 parent 指针（叶子分裂没这步）
3. 不涉及 next_leaf 链表

**insert_in_parent：分裂后插入父节点**：

```c
static void insert_in_parent(buffer_pool_t *bp, page_id_t left_pid,
                             btree_key_t key, page_id_t right_pid,
                             page_id_t *root_ptr) {
    page_t *left = bp_fetch_page(bp, left_pid);
    page_id_t parent_pid = node_get_parent(left);
    bp_unpin_page(bp, left_pid, false);

    if (parent_pid == INVALID_PAGE_ID) {            // 根分裂：创建新根
        page_t *new_root;
        page_id_t new_root_pid = bp_new_page(bp, &new_root);
        node_init(new_root, BTREE_INTERNAL, INVALID_PAGE_ID);
        node_set_num_keys(new_root, 1);
        node_set_key(new_root, 0, key);
        node_set_child(new_root, 0, left_pid);
        node_set_child(new_root, 1, right_pid);
        // ... 更新 left/right 的 parent ...
        *root_ptr = new_root_pid;                   // 更新根指针
        return;
    }

    page_t *parent = bp_fetch_page(bp, parent_pid);
    int idx = node_find_key(parent, key);
    internal_insert_at(parent, idx, key, left_pid, right_pid);  // 插入父节点

    bool need_split = node_is_full(parent);
    bp_unpin_page(bp, parent_pid, true);

    if (need_split) {                               // 父节点也满了 → 递归
        page_id_t new_internal_pid;
        btree_key_t up_key = split_internal(bp, parent_pid, &new_internal_pid);
        insert_in_parent(bp, parent_pid, up_key, new_internal_pid, root_ptr);
    }
}
```

**两种情况**：

1. **根分裂**（parent == INVALID）：创建新根，新根只有 1 个键、2 个子节点，更新 `*root_ptr`
2. **非根分裂**：把上推键插入父节点，如果父节点也满了，递归分裂

**btree_insert：插入主函数**：

```c
bool btree_insert(btree_t *tree, btree_key_t key, rid_t rid) {
    page_id_t leaf_pid = find_leaf(tree->bp, tree->root_pid, key);  // 1. 找叶子
    page_t *leaf = bp_fetch_page(tree->bp, leaf_pid);

    int idx = node_find_key(leaf, key);                             // 2. 找位置
    uint16_t n = node_get_num_keys(leaf);
    if (idx < n && node_get_key(leaf, idx) == key) {                // 3. 已存在
        bp_unpin_page(tree->bp, leaf_pid, false);
        return false;
    }

    if (!node_is_full(leaf)) {                                      // 4. 没满：直接插
        leaf_insert_at(leaf, idx, key, rid);
        bp_unpin_page(tree->bp, leaf_pid, true);
        return true;
    }

    bp_unpin_page(tree->bp, leaf_pid, false);                       // 5. 满了：先插再分裂

    page_t *leaf2 = bp_fetch_page(tree->bp, leaf_pid);
    leaf_insert_at(leaf2, idx, key, rid);
    bp_unpin_page(tree->bp, leaf_pid, true);

    page_id_t new_leaf_pid;
    btree_key_t up_key = split_leaf(tree->bp, leaf_pid, &new_leaf_pid);  // 6. 分裂
    insert_in_parent(tree->bp, leaf_pid, up_key, new_leaf_pid, &tree->root_pid);  // 7. 上推
    return true;
}
```

**注意第 5 步的 unpin+fetch**：先 unpin（false），再 fetch。这是因为 `split_leaf` 内部也会 fetch 同一个页，如果一直 pin 着可能死锁（取决于 Buffer Pool 实现）。unpin 再 fetch 是安全做法。

**btree_find：点查找**：

```c
bool btree_find(btree_t *tree, btree_key_t key, rid_t *rid) {
    page_id_t leaf_pid = find_leaf(tree->bp, tree->root_pid, key);  // 找叶子
    page_t *leaf = bp_fetch_page(tree->bp, leaf_pid);

    int idx = node_find_key(leaf, key);                             // 二分找
    uint16_t n = node_get_num_keys(leaf);
    bool found = false;
    if (idx < n && node_get_key(leaf, idx) == key) {                // 命中
        if (rid) *rid = node_get_rid(leaf, idx);
        found = true;
    }
    bp_unpin_page(tree->bp, leaf_pid, false);
    return found;
}
```

简洁明了：找叶子 → 二分找 → 检查是否精确匹配。`node_find_key` 返回的是 lower_bound，可能 `keys[idx] != key`，所以要再检查一次。

**btree_range_open / next / close：范围查询**：

```c
btree_cursor_t *btree_range_open(btree_t *tree, btree_key_t start, btree_key_t end) {
    page_id_t leaf_pid = find_leaf(tree->bp, tree->root_pid, start);  // 找起点叶子
    page_t *leaf = bp_fetch_page(tree->bp, leaf_pid);
    int idx = node_find_key(leaf, start);    // 起点在叶子内的位置
    int n = node_get_num_keys(leaf);
    bp_unpin_page(tree->bp, leaf_pid, false);

    btree_cursor_t *cur = malloc(sizeof(btree_cursor_t));
    cur->bp = tree->bp;
    cur->leaf_pid = leaf_pid;
    cur->idx = idx;
    cur->num_keys = n;
    cur->end = end;
    cur->done = false;
    return cur;
}
```

open 阶段：找到 ≥ start 的第一个位置，初始化游标。注意这里不持有页的 pin，每次 next 时临时 fetch。

```c
bool btree_range_next(btree_cursor_t *cur, btree_key_t *key, rid_t *rid) {
    if (cur->done) return false;

    for (;;) {
        if (cur->idx >= cur->num_keys) {                  // 当前叶子扫完
            page_t *leaf = bp_fetch_page(cur->bp, cur->leaf_pid);
            page_id_t next = node_get_next_leaf(leaf);    // 走链表
            bp_unpin_page(cur->bp, cur->leaf_pid, false);

            if (next == INVALID_PAGE_ID) {                // 没有下一个叶子
                cur->done = true;
                return false;
            }
            cur->leaf_pid = next;
            leaf = bp_fetch_page(cur->bp, next);
            cur->idx = 0;
            cur->num_keys = node_get_num_keys(leaf);
            bp_unpin_page(cur->bp, next, false);
        }

        page_t *leaf = bp_fetch_page(cur->bp, cur->leaf_pid);
        btree_key_t k = node_get_key(leaf, cur->idx);     // 取当前键
        rid_t r = node_get_rid(leaf, cur->idx);
        bp_unpin_page(cur->bp, cur->leaf_pid, false);

        if (k > cur->end) {                               // 超过右端点
            cur->done = true;
            return false;
        }

        cur->idx++;
        if (key) *key = k;
        if (rid) *rid = r;
        return true;
    }
}
```

next 阶段：

1. 如果当前叶子扫完（idx ≥ num_keys），走 next_leaf 到下一个叶子
2. 如果没有下一个叶子（next == INVALID），结束
3. 取当前键，如果 > end，结束
4. 否则返回当前 (key, rid)，idx++

**注意每次 next 都 fetch+unpin**：不长期持有页，避免占着 Buffer Pool 不放。如果范围查询结果很多，这种"按需 fetch"对其他查询更友好。

---

## B+Tree 在数据库中的角色

### 聚簇索引 vs 非聚簇索引

B+Tree 在数据库里有两种用法，区别在于**叶子节点存什么**：

**聚簇索引（Clustered Index）**：

```
聚簇索引的叶子直接存整行数据：
┌──────────────────────────────────┐
│ key=1 │ row=(1, "Alice", 25)    │
│ key=2 │ row=(2, "Bob", 30)      │
│ key=3 │ row=(3, "Carol", 28)    │
└──────────────────────────────────┘
        ↑ 叶子就是表数据本身
        ↑ 行数据按主键顺序物理存储
```

**非聚簇索引（Secondary Index）**：

```
非聚簇索引的叶子存 (key, RID)：
┌──────────────────────────────────┐
│ key="Alice" │ RID=(page=5, slot=2) │
│ key="Bob"   │ RID=(page=3, slot=7) │
│ key="Carol" │ RID=(page=5, slot=1) │
└──────────────────────────────────┘
        ↑ 叶子只存键 + 指向行数据的指针
        ↑ 要取整行数据，还要用 RID 去 Heap 读一次
```

**对比**：

| 特性 | 聚簇索引 | 非聚簇索引 |
|---|---|---|
| 叶子存什么 | 整行数据 | (key, RID) |
| 一张表有几个 | 1 个 | 多个 |
| 查询速度 | 快（一次 B+Tree 查找） | 慢一点（查找 + 回表） |
| 插入顺序 | 按主键有序插入最快 | 任意顺序 |
| 主键选择 | 自增 ID 最佳 | 无要求 |

**回表（Bookmark Lookup）**：

用非聚簇索引查到 RID 后，还要去 Heap 取整行数据，这叫"回表"。

```sql
SELECT * FROM users WHERE name = 'Alice';
```

```
1. 用 idx_name（非聚簇索引）查 name='Alice'
   → B+Tree 查找 → 叶子得到 RID=(page=5, slot=2)
2. 用 RID 回表
   → 读 page 5，slot 2 → 得到整行 (1, 'Alice', 25)
```

**覆盖索引（Covering Index）**：

如果查询的列都在索引里，就不用回表：

```sql
SELECT name FROM users WHERE name = 'Alice';
```

```
idx_name 的叶子已经有 name，直接返回，不用回表
→ 这叫"覆盖索引"，查询超快
```

### miniDB 的索引是什么类型

miniDB 的 B+Tree 叶子存 `(key, RID)`，所以是**非聚簇索引**。表数据存在 Heap 里（章4），索引通过 RID 指向 Heap。

```
miniDB 的存储结构：

    B+Tree 索引                    Heap 表数据
    ┌──────────┐                  ┌────────────────────┐
    │ key=1 → RID=(5,2) │  ───→   │ page 5, slot 2:    │
    │ key=2 → RID=(3,7) │  ───→   │   (1, "Alice", 25) │
    │ key=3 → RID=(5,1) │  ───→   │ page 3, slot 7:    │
    └──────────┘                  │   (2, "Bob", 30)   │
                                  │ page 5, slot 1:    │
                                  │   (3, "Carol", 28) │
                                  └────────────────────┘
```

这种设计简单清晰：索引和数据分离，各自管理。MySQL InnoDB 的聚簇索引更复杂——叶子直接存行数据，二级索引的叶子存主键值（不是 RID），要查两次 B+Tree。

### 索引的选择性

不是所有列都适合建索引。**选择性**（Selectivity）= 不同值数量 / 总行数。

| 选择性 | 例子 | 适合索引？ |
|---|---|---|
| 高（接近 1） | 主键、身份证号 | 非常适合 |
| 中 | 姓名、年龄 | 适合 |
| 低 | 性别、状态 | 不适合 |
| 极低（1 种值） | 布尔标志 | 不适合 |

**为什么低选择性不适合**：

```
性别索引（男/女）：
    男 → [RID1, RID2, RID3, ... RID500万]   ← 一半数据
    女 → [RID500万+1, ... RID1000万]        ← 另一半

SELECT * FROM users WHERE gender = '男';
→ 索引返回 500 万个 RID
→ 500 万次回表
→ 比全表扫描还慢！
```

**经验法则**：选择性 > 30% 才考虑建索引。

---

## 与真实数据库对比

### miniDB vs SQLite vs PostgreSQL vs MySQL

| 特性 | miniDB | SQLite | PostgreSQL | MySQL InnoDB |
|---|---|---|---|---|
| 索引结构 | B+Tree | B-Tree | B-Tree（变种） | B+Tree |
| 页大小 | 4 KB | 4 KB | 8 KB | 16 KB |
| 阶数 | 固定 32 | 动态计算 | 动态计算 | 动态计算 |
| 键类型 | int32 | 任意类型 | 任意类型 | 任意类型 |
| 聚簇索引 | 无 | 无（rowid 近似） | 无（堆表为主） | 有（主键即聚簇） |
| 叶子链表 | 有 | 有 | 有 | 有（双向） |
| 删除策略 | 简化（不合并） | 借位+合并 | 标记+VACUUM | 借位+合并 |
| 事务支持 | 无 | WAL/rollback | MVCC | MVCC |
| 变长键 | 不支持 | 支持 | 支持 | 支持 |
| 复合键 | 不支持 | 支持 | 支持 | 支持 |

### SQLite 的 B-Tree

SQLite 用的是**B-Tree**（不是 B+Tree），内节点也存数据。但它的页大小 4KB，键通常很小（rowid 是 64 位整数），所以内节点也能有几百的扇出。

```
SQLite B-Tree 节点：
┌─────────────────────────────────────┐
│ key1 │ payload1 │ key2 │ payload2 │ │   ← 内节点也存 payload（行数据）
│ child1 │ child2 │ child3 │          │
└─────────────────────────────────────┘
```

SQLite 选 B-Tree 的原因：

1. 实现简单（一个结构搞定，不用区分聚簇/非聚簇）
2. 单文件数据库，页管理简单
3. 嵌入式场景，数据量通常不大

### PostgreSQL 的 B-Tree

PostgreSQL 的 B-Tree 是**B-Tree 的变种**，但叶子有链表（所以很多特性像 B+Tree）。它的特点是：

1. **堆表 + 索引分离**：表数据存 Heap，索引存 TID（类似 RID）
2. **MVCC 多版本**：同一行可能有多个版本，索引指向最新版本
3. **VACUUM 清理**：删除是标记删除，VACUUM 阶段真正回收空间
4. **页大小 8KB**：比 miniDB 大一倍，扇出更大

```
PostgreSQL 的存储：
    索引（B-Tree）              堆表（Heap）
    ┌──────────┐              ┌──────────────┐
    │ key → TID │  ─────────→ │ tuple (行)   │
    │ key → TID │  ─────────→ │ tuple (行)   │
    └──────────┘              └──────────────┘
    TID = (block, offset)
```

### MySQL InnoDB 的 B+Tree

InnoDB 是最经典的 B+Tree 实现：

1. **聚簇索引**：主键的 B+Tree 叶子直接存整行数据
2. **二级索引存主键值**：二级索引叶子存 (索引键, 主键值)，不是 RID
3. **页大小 16KB**：扇出大，树矮
4. **双向链表**：叶子节点有 prev 和 next，支持反向扫描
5. **页合并**：删除时自动合并

```
InnoDB 的聚簇索引（主键索引）：
    ┌──────────────────────────────┐
    │ pk=1 │ row=(1, "Alice", 25) │   ← 叶子直接存行
    │ pk=2 │ row=(2, "Bob", 30)   │
    └──────────────────────────────┘

InnoDB 的二级索引（name 索引）：
    ┌──────────────────────────┐
    │ name="Alice" │ pk=1      │   ← 叶子存主键值，不是 RID
    │ name="Bob"   │ pk=2      │
    └──────────────────────────┘
    
    查询 SELECT * FROM users WHERE name='Alice':
    1. 查 idx_name → 得到 pk=1
    2. 查聚簇索引 → 得到整行 (1, "Alice", 25)
    → 两次 B+Tree 查找（除非覆盖索引）
```

**为什么二级索引存主键而不是 RID**：

| 方案 | 优点 | 缺点 |
|---|---|---|
| 存 RID（miniDB/PG） | 回表一次 IO | 行移动时索引要更新 |
| 存主键（InnoDB） | 行移动时索引不用更新 | 回表要再查一次 B+Tree |

InnoDB 选主键方案：聚簇索引的行可能因为页分裂而移动，如果二级索引存 RID，每次行移动都要更新所有二级索引，太慢。存主键则不用动，代价是回表多一次 B+Tree 查找。

### miniDB 的设计取舍

miniDB 的 B+Tree 是**教学最优**而非**性能最优**：

| 决策 | miniDB 的选择 | 真实数据库的选择 | miniDB 的理由 |
|---|---|---|---|
| 阶数 | 固定 32 | 动态计算 | 教学清晰 |
| 键类型 | int32 | 任意类型 | 简化 |
| 删除 | 不合并 | 借位+合并 | 控制代码量 |
| 聚簇索引 | 无 | InnoDB 有 | 简化（Heap + RID 即可） |
| 事务 | 无 | 都有 | 章5 再加 |
| 变长键 | 不支持 | 支持 | 简化 |
| 双向链表 | 单向 | InnoDB 双向 | 简化 |

---

## 使用示例

完整的使用示例：创建索引、插入、点查、范围查。

```c
#include "btree.h"
#include "buffer_pool.h"
#include "pager.h"

int main() {
    /* 1. 准备存储：pager + buffer pool + btree */
    pager_t *pager = pager_open("index.db");
    buffer_pool_t *bp = bp_create(pager, 64, REPLACER_LRU);
    btree_t *tree = btree_create(bp);

    /* 2. 插入 1000 条索引项 */
    for (int i = 1; i <= 1000; i++) {
        rid_t rid = { .page_id = i / 10, .slot_id = i % 10 };
        btree_insert(tree, i, rid);
    }
    printf("插入完成，根页号 = %u\n", btree_root_pid(tree));

    /* 3. 点查 */
    rid_t rid;
    if (btree_find(tree, 42, &rid)) {
        printf("找到 key=42: page=%u slot=%u\n", rid.page_id, rid.slot_id);
    } else {
        printf("没找到 key=42\n");
    }

    /* 4. 范围查 100~200 */
    btree_cursor_t *cur = btree_range_open(tree, 100, 200);
    btree_key_t key;
    int count = 0;
    while (btree_range_next(cur, &key, &rid)) {
        if (count < 5) {  // 只打印前5条
            printf("  key=%d -> page=%u slot=%u\n", key, rid.page_id, rid.slot_id);
        }
        count++;
    }
    printf("范围 100~200 共 %d 条\n", count);
    btree_range_close(cur);

    /* 5. 删除 */
    btree_delete(tree, 42);
    if (btree_find(tree, 42, &rid)) {
        printf("删除失败？还能找到 42\n");
    } else {
        printf("删除成功，42 已不存在\n");
    }

    /* 6. 清理 */
    btree_destroy(tree);
    bp_destroy(bp);
    pager_close(pager);
    return 0;
}
```

**预期输出**：

```
插入完成，根页号 = 1
找到 key=42: page=4 slot=2
  key=100 -> page=10 slot=0
  key=101 -> page=10 slot=1
  key=102 -> page=10 slot=2
  key=103 -> page=10 slot=3
  key=104 -> page=10 slot=4
范围 100~200 共 101 条
删除成功，42 已不存在
```

---

## 设计决策汇总

| 决策 | 选择 | 理由 | 代价 |
|---|---|---|---|
| 索引结构 | B+Tree | 磁盘友好、范围查询快 | 实现比 BST 复杂 |
| 键类型 | int32_t | 教学简化 | 不支持字符串/复合键 |
| 阶数 | 固定 32 | 教学清晰 | 页空间利用率低（415B/4KB） |
| 删除 | 简化（不合并） | 控制代码量 | 树可能不平衡 |
| 节点存储 | 复用 page_t | 一节点一页，复用 Buffer Pool | 和页管理耦合 |
| 叶子链表 | next_leaf 单向 | 支持范围扫描 | 不支持反向扫描 |
| 字节序 | 大端 | 跨平台一致 | 读写要转换 |
| 分裂策略 | 从中间分裂 | 简单、保证平衡 | 不是最优（真实DB有更聪明的分裂） |
| 重复键 | 不允许 | 简化 | 真实DB要支持 |
| 父指针 | 有（parent_page_id） | 分裂时找父节点方便 | 多 4B 空间 |

---

## 常见误区

### 误区1：B+Tree 就是 B-Tree 加个链表

**错**。B+Tree 和 B-Tree 的核心区别是**内节点是否存数据**：

- B-Tree：内节点和叶子都存 (key, data)
- B+Tree：只有叶子存 (key, data)，内节点只存 key

链表是 B+Tree 的附加优势，不是本质区别。本质区别是数据存放位置。

### 误区2：B+Tree 一定比 B-Tree 好

**不一定**。如果数据全在内存（不涉及磁盘 IO），B-Tree 可能更好：

- B-Tree 内节点也存数据，单次查找可能更早命中（不用走到叶子）
- B+Tree 必须走到叶子才能拿到数据

B+Tree 的优势在**磁盘环境**：树矮 + 范围扫描。内存环境两者差不多，甚至 B-Tree 略优。

### 误区3：阶数越大越好

**错**。阶数大 → 扇出大 → 树矮 → IO 少，但：

- 阶数大 → 节点大 → 单次 IO 读的数据多 → 如果节点不满，浪费 IO 带宽
- 阶数大 → 二分查找比较次数多 → CPU 时间增加
- 阶数要和页大小匹配，一页一个节点是最佳

真实数据库的阶数是 `(页大小 - header) / (key + child)` 算出来的，不是越大越好。

### 误区4：索引越多越好

**错**。索引有代价：

- 每个索引占空间（1000 万行的索引可能几百 MB）
- 每次 INSERT/UPDATE/DELETE 都要更新所有索引
- 索引太多 → 写入慢、占内存、优化器选择困难

**经验**：一张表 3~5 个索引是合理的，超过 10 个要反思。

### 误区5：B+Tree 查找是 O(1)

**错**。B+Tree 查找是 O(log n)，只是底数大（m 阶 → log_m n）。100 万数据 log₃₂(100万) ≈ 4，不是 1。但因为每次 IO 10ms，4 次 IO = 40ms，体感像 O(1)。

真正的 O(1) 查找是哈希索引，但哈希不支持范围查询。

---

## 习题

### 基础题

1. **画图**：给定 MAX_KEYS=3，依次插入 5, 3, 7, 1, 4, 6, 8，画出每次插入后的树形图。

2. **概念**：B-Tree 和 B+Tree 的三个本质区别是什么？

3. **计算**：miniDB 的 4KB 页，如果键改为 16 字节的字符串，内节点最多能存多少键？（header 31 字节，child 4 字节）

4. **查找**：在下面的树里查找 key=35，写出每一步走的节点和位置。

```
        [20 | 40 | 60]
       /    |    |    \
  [10] [20|30] [40|50] [60|70|80]
```

### 进阶题

5. **分裂**：叶子分裂和内节点分裂有什么区别？为什么内节点分裂时中间键要移走，而叶子分裂时中间键是复制？

6. **链表**：如果不做叶子链表，范围查询还能实现吗？性能如何？给出伪代码。

7. **删除**：miniDB 的简化删除会导致什么问题？写一个测试用例触发这个问题。

8. **阶数**：如果键类型改为变长字符串，节点布局需要怎么调整？固定阶数还可行吗？

### 思考题

9. **聚簇 vs 非聚簇**：为什么 MySQL InnoDB 的二级索引存主键值而不是 RID？什么场景下存 RID 更好？

10. **根分裂**：B+Tree 的根节点分裂时，树高增加。这对查询性能有什么影响？树高从 3 变 4，查询慢多少？

11. **重复键**：如何支持重复键？（提示：键+RID 组合唯一，或溢出页，或允许叶子有相同键）

12. **前缀压缩**：如何实现"前缀压缩"来增加内节点的扇出？（提示：如果键都是 `user_001`、`user_002`，可以只存 `001`、`002`）

13. **批量插入**：如果要批量插入 100 万条数据，逐条插入会触发很多分裂。如何优化？（提示：先排序，自底向上构建）

14. **Buffer Pool 交互**：btree_insert 里有一段 `unpin(false)` 后立即 `fetch` 再 `unpin(true)`，为什么不直接保持 pin？如果 Buffer Pool 只有 1 个页槽会怎样？

15. **对比真实数据库**：miniDB 的删除不合并，PostgreSQL 用标记删除+VACUUM，MySQL 用即时合并。三种策略各自的优缺点？什么工作负载适合哪种？

---

## 小结

本章从"为什么需要索引"出发，走完了 BST → B-Tree → B+Tree 的演进，详细讲解了 B+Tree 的结构、插入分裂、查找、范围查询、删除，并逐行解读了 miniDB 的实现代码。

**核心要点**：

| 要点 | 一句话 |
|---|---|
| 为什么用 B+Tree | 树矮（IO 少）+ 叶子链表（范围快） |
| 内节点 vs 叶子 | 内节点路由（只存键），叶子存数据（键+RID） |
| 插入 | 找叶子 → 插入 → 满了分裂 → 上推 → 递归 |
| 查找 | 从根走到叶子，二分找 |
| 范围查询 | 找起点 → 顺着叶子链表扫描 |
| 删除 | miniDB 简化：只删不合并 |
| 聚簇 vs 非聚簇 | 聚簇叶子存行，非聚簇叶子存 RID |

**下一章预告**：章4 实现 Heap 表存储，B+Tree 的 RID 就是指向 Heap 里的行。索引 + Heap = 完整的存储引擎。

---

上一章：[章2 Buffer Pool](02-buffer-pool.md) | 下一章：[章4 堆表存储](04-heap.md)
