# 章4：堆表存储

> 表和索引分开存储。堆表存数据，索引指向数据。本章实现完整的表存储层。
>
> **适用读者**：刚学完章1（slotted page）、章2（buffer pool）、章3（B+Tree）的同学。
>
> **本章目标**：搞懂一张"表"在磁盘上到底长什么样、INSERT 的数据是怎么一步步落到页里的、SELECT 又是怎么把数据找回来的。

---

## 目录

- [0. 先建立一个直观印象](#0-先建立一个直观印象)
- [1. 表存储的两种方式：堆表 vs 索引组织表](#1-表存储的两种方式堆表-vs-索引组织表)
- [2. Tuple 布局详解](#2-tuple-布局详解)
- [3. Schema 设计](#3-schema-设计)
- [4. Heap 的页链](#4-heap-的页链)
- [5. Table = Heap + Index](#5-table--heap--index)
- [6. 插入流程：从 SQL INSERT 到页写入](#6-插入流程从-sql-insert-到页写入)
- [7. 查询流程：全表扫描 vs 索引查找](#7-查询流程全表扫描-vs-索引查找)
- [8. 代码逐行解读](#8-代码逐行解读)
- [9. 空间管理：FSM 概念](#9-空间管理fsm-概念)
- [10. 与真实数据库对比](#10-与真实数据库对比)
- [11. 习题](#11-习题)

---

## 0. 先建立一个直观印象

在进入细节之前，先用一句话概括 miniDB 的存储思想：

> **数据按插入顺序堆在"堆表"里；为了能快速查找，再额外建一棵 B+Tree 索引，索引里只存"键 → 数据位置"。**

这就像图书馆的两种找书方式：

```
方式 A（全表扫描）：    从第 1 号书架一直走到最后，逐本翻看 → 慢，但不用目录
方式 B（索引查找）：    先查目录卡片得到"在第 3 排第 7 本"，直接走过去 → 快，但需要目录
```

miniDB 同时提供这两种方式，由 `table_t` 把它们组合起来。下面这张总图先看一眼，不必全懂，本章讲完后再回来看会很清楚：

```
                       ┌──────────────────────────┐
   SQL: INSERT ... ──→ │  table_t                 │
                       │   ├─ heap_t   (堆表)     │  ← 数据真正存放处
                       │   └─ btree_t  (索引)     │  ← 只存 pk → RID
                       └──────────────────────────┘
                                  │
            ┌─────────────────────┼─────────────────────┐
            ▼                                            ▼
   ┌─────────────────┐                       ┌─────────────────┐
   │  Heap (页链表)  │                       │   B+Tree 索引   │
   │  Page0 → Page1  │  ←── 回表取数据 ──── │  pk=1 → RID(0,0)│
   │  → Page2 → ...  │                       │  pk=2 → RID(0,1)│
   └─────────────────┘                       └─────────────────┘
            │
            ▼
   ┌─────────────────────────┐
   │  Buffer Pool (章2)      │  ← 把页缓存到内存
   └─────────────────────────┘
            │
            ▼
   ┌─────────────────────────┐
   │  磁盘文件 (章1 pager)   │
   └─────────────────────────┘
```

带着这张图，开始进入正题。

---

## 1. 表存储的两种方式：堆表 vs 索引组织表

### 1.1 为什么会有"两种方式"？

考虑一个用户表 `users(id, name, age)`，假设主键是 `id`。同一份逻辑数据，物理上可以有两种摆法：

**方式一：堆表（Heap）**

数据按"插入顺序"堆在一起，主键索引单独放一边。

```
堆表（按插入顺序）              主键索引（B+Tree）
┌────────────────────┐         ┌────────────────────┐
│ tuple: (3, Carol)  │         │ key=1 → RID(1,0)   │
│ tuple: (1, Alice)  │         │ key=2 → RID(1,1)   │
│ tuple: (2, Bob)    │         │ key=3 → RID(0,0)   │
└────────────────────┘         └────────────────────┘
   ↑ 数据在这里                   ↑ 索引指向数据位置
```

**方式二：索引组织表（IOT，Index-Organized Table）**

数据本身就按主键顺序存在 B+Tree 的叶子节点里，不再单独建堆表。

```
        B+Tree（既是索引又是数据）
              ┌──────────┐
              │  内部节点 │
              └────┬─────┘
        ┌──────────┼──────────┐
        ▼          ▼          ▼
   ┌─────────┐ ┌─────────┐ ┌─────────┐
   │(1,Alice)│ │(2,Bob)  │ │(3,Carol)│   ← 叶子节点直接存完整 tuple
   │(1,Bob)  │ │(2,...)  │ │(3,...)  │
   └─────────┘ └─────────┘ └─────────┘
```

### 1.2 两种方式的对比

| 维度             | 堆表 (Heap)                | 索引组织表 (IOT)            |
|------------------|----------------------------|------------------------------|
| 数据存放位置     | 独立的堆页                | B+Tree 叶子节点             |
| 按主键查找       | 索引 → RID → 回表          | 直接在 B+Tree 中找到         |
| 按插入顺序扫描   | 快（页链表顺序读）         | 慢（要按主键顺序遍历）       |
| 二级索引         | 简单：指向 RID             | 复杂：要存主键（间接寻址）   |
| 空间利用率       | 高（插入紧凑）             | 中（B+Tree 需要分裂留空）    |
| 范围查询（主键） | 中（索引扫 + 回表）        | 快（叶子链表顺序读）         |
| 更新非主键列     | 索引不用动                 | 可能引起 B+Tree 调整         |
| 实现复杂度       | 低                         | 中                           |
| 代表数据库       | PostgreSQL、miniDB         | MySQL InnoDB、SQLite         |

### 1.3 miniDB 的选择

miniDB 采用**堆表方式**，理由有三：

1. **教学清晰**：数据和索引职责分离，便于分章讲解（章3 讲 B+Tree，章4 讲堆表）。
2. **复用前章**：章1 的 slotted page、章2 的 buffer pool 直接拿来装堆表即可。
3. **贴近 PostgreSQL**：让读者学完后能平滑过渡到真实数据库的设计思想。

### 1.4 一个生活类比

```
堆表  ≈  仓库里按到货顺序堆放的箱子
        +  一本"编号→箱位"的账本（索引）

IOT   ≈  仓库里箱子本身就按编号排好
        不需要额外账本，但到货时要找位置插入
```

如果经常"按编号范围取货"，IOT 更方便；如果经常"统计最近到货的货"，堆表更方便。miniDB 选了后者。

---

## 2. Tuple 布局详解

### 2.1 什么是 Tuple？

**Tuple（元组）** 就是一"行"数据在内存/磁盘上的物理表示。逻辑上一行是 `(id=1, name=Alice, age=20)`，物理上它是一段连续的字节。

```
逻辑行：  id=1, age=20, score=95.5
                │
                ▼  序列化
物理 tuple：  [01 00 00 00] [14 00 00 00] [00 00 BF 42]   ← 12 字节
              └─ id ─┘     └─ age ─┘    └─ score ─┘
```

### 2.2 miniDB 的 Tuple 布局

miniDB 的 tuple 由两部分组成：**NULL 位图 + 定长数据区**。

```
┌─────────────────┬───────────────────────────────────────┐
│   NULL bitmap   │   col0 数据 │ col1 数据 │ col2 数据 │ ...
└─────────────────┴───────────────────────────────────────┘
  ↑ ceil(列数/8) 字节      ↑ 每列按 schema 类型定长
```

具体到 `users(id INT32, age INT32, score FLOAT)` 这个 schema：

```
┌──────────┬────────┬────────┬────────┐
│ null_bm  │  id    │  age   │ score  │
│ 1 byte   │ 4 byte │ 4 byte │ 4 byte │
└──────────┴────────┴────────┴────────┘
   偏移 0     偏移 1    偏移 5    偏移 9    总长 13 字节
```

- `null_bm`：1 字节，3 列只用 3 bit，剩余 5 bit 留空。
- `id`：INT32，4 字节，偏移 1。
- `age`：INT32，4 字节，偏移 5。
- `score`：FLOAT，4 字节，偏移 9。
- 总长 = 1 + 4 + 4 + 4 = 13 字节。

### 2.3 NULL 位图原理图解

NULL 位图的作用：**用 1 个 bit 表示一列是否为 NULL，节省"另存一个 NULL 标记"的开销。**

```
位图字节：  bit7  bit6  bit5  bit4  bit3  bit2  bit1  bit0
            ────  ────  ────  ────  ────  ────  ────  ────
            未用  未用  未用  未用  未用  col2 col1 col0
            0     0     0     0     0     0    1    0

含义：col0 非 NULL，col1 是 NULL，col2 非 NULL
```

**判断第 col 列是否为 NULL 的位运算：**

```c
// 读：取第 col 位
bool is_null = (bitmap[col / 8] >> (col % 8)) & 1;

// 写：把第 col 位置 1（标记为 NULL）
bitmap[col / 8] |= (1 << (col % 8));

// 清：把第 col 位置 0（标记为非 NULL）
bitmap[col / 8] &= ~(1 << (col % 8));
```

举例：3 列的 schema，`col=1`（age 列）：

```
col / 8 = 1 / 8 = 0   → 操作第 0 个字节
col % 8 = 1 % 8 = 1   → 操作第 1 个 bit

置 1：  bitmap[0] |= 0b00000010   →  bitmap[0] = 0b00000010
读：   (0b00000010 >> 1) & 1 = 1  →  是 NULL
```

### 2.4 定长 vs 变长字段

miniDB 目前**只支持定长字段**（INT32/INT64/FLOAT），这让 tuple 布局非常简单：

```
定长布局（miniDB 当前）：
┌─────────┬───────┬───────┬───────┐
│ null_bm │ col0  │ col1  │ col2  │   ← 每列位置固定，直接算偏移
└─────────┴───────┴───────┴───────┘
```

如果要支持变长字段（如 VARCHAR），布局会复杂得多：

```
变长布局（真实数据库的做法）：
┌─────────┬──────────────┬──────────────────────────┐
│ null_bm │ 定长列数据区  │ 变长列数据区              │
│         │ col0 col1 ...│ "Alice" "Beijing" ...    │
└─────────┴──────────────┴──────────────────────────┘
                         ↑
            还需要一个"变长偏移表"来定位每列起始位置：

更真实的布局（PostgreSQL 风格）：
┌────────┬──────────┬──────────────┬──────────────┐
│ header │ var-off  │ 定长列数据   │ 变长列数据    │
│ 23B    │ 表       │ (从后往前)   │ (从前往后)    │
└────────┴──────────┴──────────────┴──────────────┘
```

变长字段为什么难？

1. **偏移不固定**：不能像定长那样 `offset = null_bm_size + sum(prev_col_size)`，要查偏移表。
2. **页内空间碎片**：一个 tuple 更新后变长，原位置放不下，要"挪走"或用 TOAST。
3. **更新策略**：PostgreSQL 的 MVCC 甚至会留下"旧版本 tuple"在页里，更复杂。

miniDB 刻意避开这些复杂度，让初学者先理解"定长 tuple + NULL 位图"这个最核心的骨架。

### 2.5 一个完整的 Tuple 示例

假设 schema = `users(id INT32, age INT32, score FLOAT)`，要存一行 `id=42, age=NULL, score=95.5`：

```
步骤 1：分配 13 字节，全清零
        [00 00 00 00 00 00 00 00 00 00 00 00 00]

步骤 2：写 id=42（小端）
        42 = 0x2A → 字节 [2A 00 00 00]
        同时清 col0 的 NULL 位（已是 0）
        [00 2A 00 00 00 00 00 00 00 00 00 00 00]

步骤 3：age 设为 NULL → 置 col1 位
        bitmap[0] |= 0b00000010 = 0x02
        [02 2A 00 00 00 00 00 00 00 00 00 00 00]

步骤 4：写 score=95.5（IEEE 754 float）
        95.5 → 0x42BE0000 → 字节 [00 00 BE 42]
        同时清 col2 的 NULL 位
        [02 2A 00 00 00 00 00 00 00 00 00 BE 42]

最终：  13 字节，age 列的 4 字节是"占位垃圾"，读取时会被 NULL 位图跳过
```

### 2.6 序列化与反序列化

由于 miniDB 的 tuple 是一段连续字节，序列化就是直接 `memcpy`：

```c
// 序列化：tuple → 字节流
uint16_t tuple_serialize(const tuple_t *t, uint8_t *buf) {
    memcpy(buf, t->data, t->length);
    return t->length;
}

// 反序列化：字节流 → tuple
void tuple_deserialize(tuple_t *t, const uint8_t *buf, uint16_t len) {
    memcpy(t->data, buf, len);
    t->length = len;
}
```

这种"零拷贝式"序列化只有在**定长 + 无指针**布局下才可能。变长字段需要逐列解析。

---

## 3. Schema 设计

### 3.1 Schema 是什么？

**Schema（模式）** 描述一张表的"形状"：表名、有几列、每列叫什么、什么类型、能否为 NULL。

```
schema_t {
    name = "users"
    num_cols = 3
    cols[0] = { name="id",    type=INT32, nullable=false }
    cols[1] = { name="age",   type=INT32, nullable=true  }
    cols[2] = { name="score", type=FLOAT,  nullable=true  }
}
```

Schema 是 tuple 的"说明书"：没有 schema，你拿到一段字节也不知道每列从哪里切。

### 3.2 miniDB 支持的列类型

```c
typedef enum {
    COL_INT32 = 0,   // 4 字节有符号整数
    COL_INT64 = 1,   // 8 字节有符号整数
    COL_FLOAT  = 2,   // 4 字节单精度浮点
} col_type_t;
```

| 类型      | 字节数 | 取值范围                       | 用途                       |
|-----------|--------|--------------------------------|----------------------------|
| INT32     | 4      | -2^31 ~ 2^31-1（约 ±21 亿）    | 主键、计数、小整数         |
| INT64     | 8      | -2^63 ~ 2^63-1                 | 大计数、时间戳、大主键     |
| FLOAT     | 4      | ±3.4e38（6-7 位有效数字）      | 评分、比例、科学计算       |

### 3.3 为什么 miniDB 只支持这三种类型？

这是一个**刻意的教学简化**，原因有四：

**原因 1：定长简化布局**

三种类型都是定长的（4 或 8 字节）。定长意味着：

- 列偏移可以**编译期算好**（实际是 O(列数) 算一次）。
- tuple 总长固定，页内 slot 管理简单。
- 不需要变长偏移表、TOAST、行迁移等机制。

```
定长：  offset(col) = null_bm_size + sum(col_size[0..col-1])   ← 一次加法
变长：  offset(col) = 查 var_offset_table[col]                  ← 多一次访存
```

**原因 2：覆盖基本需求**

INT32 + INT64 + FLOAT 足以表达"整数、大整数、小数"三类核心数据，足够演示一个数据库的完整链路。

**原因 3：避免类型转换地狱**

如果支持 VARCHAR/DATE/DECIMAL/BLOB，就要处理：

- 字符集编码（UTF-8 vs GBK）
- 日期格式与时区
- 高精度十进制运算
- 大对象分页存储

每一项都是一个独立大坑，与"学数据库存储原理"的目标无关。

**原因 4：对齐友好**

4 和 8 都是 2 的幂，内存对齐天然满足，不会因为对齐填充浪费空间。

### 3.4 如何扩展更多类型？

如果将来要加 `COL_STRING`，需要改动的地方：

```
1. schema.h:  在 col_type_t 枚举里加 COL_STRING
2. schema.c:  schema_col_size() 对 COL_STRING 返回 ... 嗯，定长还是变长？
              - 若定长（如 CHAR(32)）：返回 32，简单
              - 若变长（VARCHAR）：需要改 tuple 布局，引入偏移表
3. tuple.c:   增加 tuple_set_string / tuple_get_string
4. SQL 层:    支持 'xxx' 字面量、字符串比较
5. 显示层:   打印时按字符串而非数字
```

可见"加一个定长类型"很容易，"加一个变长类型"则牵一发动全身。

### 3.5 Schema 的关键函数

```c
// 初始化一个空 schema
void schema_init(schema_t *s, const char *name);

// 添加一列，返回列号（0-based），失败返回 -1
int schema_add_col(schema_t *s, const char *name, col_type_t type, bool nullable);

// 某类型占多少字节
uint16_t schema_col_size(col_type_t type);

// NULL 位图占多少字节 = ceil(列数 / 8)
uint16_t schema_null_bm_size(const schema_t *s);

// 整个 tuple 占多少字节 = null_bm_size + 各列 size 之和
uint16_t schema_tuple_size(const schema_t *s);

// 按列名找列号，找不到返回 -1
int schema_find_col(const schema_t *s, const char *name);
```

### 3.6 限制

miniDB 的 schema 有几个硬编码限制：

| 限制            | 值     | 定义位置          | 含义                       |
|-----------------|--------|-------------------|----------------------------|
| MAX_COLS        | 16     | schema.h:7        | 一张表最多 16 列            |
| MAX_NAME_LEN    | 32     | schema.h:8        | 表名/列名最多 32 字符       |

这些限制是为了：

- `cols[MAX_COLS]` 是定长数组，避免动态内存管理。
- 32 字符足以表达教学用表名。
- 真实数据库的限制：PostgreSQL 最多 1600 列，MySQL 最多 4096 列。

---

## 4. Heap 的页链

### 4.1 Heap 是什么？

**Heap（堆表）** 是一张表所有 tuple 的物理集合。它由一组**页（page）**组成，页之间用**链表**串起来。

```
heap_t {
    bp          → buffer pool（章2）
    schema      → 表的 schema
    first_page  → 第一个页的 id
    last_page   → 最后一个页的 id（插入用）
}
```

为什么需要 `first_page` 和 `last_page` 两个指针？

- `first_page`：顺序扫描的起点。
- `last_page`：插入的落点（新 tuple 优先尝试放最后一页）。

### 4.2 页链表结构

每个堆页的 header 里有一个 `next_page_id` 字段，指向链表的下一个页：

```
   first_page                              last_page
       │                                        │
       ▼                                        ▼
   ┌────────┐    next    ┌────────┐    next    ┌────────┐
   │ Page 0 │ ─────────→ │ Page 1 │ ─────────→ │ Page 2 │ ──→ INVALID
   │ tuples │            │ tuples │            │ tuples │
   │  ...   │            │  ...   │            │  ...   │
   └────────┘            └────────┘            └────────┘
```

`INVALID_PAGE_ID`（通常是 -1 或 0xFFFFFFFF）表示链表结束。

### 4.3 页的增长策略

当 `last_page` 放不下新 tuple 时，heap 会：

1. 向 buffer pool 申请一个新页。
2. 把旧 `last_page` 的 `next_page_id` 指向新页。
3. 更新 `heap->last_page = 新页`。
4. 把 tuple 放进新页。

```
插入前：
   ┌────────┐         ┌────────┐
   │ Page 0 │ ──────→ │ Page 1 │ ──→ INVALID   ← last_page，已满
   │ (满)   │         │ (满)   │
   └────────┘         └────────┘

插入时申请新页 Page 2，把 Page 1 的 next 指向它：

   ┌────────┐         ┌────────┐         ┌────────┐
   │ Page 0 │ ──────→ │ Page 1 │ ──────→ │ Page 2 │ ──→ INVALID
   │ (满)   │         │ (满)   │         │ 新tuple│
   └────────┘         └────────┘         └────────┘
                                          ↑ 新的 last_page
```

### 4.4 为什么用链表而不是数组？

**如果用数组（连续页号）**：

- 优点：`page_id = first_page + i`，随机访问第 i 页 O(1)。
- 缺点：表增长时要预分配或重新整理；删除某页后中间留洞。

**用链表**：

- 优点：增长只需追加一页，不用整理；删除/复用页灵活。
- 缺点：访问第 i 页要 O(i) 遍历；但堆表本来主要就是顺序扫描，无所谓。

miniDB 选链表，因为堆表的典型操作是**顺序扫描**和**按 RID 随机取页**（RID 直接含 page_id，不用遍历链表）。

### 4.5 页链表与 RID 的关系

**RID（Row ID）** = `(page_id, slot_id)`，是 tuple 在堆表中的全局地址。

```
RID = (page_id=1, slot_id=2)
       │            │
       │            └── 在该页内的 slot 编号
       └── 页号（可直接定位，不用走链表）
```

所以"按 RID 取 tuple"是 O(1) 的：直接 `bp_fetch_page(page_id)` → `page_get_tuple(slot_id)`。链表只用于顺序扫描。

### 4.6 顺序扫描如何走链表

```
heap_scan_open(heap):
    cur_page = heap->first_page
    cur_slot = 0

heap_scan_next(scan):
    loop:
        page = fetch(scan->cur_page)
        while scan->cur_slot < page.num_slots:
            if slot 未被删除:
                返回 (cur_page, cur_slot) 的 tuple
            scan->cur_slot++
        # 本页扫完，走 next_page
        next = page.next_page
        unpin(cur_page)
        if next == INVALID:
            return false   # 扫完了
        cur_page = next
        cur_slot = 0
```

图示：

```
扫描游标：  ┌───┐
            │ ▼ │  cur_page=0, cur_slot=0
   ┌────────┴───┴┐    next    ┌────────┐    next    ┌────────┐
   │ Page 0      │ ─────────→ │ Page 1 │ ─────────→ │ Page 2 │
   │ [t0][t1][t2]│            │ [t0][t1]│            │ [t0]   │
   └─────────────┘            └────────┘            └────────┘
   扫完 → cur_page=1, cur_slot=0
   扫完 → cur_page=2, cur_slot=0
   扫完 → next=INVALID → 结束
```

### 4.7 页链表的缺点与改进方向

| 缺点                      | 真实数据库的改进                          |
|---------------------------|------------------------------------------|
| 单向，不能反向扫描        | 双向链表（PostgreSQL 页头有 prev/next）  |
| 删除页后链表留洞           | FSM + 空闲页回收（见第 9 节）            |
| 顺序扫描必须读所有页      | Visibility Map 跳过全空页（PostgreSQL）  |
| 链表无索引，按页号定位慢   | RID 直接含页号，不需要走链表             |

---

## 5. Table = Heap + Index

### 5.1 为什么要分离？

回顾章0 的总图：`table_t` 把 `heap_t` 和 `btree_t` 组合起来。为什么不让 heap 自己带索引？

**原因 1：一张表可以有多个索引**

```
users 表：
  - 主键索引 on id      （B+Tree 1）
  - 二级索引 on age     （B+Tree 2）
  - 组合索引 on (age, score) （B+Tree 3）

数据只存一份（在 heap 里），三个索引都指向 heap。
```

如果数据和索引揉在一起（如 IOT），加第二个索引就要再存一份数据，浪费空间。

**原因 2：索引体积小，更容易驻留内存**

索引只存 `key → RID`，不存完整 tuple。一个 1KB 的 tuple，索引项可能只有 8 字节（4 字节 key + 4 字节 RID）。索引更小，更可能整个放进 buffer pool 的热区。

**原因 3：更新非索引列时，索引不用动**

```
UPDATE users SET age = 30 WHERE id = 1;
```

这条 SQL 只改了 age 列。如果 id 上有索引：

- 堆表方式：只改 heap 里的 tuple，索引完全不动。
- IOT 方式：如果 tuple 变长，可能引起 B+Tree 节点分裂。

**原因 4：顺序扫描高效**

`SELECT * FROM users;` 这种全表查询，堆表只需沿页链表顺序读，对 IO 友好（顺序读比随机读快很多）。IOT 要按主键顺序遍历 B+Tree 叶子，虽然也是顺序，但中间夹着内部节点的查找。

### 5.2 table_t 的结构

```c
struct table {
    heap_t   *heap;     // 堆表，存数据
    btree_t  *index;    // B+Tree 索引，存 pk → RID
    const schema_t *schema;
    int       pk_col;   // 主键列号
};
```

```
            table_t
           ┌────────────────┐
           │ schema         │
           │ pk_col = 0     │
           │ heap ──────┐   │
           │ index ──┐  │   │
           └─────────┼──┼───┘
                     │  │
                     ▼  ▼
              ┌────────┐ ┌────────┐
              │ B+Tree │ │  Heap  │
              │ (索引) │ │ (数据) │
              └────────┘ └────────┘
```

### 5.3 查询时如何协作？

**按主键查找 `table_find(pk)`**：

```
步骤 1：在 B+Tree 里查 pk → 得到 RID
步骤 2：用 RID 去 heap 取完整 tuple   ← 这一步叫"回表"
```

```
table_find(pk=42):

   B+Tree 索引                  Heap 堆表
   ┌──────────────┐            ┌──────────────┐
   │ 查找 42      │            │              │
   │  → RID(3,7)  │ ─────────→ │ fetch(3,7)   │
   │              │   回表     │  → tuple     │
   └──────────────┘            └──────────────┘
   O(log n)                    O(1)
```

**范围查询 `table_scan_open(start, end)`**：

```
步骤 1：在 B+Tree 上开一个范围游标 [start, end]
步骤 2：每调一次 next：
        - 从 B+Tree 游标取下一个 (key, RID)
        - 用 RID 去 heap 取 tuple
        - 返回 tuple
```

```
table_scan(10, 50):

   B+Tree 范围扫描              Heap 回表
   ┌──────────────┐            ┌──────────────┐
   │ 10 → RID(0,1)│ ─────────→ │ fetch(0,1)   │ → tuple1
   │ 11 → RID(2,0)│ ─────────→ │ fetch(2,0)   │ → tuple2
   │ 12 → RID(1,3)│ ─────────→ │ fetch(1,3)   │ → tuple3
   │ ...          │            │              │
   │ 50 → RID(5,2)│ ─────────→ │ fetch(5,2)   │ → tupleN
   └──────────────┘            └──────────────┘
```

### 5.4 插入时如何协作？

```c
rid_t table_insert(table_t *table, const tuple_t *tuple) {
    rid_t rid = heap_insert(table->heap, tuple);          // 1. 先插堆表
    int32_t pk = tuple_get_int32(tuple, table->pk_col);   // 2. 取主键
    btree_insert(table->index, pk, rid);                  // 3. 再插索引
    return rid;
}
```

顺序很重要：**先插堆表，再插索引**。

- 如果先插索引：索引指向的 RID 还没有数据，期间有人查询会读到空。
- 先插堆表：即使索引还没插完，查询最多"找不到"，不会"找到空数据"。

```
时间线：
  t1: heap_insert  ──→ 数据已落盘，RID 已知
  t2: btree_insert ──→ 索引指向 RID

  在 t1 和 t2 之间查询：索引找不到 → 返回空（可接受）
  在 t2 之后查询：     索引找到 → 回表取数据（正确）
```

### 5.5 删除时如何协作？

```c
bool table_delete(table_t *table, int32_t pk) {
    rid_t rid;
    if (!btree_find(table->index, pk, &rid)) return false;  // 1. 索引找不到 → 没这行
    btree_delete(table->index, pk);                          // 2. 删索引
    heap_delete(table->heap, rid);                           // 3. 删堆表
    return true;
}
```

删除顺序：**先删索引，再删堆表**。

- 先删索引：之后即使有人查这个 pk，索引找不到，不会去访问堆表里那个"将被删除"的 tuple。
- 如果先删堆表：索引还指向那个 RID，查询会回表取到一个已删除的 slot（虽然 `page_is_slot_deleted` 会处理，但多了一次无效访问）。

---

## 6. 插入流程：从 SQL INSERT 到页写入

### 6.1 完整流程图

以 `INSERT INTO users VALUES (42, 20, 95.5);` 为例，从 SQL 文本到磁盘字节的全过程：

```
SQL: INSERT INTO users VALUES (42, 20, 95.5);
  │
  ▼
┌─────────────────────────────────┐
│ 1. SQL 解析（章6 会讲）         │  解析出表名、列值
│    → table=users, vals=[42,20,95.5]│
└─────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────┐
│ 2. 构造 tuple                   │  按 schema 创建 tuple 并填值
│    tuple = tuple_create(schema) │
│    tuple_set_int32(tuple,0,42)  │
│    tuple_set_int32(tuple,1,20)  │
│    tuple_set_float(tuple,2,95.5)│
└─────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────┐
│ 3. table_insert(table, tuple)   │  进入 table 层
│    ├─ 3a. heap_insert(heap,t)   │  先插堆表
│    │    ├─ 序列化 tuple → buf   │
│    │    ├─ 取 last_page         │
│    │    ├─ 若页满 → 申请新页    │
│    │    │         → 链接页链表  │
│    │    ├─ page_add_tuple(buf)  │  写入页的 slot
│    │    └─ 返回 RID(pid, sid)   │
│    ├─ 3b. pk = tuple_get_int32  │  取主键
│    └─ 3c. btree_insert(index,pk,rid)│  插索引
└─────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────┐
│ 4. buffer pool 管理             │  页被 pin → 修改 → unpin(dirty)
│    bp_fetch_page / bp_unpin_page│  dirty 页后续由 pager 写回磁盘
└─────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────┐
│ 5. 磁盘（章1 pager）            │  WAL 先写（章5），数据页后写
└─────────────────────────────────┘
```

### 6.2 逐步详解

**步骤 1：SQL 解析**

这一步在章6 会详细讲，这里只需知道：解析器把 SQL 文本变成一个"插入操作"对象，包含表名和一组列值。

**步骤 2：构造 tuple**

```c
tuple_t *t = tuple_create(&schema);      // 分配 13 字节
tuple_set_int32(t, 0, 42);               // id = 42
tuple_set_int32(t, 1, 20);               // age = 20
tuple_set_float(t, 2, 95.5);             // score = 95.5
```

此时 tuple 在内存中是：

```
[00 2A 00 00 00 14 00 00 00 00 00 BE 42]
 ↑  ↑─────────↑  ↑─────────↑  ↑─────────↑
 │  id=42       age=20       score=95.5
 NULL 位图（全 0，没有 NULL）
```

**步骤 3a：heap_insert**

```c
rid_t heap_insert(heap_t *heap, const tuple_t *tuple) {
    uint8_t buf[PAGE_SIZE];
    uint16_t len = tuple_serialize(tuple, buf);   // tuple → buf，len=13

    page_id_t target_pid = heap->last_page;       // 先看最后一页
    page_t *page = bp_fetch_page(heap->bp, target_pid);

    if (!page_has_space(page, len)) {             // 页满了？
        // ... 申请新页，链接页链表 ...
    }

    slot_id_t sid = page_add_tuple(page, buf, len);  // 写入 slot
    rid_t rid = { target_pid, sid };
    bp_unpin_page(heap->bp, target_pid, true);    // dirty unpin
    return rid;
}
```

**步骤 3a 的页满分支**：

```
页满时的处理：

  ┌──────────┐                              ┌──────────┐
  │ old_page │  next=INVALID                │ new_page │
  │ (满)     │                              │ (空)     │
  └──────────┘                              └──────────┘

  ① bp_new_page → 得到 new_pid 和 new_page 指针
  ② page_set_next_page(old_page, new_pid)   ← 把旧页 next 指向新页
  ③ heap->last_page = new_pid               ← 更新 last 指针
  ④ 在 new_page 上 page_add_tuple
```

**步骤 3b：取主键**

```c
int32_t pk = tuple_get_int32(tuple, table->pk_col);   // pk_col=0 → 取 id 列
// pk = 42
```

**步骤 3c：btree_insert**

```c
btree_insert(table->index, pk, rid);
// 把 (42, RID(0, 3)) 插入 B+Tree
```

B+Tree 的插入细节见章3。这里只需知道：索引项是 `key=42 → RID(0,3)`。

**步骤 4：buffer pool**

整个过程中，页被 `bp_fetch_page` 取出、修改、`bp_unpin_page` 标记为 dirty。buffer pool 会在后续按 LRU/刷脏策略把页写回磁盘。

**步骤 5：磁盘**

真正写磁盘由 pager（章1）完成。为了保证崩溃恢复，会先写 WAL（章5）再写数据页。

### 6.3 插入的时间复杂度

| 步骤              | 复杂度          | 说明                          |
|-------------------|-----------------|-------------------------------|
| tuple 构造        | O(列数)         | 逐列设值                      |
| heap_insert       | O(1) 均摊       | 最后一页通常有空间            |
| 申请新页          | O(1)            | buffer pool 分配              |
| btree_insert      | O(log n)        | B+Tree 插入                   |
| **总计**          | **O(log n)**    | 由 B+Tree 主导                |

---

## 7. 查询流程：全表扫描 vs 索引查找

### 7.1 两种查询路径

```
SELECT * FROM users WHERE id = 42;
```

这条 SQL 有两种执行方式：

```
路径 A：全表扫描（无索引或优化器选择）
   ┌────────┐    ┌────────┐    ┌────────┐
   │ Page 0 │ →  │ Page 1 │ →  │ Page 2 │ → ...
   └────────┘    └────────┘    └────────┘
       │             │             │
       ▼             ▼             ▼
   逐 tuple 比较 id==42？
   找到 → 返回；扫完没找到 → 返回空
   代价：O(n)，n = tuple 总数

路径 B：索引查找（有索引且选择性高）
   ┌──────────┐
   │ B+Tree   │  查 42 → RID(3,7)
   └──────────┘
       │
       ▼  回表
   ┌──────────┐
   │ Page 3   │  fetch slot 7 → tuple
   └──────────┘
   代价：O(log n) + O(1)
```

### 7.2 全表扫描详解

```c
heap_scan_t *scan = heap_scan_open(heap);
tuple_t *t = tuple_create(&schema);
rid_t rid;
while (heap_scan_next(scan, &rid, t)) {
    if (tuple_get_int32(t, 0) == 42) {   // id == 42?
        printf("found!\n");
        break;
    }
}
heap_scan_close(scan);
```

执行过程：

```
scan_open:  cur_page=0, cur_slot=0

next 调用 1: fetch page 0, slot 0 → tuple(1,Alice,20)   id=1≠42，继续
next 调用 2: fetch page 0, slot 1 → tuple(2,Bob,25)     id=2≠42，继续
next 调用 3: fetch page 0, slot 2 → tuple(3,Carol,30)   id=3≠42，继续
... （假设 page 0 有 100 个 tuple）
next 调用 101: page 0 扫完，cur_page=1, cur_slot=0
next 调用 102: fetch page 1, slot 0 → ...
... 直到找到 id=42 或扫完所有页
```

**特点**：

- 不用索引，直接遍历。
- 适合"扫描大部分数据"的查询，如 `SELECT * FROM users WHERE age > 18;`（命中率高）。
- 适合没有索引的列。

### 7.3 索引查找详解

```c
tuple_t *found = table_find(table, 42);
// 内部：
//   1. btree_find(index, 42) → RID(3, 7)
//   2. heap_fetch(heap, RID(3,7)) → tuple
```

执行过程：

```
btree_find(42):
   根节点 → 比较 42 找到子节点
   内部节点 → 继续向下
   叶子节点 → 找到 key=42, value=RID(3,7)
   （共 log n 次节点访问）

heap_fetch(RID(3,7)):
   bp_fetch_page(3) → page
   page_get_tuple(page, 7) → tuple 数据
   tuple_deserialize → tuple
   （O(1)）
```

**特点**：

- 通过索引快速定位，O(log n)。
- 适合"选择性高"的查询（命中少量行）。
- 需要回表（除非覆盖索引）。

### 7.4 路径选择：选择率

优化器（章8）如何选择走哪条路？关键指标是**选择率（selectivity）**：

```
选择率 = 命中行数 / 总行数
```

| 选择率   | 推荐路径   | 原因                                   |
|----------|------------|----------------------------------------|
| < 5%     | 索引扫描   | 命中少，回表代价小                     |
| 5%~10%   | 看情况     | 与页大小、缓存命中率有关               |
| > 10%    | 全表扫描   | 命中多，回表代价超过顺序扫             |
| 100%     | 全表扫描   | 无条件扫描                             |

**直觉理解**：

```
假设表有 10000 行，每页 100 行，共 100 页。

查询命中 10 行（选择率 0.1%）：
  索引扫描：log(10000)≈13 次索引 IO + 10 次回表 IO = 23 次
  全表扫描：100 次
  → 索引快

查询命中 5000 行（选择率 50%）：
  索引扫描：13 次索引 IO + 5000 次回表 IO = 5013 次
  全表扫描：100 次
  → 全表快
```

### 7.5 范围查询

```c
table_scan_t *scan = table_scan_open(table, 10, 50);  // id ∈ [10, 50]
while (table_scan_next(scan, t)) {
    printf("id=%d\n", tuple_get_int32(t, 0));
}
table_scan_close(scan);
```

内部实现是"B+Tree 范围游标 + 逐条回表"：

```
btree_range_open(10, 50):
   定位到 key=10 的叶子节点位置

每次 next:
   btree_range_next → (key, RID)     ← 沿叶子链表顺序取
   heap_fetch(RID) → tuple           ← 回表
   返回 tuple
```

```
B+Tree 叶子链表（章3）：
   ... [9][10][11][12]...[50][51] ...
            ↑                ↑
            start            end
   游标从 10 开始，逐个取到 50，每个都回表
```

### 7.6 覆盖索引

**覆盖索引（Covering Index）**：如果查询的列全部包含在索引里，就不需要回表。

```
索引 on (id, age)：    ← 假设有这种复合索引
SELECT age FROM users WHERE id = 42;

索引项里已经有 id 和 age，直接返回，不用去 heap。
```

miniDB 的索引只存 `pk → RID`，不存其他列，所以**没有覆盖索引**。真实数据库的二级索引可以选择包含额外列（PostgreSQL 的 `INCLUDE` 子句）。

### 7.7 两种路径的对比总结

| 维度          | 全表扫描                | 索引查找                |
|---------------|-------------------------|-------------------------|
| 复杂度        | O(n)                    | O(log n + k)            |
| 适合          | 选择率 > 10%            | 选择率 < 5%             |
| IO 模式       | 顺序读                  | 随机读（回表）          |
| 需要索引      | 否                      | 是                      |
| 回表          | 不需要                  | 需要（除非覆盖索引）    |
| miniDB 实现   | heap_scan_next          | table_find / table_scan |

---

## 8. 代码逐行解读

### 8.1 schema.c 逐行解读

```c
#include "schema.h"
#include <string.h>

// 初始化 schema：清零 + 复制表名 + 列数置 0
void schema_init(schema_t *s, const char *name) {
    memset(s, 0, sizeof(schema_t));              // 整个结构清零
    strncpy(s->name, name, MAX_NAME_LEN - 1);    // 复制表名，留 1 字节给 \0
    s->num_cols = 0;                             // 还没有列
}

// 添加一列
int schema_add_col(schema_t *s, const char *name, col_type_t type, bool nullable) {
    if (s->num_cols >= MAX_COLS) return -1;      // 列数上限检查
    col_def_t *col = &s->cols[s->num_cols];      // 取下一个空位
    strncpy(col->name, name, MAX_NAME_LEN - 1);  // 填列名
    col->type = type;                            // 填类型
    col->nullable = nullable;                    // 填是否可空
    return s->num_cols++;                        // 返回当前列号，然后列数+1
}

// 某类型占多少字节
uint16_t schema_col_size(col_type_t type) {
    switch (type) {
        case COL_INT32: return 4;   // 32 位 = 4 字节
        case COL_INT64: return 8;   // 64 位 = 8 字节
        case COL_FLOAT: return 4;   // 单精度 = 4 字节
    }
    return 0;   // 未知类型（不应该发生）
}

// NULL 位图字节数 = ceil(列数 / 8)
uint16_t schema_null_bm_size(const schema_t *s) {
    return (uint16_t)((s->num_cols + 7) / 8);
    //  trick：(n+7)/8 等价于 ceil(n/8)，避免浮点
    //  例：3 列 → (3+7)/8 = 1 字节
    //      8 列 → (8+7)/8 = 1 字节
    //      9 列 → (9+7)/8 = 2 字节
}

// 整个 tuple 的字节数
uint16_t schema_tuple_size(const schema_t *s) {
    uint16_t size = schema_null_bm_size(s);      // 从 NULL 位图开始
    for (int i = 0; i < s->num_cols; i++) {
        size += schema_col_size(s->cols[i].type); // 逐列累加
    }
    return size;
}

// 按列名找列号
int schema_find_col(const schema_t *s, const char *name) {
    for (int i = 0; i < s->num_cols; i++) {
        if (strcmp(s->cols[i].name, name) == 0) return i;  // 找到
    }
    return -1;   // 没找到
}
```

### 8.2 tuple.c 逐行解读

```c
#include "tuple.h"
#include <stdlib.h>
#include <string.h>

// ─── NULL 位图内部辅助函数 ───

// 读：第 col 列是否为 NULL
static bool bm_is_null(const uint8_t *data, int col) {
    return (data[col / 8] >> (col % 8)) & 1;
    //  col / 8 → 第几个字节
    //  col % 8 → 字节内第几个 bit
    //  >> 把目标 bit 移到最低位
    //  & 1 取出该 bit
}

// 写：设置第 col 列的 NULL 状态
static void bm_set_null(uint8_t *data, int col, bool is_null) {
    if (is_null)
        data[col / 8] |= (uint8_t)(1 << (col % 8));    // 置 1
    else
        data[col / 8] &= (uint8_t)~(1 << (col % 8));   // 清 0
}

// 计算第 col 列数据在 tuple 中的字节偏移
static uint16_t col_offset(const schema_t *s, int col) {
    uint16_t off = schema_null_bm_size(s);              // 跳过 NULL 位图
    for (int i = 0; i < col; i++) {
        off += schema_col_size(s->cols[i].type);        // 跳过前面的列
    }
    return off;
}

// ─── tuple 生命周期 ───

tuple_t *tuple_create(const schema_t *schema) {
    tuple_t *t = malloc(sizeof(tuple_t));       // 分配 tuple 结构
    t->schema = schema;
    t->length = schema_tuple_size(schema);      // 定长，长度由 schema 决定
    t->data = calloc(1, t->length);             // 分配并清零数据区
    return t;
    //  calloc 而非 malloc：让 NULL 位图初始为 0（全部非 NULL）
    //  注意：这意味着"未设置的列"默认是非 NULL 的 0 值，而非 NULL
    //  调用者应显式调用 tuple_set_null 来标记 NULL
}

void tuple_destroy(tuple_t *t) {
    if (!t) return;
    free(t->data);   // 先释放数据
    free(t);         // 再释放结构
}

// ─── INT32 读写 ───

void tuple_set_int32(tuple_t *t, int col, int32_t val) {
    bm_set_null(t->data, col, false);           // 先标记为非 NULL
    memcpy(t->data + col_offset(t->schema, col), &val, 4);
    //  写到"NULL 位图之后 + 前面列总宽"的位置
    //  用 memcpy 而非 *(int32_t*)= 是为了避免对齐/别名问题
}

int32_t tuple_get_int32(const tuple_t *t, int col) {
    int32_t val;
    memcpy(&val, t->data + col_offset(t->schema, col), 4);
    return val;
    //  注意：调用者应先检查 tuple_is_null(col)
    //  如果该列是 NULL，这里读出的是"占位垃圾"，不是有意义的数据
}

// INT64 / FLOAT 的读写完全类似，只是字节数不同，此处省略

// ─── NULL 操作 ───

void tuple_set_null(tuple_t *t, int col) {
    bm_set_null(t->data, col, true);    // 只改位图，数据区不动
}

bool tuple_is_null(const tuple_t *t, int col) {
    return bm_is_null(t->data, col);    // 只读位图
}

// ─── 序列化 ───

uint16_t tuple_serialize(const tuple_t *t, uint8_t *buf) {
    memcpy(buf, t->data, t->length);    // 整块拷贝
    return t->length;
}

void tuple_deserialize(tuple_t *t, const uint8_t *buf, uint16_t len) {
    memcpy(t->data, buf, len);          // 整块拷贝
    t->length = len;
}
```

### 8.3 heap.c 逐行解读

```c
#include "heap.h"
#include <stdlib.h>
#include <string.h>

// heap 结构：buffer pool + schema + 页链表首尾指针
struct heap {
    buffer_pool_t *bp;
    const schema_t *schema;
    page_id_t first_page;   // 扫描起点
    page_id_t last_page;    // 插入落点
};

// 扫描游标：记录当前页、当前 slot、是否扫完
struct heap_scan {
    buffer_pool_t *bp;
    const schema_t *schema;
    page_id_t cur_page;
    slot_id_t cur_slot;
    bool done;
};

// 创建堆表：申请第一页，首尾都指向它
heap_t *heap_create(buffer_pool_t *bp, const schema_t *schema) {
    heap_t *heap = malloc(sizeof(heap_t));
    heap->bp = bp;
    heap->schema = schema;

    page_t *page;
    page_id_t pid = bp_new_page(bp, &page);   // 申请第一页
    heap->first_page = pid;
    heap->last_page = pid;                    // 只有一页时首尾相同
    bp_unpin_page(bp, pid, true);             // 新页是 dirty 的（要写回）
    return heap;
}

void heap_destroy(heap_t *heap) {
    free(heap);
    //  注意：页本身由 buffer pool / pager 管理，这里不释放
}

// 插入一个 tuple，返回 RID
rid_t heap_insert(heap_t *heap, const tuple_t *tuple) {
    uint8_t buf[PAGE_SIZE];
    uint16_t len = tuple_serialize(tuple, buf);   // tuple → 字节流

    page_id_t target_pid = heap->last_page;       // 先尝试最后一页
    page_t *page = bp_fetch_page(heap->bp, target_pid);

    if (!page_has_space(page, len)) {             // 页放不下？
        bp_unpin_page(heap->bp, target_pid, false);  // 先释放（没改）

        page_t *new_page;
        page_id_t new_pid = bp_new_page(heap->bp, &new_page);  // 申请新页

        page_t *old_page = bp_fetch_page(heap->bp, target_pid);  // 重新取旧页
        page_set_next_page(old_page, new_pid);   // 旧页 next → 新页
        bp_unpin_page(heap->bp, target_pid, true);  // 旧页 dirty

        heap->last_page = new_pid;               // 更新尾指针
        target_pid = new_pid;
        page = new_page;                         // 后续在新页上操作
    }

    slot_id_t sid = page_add_tuple(page, buf, len);  // 写入 slot
    rid_t rid = { target_pid, sid };
    bp_unpin_page(heap->bp, target_pid, true);   // dirty unpin
    return rid;
}

// 按 RID 取 tuple
tuple_t *heap_fetch(heap_t *heap, rid_t rid) {
    page_t *page = bp_fetch_page(heap->bp, rid.page_id);  // 直接按页号取
    uint16_t len;
    const void *data = page_get_tuple(page, rid.slot_id, &len);

    tuple_t *tuple = NULL;
    if (data && len > 0) {                       // slot 有效
        tuple = tuple_create(heap->schema);
        tuple_deserialize(tuple, (const uint8_t *)data, len);
    }
    bp_unpin_page(heap->bp, rid.page_id, false); // 只读，不 dirty
    return tuple;
}

// 删除：只标记 slot 为 deleted，不立即回收空间
bool heap_delete(heap_t *heap, rid_t rid) {
    page_t *page = bp_fetch_page(heap->bp, rid.page_id);
    bool ok = page_delete_tuple(page, rid.slot_id);
    bp_unpin_page(heap->bp, rid.page_id, true);  // dirty
    return ok;
}

// 打开顺序扫描
heap_scan_t *heap_scan_open(heap_t *heap) {
    heap_scan_t *scan = malloc(sizeof(heap_scan_t));
    scan->bp = heap->bp;
    scan->schema = heap->schema;
    scan->cur_page = heap->first_page;   // 从第一页开始
    scan->cur_slot = 0;
    scan->done = false;
    return scan;
}

// 取下一个 tuple
bool heap_scan_next(heap_scan_t *scan, rid_t *rid, tuple_t *tuple) {
    if (scan->done) return false;

    for (;;) {                                   // 外层循环：翻页
        page_t *page = bp_fetch_page(scan->bp, scan->cur_page);
        uint16_t num_slots = page_get_num_slots(page);

        while (scan->cur_slot < num_slots) {     // 内层循环：页内 slot
            slot_id_t sid = scan->cur_slot;
            scan->cur_slot++;

            if (page_is_slot_deleted(page, sid)) continue;  // 跳过已删除

            uint16_t len;
            const void *data = page_get_tuple(page, sid, &len);
            if (!data) continue;                 // 跳过无效 slot

            if (rid) {                           // 输出 RID
                rid->page_id = scan->cur_page;
                rid->slot_id = sid;
            }
            if (tuple) {                         // 输出 tuple
                tuple_deserialize(tuple, (const uint8_t *)data, len);
            }
            bp_unpin_page(scan->bp, scan->cur_page, false);
            return true;
        }

        // 本页扫完，翻到下一页
        page_id_t next = page_get_next_page(page);
        bp_unpin_page(scan->bp, scan->cur_page, false);

        if (next == INVALID_PAGE_ID) {           // 链表结束
            scan->done = true;
            return false;
        }
        scan->cur_page = next;
        scan->cur_slot = 0;
    }
}

void heap_scan_close(heap_scan_t *scan) {
    free(scan);
}

page_id_t heap_first_page(heap_t *heap) {
    return heap->first_page;
}
```

### 8.4 table.c 逐行解读

```c
#include "table.h"
#include <stdlib.h>

// table = heap + btree + schema + 主键列号
struct table {
    heap_t *heap;
    btree_t *index;
    const schema_t *schema;
    int pk_col;
};

// 范围扫描游标 = B+Tree 范围游标 + heap 引用
struct table_scan {
    btree_cursor_t *bcur;
    heap_t *heap;
};

// 创建表：同时建堆表和索引
table_t *table_create(buffer_pool_t *bp, const schema_t *schema, int pk_col) {
    table_t *table = malloc(sizeof(table_t));
    table->heap = heap_create(bp, schema);       // 建堆表
    table->index = btree_create(bp);             // 建索引
    table->schema = schema;
    table->pk_col = pk_col;
    return table;
}

void table_destroy(table_t *table) {
    if (!table) return;
    heap_destroy(table->heap);
    btree_destroy(table->index);
    free(table);
}

// 插入：先堆表后索引
rid_t table_insert(table_t *table, const tuple_t *tuple) {
    rid_t rid = heap_insert(table->heap, tuple);           // 1. 插堆表，得 RID
    int32_t pk = tuple_get_int32(tuple, table->pk_col);    // 2. 取主键值
    btree_insert(table->index, pk, rid);                   // 3. 插索引
    return rid;
}

// 按主键查：索引找 RID，再回表
tuple_t *table_find(table_t *table, int32_t pk) {
    rid_t rid;
    if (!btree_find(table->index, pk, &rid)) return NULL;  // 索引没找到
    return heap_fetch(table->heap, rid);                   // 回表
}

// 删除：先索引后堆表
bool table_delete(table_t *table, int32_t pk) {
    rid_t rid;
    if (!btree_find(table->index, pk, &rid)) return false; // 没这行
    btree_delete(table->index, pk);                        // 删索引
    heap_delete(table->heap, rid);                         // 删堆表
    return true;
}

// 范围扫描：B+Tree 范围游标 + 逐条回表
table_scan_t *table_scan_open(table_t *table, int32_t start, int32_t end) {
    table_scan_t *scan = malloc(sizeof(table_scan_t));
    scan->bcur = btree_range_open(table->index, start, end);  // 开 B+Tree 游标
    scan->heap = table->heap;
    return scan;
}

bool table_scan_next(table_scan_t *scan, tuple_t *tuple) {
    btree_key_t key;
    rid_t rid;
    if (!btree_range_next(scan->bcur, &key, &rid)) return false;  // 取下一个 RID

    tuple_t *fetched = heap_fetch(scan->heap, rid);    // 回表
    if (!fetched) return false;

    tuple_deserialize(tuple, fetched->data, fetched->length);  // 拷给调用者
    tuple_destroy(fetched);                            // 释放临时 tuple
    return true;
}

void table_scan_close(table_scan_t *scan) {
    btree_range_close(scan->bcur);
    free(scan);
}

// 访问器：暴露内部 heap 和 index（供高级用法）
heap_t *table_heap(table_t *table) { return table->heap; }
btree_t *table_index(table_t *table) { return table->index; }
```

---

## 9. 空间管理：FSM 概念

### 9.1 什么是 FSM？

**FSM（Free Space Map，空闲空间映射）** 是一张记录"每个页还有多少空闲空间"的辅助数据结构。

miniDB 的 `heap_insert` 总是往 `last_page` 插。如果 `last_page` 满了，就申请新页。这有一个问题：

```
场景：删除大量 tuple 后，很多页都有大量空闲空间，但 last_page 还是满的。

   ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐
   │ Page 0 │  │ Page 1 │  │ Page 2 │  │ Page 3 │ ← last_page（满）
   │ 删了很多│  │ 删了很多│  │ 删了很多│  │ 刚插满  │
   │ 80%空  │  │ 70%空  │  │ 90%空  │  │ 0%空   │
   └────────┘  └────────┘  └────────┘  └────────┘

新插入：miniDB 会直接申请 Page 4，而不是复用 Page 0/1/2 的空闲空间！
```

这造成**空间浪费**：旧页的空闲空间无法被新插入利用。

### 9.2 FSM 的作用

FSM 记录每个页的空闲空间量，插入时找一页"够大且不太空"的页来复用：

```
FSM（简化版）：
   page 0: 80% free   ← 适合放 60 字节的 tuple
   page 1: 70% free
   page 2: 90% free
   page 3: 0%  free   ← 跳过
   page 4: 100% free  ← 新页，最后备选

插入 60 字节 tuple：
  1. 查 FSM，找到 page 0 有 80% 空闲，够用
  2. 去 page 0 插入
  3. 更新 FSM：page 0 现在剩 75% 空闲
```

### 9.3 PostgreSQL 的 FSM 实现

PostgreSQL 的 FSM 是一棵**树形结构**，每个文件有一个 FSM：

```
                    ┌────────────┐
                    │  root      │  max(子节点)
                    │  = 255     │
                    └─────┬──────┘
              ┌───────────┼───────────┐
              ▼           ▼           ▼
          ┌──────┐    ┌──────┐    ┌──────┐
          │ 200  │    │ 150  │    │ 255  │   ← 每个节点存子树最大空闲
          └───┬──┘    └───┬──┘    └───┬──┘
              ...         ...         ...

查找"至少需要 X 字节"的页：
  从根开始，找第一个 >= X 的子节点，向下直到叶子 → 页号
  O(log n)
```

空闲量用 0-255 的字节表示（255 表示完全空），是 4KB 空间的对数映射，节省 FSM 体积。

### 9.4 miniDB 为什么没有 FSM？

miniDB 的 `heap_insert` 只用 `last_page`，没有 FSM，原因是：

1. **教学简化**：FSM 是优化，不影响正确性。
2. **避免回收的复杂度**：复用旧页空闲空间要处理"页内碎片整理"（页内 slot 重新排列），复杂。
3. **miniDB 假设以插入为主**：教学场景下删除不多，空间浪费不严重。

### 9.5 如果要给 miniDB 加 FSM

最小可行实现：

```c
// 简化 FSM：一个数组，fsm[page_id] = 该页空闲字节数
typedef struct {
    uint16_t *free_space;   // 数组，按 page_id 索引
    int num_pages;
} fsm_t;

// 插入时：先查 FSM 找一页够大的
page_id_t fsm_find(fsm_t *fsm, uint16_t need) {
    for (int i = 0; i < fsm->num_pages; i++) {
        if (fsm->free_space[i] >= need) return i;
    }
    return INVALID_PAGE_ID;   // 没有合适的，要申请新页
}

// 插入后更新 FSM
void fsm_update(fsm_t *fsm, page_id_t pid, uint16_t new_free) {
    fsm->free_space[pid] = new_free;
}
```

这个 O(n) 的 FSM 对小表够用。大表需要树形 FSM（如 PostgreSQL）。

### 9.6 其他空间管理机制

| 机制              | 作用                           | 代表数据库       |
|-------------------|--------------------------------|------------------|
| FSM               | 找有空间的页插入               | PostgreSQL       |
| FSM (Free Map)    | 同上                           | SQLite（freelist）|
| Visibility Map    | 跳过全空/全不可见的页          | PostgreSQL       |
| Vacuum            | 回收已删除 tuple 的空间        | PostgreSQL       |
| Auto-vacuum       | 自动触发 vacuum                | PostgreSQL       |
| Page compaction   | 页内整理碎片                   | SQLite（VACUUM） |

miniDB 目前只有"删除标记"（`page_delete_tuple` 标记 slot 为 deleted），没有 vacuum。空间不会自动回收，但删除的 slot 在扫描时会被跳过。

---

## 10. 与真实数据库对比

### 10.1 整体对比

| 维度              | miniDB                  | PostgreSQL               | SQLite                     |
|-------------------|-------------------------|--------------------------|----------------------------|
| 表存储方式        | 堆表                    | 堆表                     | IOT（按 rowid B+Tree）     |
| 索引组织          | 独立 B+Tree             | 独立 B+Tree              | 数据就在 B+Tree 叶子       |
| Tuple 布局        | NULL 位图 + 定长        | header + null bm + 列    | header + record + ...      |
| 变长字段          | 不支持                  | 支持（varlena）          | 支持（serial type）        |
| NULL 表示         | 位图                    | 位图                     | 位图 + 类型码              |
| 页大小            | 4KB（PAGE_SIZE）        | 8KB                      | 4KB（可配）                |
| 页链表            | 单向 next               | 单向（heap 不用链表）    | B+Tree 叶子链表            |
| 空间管理          | 无（只用 last_page）    | FSM + Visibility Map     | freelist                   |
| MVCC              | 无                      | 有（xmin/xmax）          | 有（wal + rollback）       |
| WAL               | 章5 会讲                | 有                       | 有                         |
| 主键索引          | 单列 INT32              | 任意类型/组合            | rowid 自动 + 用户索引      |
| 二级索引          | 无                      | 有                       | 有                         |
| 覆盖索引          | 无                      | 有（INCLUDE）            | 有                         |
| TOAST（大字段）   | 无                      | 有                       | 有（溢出页）               |

### 10.2 Tuple 布局对比

**miniDB**：

```
┌──────────┬────────┬────────┬────────┐
│ null_bm  │ col0   │ col1   │ col2   │   定长，13 字节
└──────────┴────────┴────────┴────────┘
```

**PostgreSQL**：

```
┌──────────────┬──────────────┬────────────────────────────┐
│ HeapTupleHdr │ NULL bitmap  │ 列数据（定长 + 变长）       │
│ 23 字节      │ ceil(列数/8) │                            │
└──────────────┴──────────────┴────────────────────────────┘
   ↑
   包含 xmin/xmax（MVCC）、cid、ctid（指向自己或更新后版本）、
   t_hoff（数据起始偏移）等
```

PostgreSQL 的 header 有 23 字节，远比 miniDB 复杂，因为它要支持 MVCC、行迁移、变长偏移等。

**SQLite**：

```
┌────────┬──────────────────────────────────┐
│ header │ payload（按"serial type"编码）   │
│ varint │                                  │
└────────┴──────────────────────────────────┘
   ↑
   每列前有一个 serial type code，指明类型和长度
   NULL 也有专门的 type code（0），不需要位图
```

SQLite 的"record format"完全不同：每列前带类型码，变长字段天然支持。

### 10.3 页链表对比

**miniDB**：堆页用单向链表（`next_page_id`）。

**PostgreSQL**：堆表**不用页链表**！顺序扫描是按"文件内页号 0, 1, 2, ..."顺序读，因为堆表是一个文件，页就是文件的固定大小块。索引通过 TID（页号, 行号）定位。

```
PostgreSQL 堆表文件：
   ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐
   │Page 0│ │Page 1│ │Page 2│ │Page 3│   ← 文件内连续块
   └──────┘ └──────┘ └──────┘ └──────┘
   顺序扫描：for i in 0..N: fetch(i)   ← 不需要链表
```

**SQLite**：数据在 B+Tree 里，叶子节点之间有双向链表（章3 讲过）。

### 10.4 空间管理对比

**miniDB**：无 FSM，只在 `last_page` 插入。

**PostgreSQL**：每个表文件有配套的 FSM 文件（`<oid>_fsm`），树形结构，O(log n) 查找。

**SQLite**：每个 B+Tree 有 freelist（空闲页列表），删除的页加入 freelist，新页优先从 freelist 取。

### 10.5 为什么 miniDB 选堆表，SQLite 选 IOT？

```
miniDB 堆表：
  + 教学清晰：数据/索引分离
  + 复用 slotted page：章1 的页直接拿来用
  - 顺序扫描要回表（其实堆表顺序扫描不用回表，这里指索引扫描）

SQLite IOT：
  + 单文件即表，部署简单（SQLite 的核心卖点）
  + 按主键查询不用回表
  - 二级索引要存 rowid，间接寻址
```

两者都是合理选择，只是设计目标不同：miniDB 重教学，SQLite 重嵌入式简洁。

### 10.6 功能差距清单

如果你想把 miniDB 升级到"能用"的程度，至少需要：

```
□ 变长字段（VARCHAR/TEXT）
□ 更多类型（DATE/DECIMAL/BLOB）
□ 二级索引（目前只有主键索引）
□ 覆盖索引
□ FSM（空间回收）
□ Vacuum（清理已删除 tuple）
□ MVCC（并发控制）
□ TOAST（大字段溢出）
□ 事务（BEGIN/COMMIT/ROLLBACK）
□ SQL 解析器（目前是直接调 C API）
□ 查询优化器（目前手动选路径）
```

每一项都是真实数据库几千行代码的模块。miniDB 的价值在于把这些"骨架"先搭起来，让你看到全貌。

---

## 11. 习题

### 习题 1（变长字段）

如果要给 miniDB 加 `VARCHAR(N)` 类型，tuple 布局要怎么调整？画出新的布局图，并说明 `col_offset` 函数如何修改。

**提示**：考虑"定长区 + 变长区 + 偏移表"的方案。变长列在数据区末尾，偏移表记录每列起始位置。

### 习题 2（多索引写代价）

一张表有 1 个主键索引 + 2 个二级索引，插入一个 tuple 需要几次写操作？删除呢？更新一个非索引列呢？

**答**：
- 插入：1 次 heap + 3 次索引 = 4 次
- 删除：1 次 heap + 3 次索引 = 4 次
- 更新非索引列：1 次 heap + 0 次索引 = 1 次（这正是堆表的优势）

### 习题 3（覆盖索引）

覆盖索引为什么不需要回表？如何判断 `SELECT age FROM users WHERE id = 42;` 是否可以用覆盖索引？miniDB 能支持吗？

**提示**：如果索引包含查询涉及的所有列（id 和 age），就不用回表。miniDB 的索引只存 `pk → RID`，不存其他列，所以不支持覆盖索引。要支持，需要在索引项里额外存 age 的值。

### 习题 4（反向扫描）

miniDB 的页链表是单向的（只有 `next_page`）。如果要支持 `ORDER BY id DESC` 的反向顺序扫描，怎么改？

**提示**：给页头加一个 `prev_page_id` 字段，维护双向链表。或者反向扫描时先正向收集所有页号，再倒序遍历。

### 习题 5（大 tuple 溢出）

如果 tuple 太大放不下一页（例如一个 10KB 的 BLOB），怎么处理？

**提示**：TOAST（The Oversized-Attribute Storage Technique）。把大字段单独存到一张"溢出表"里，原 tuple 只存一个指针。PostgreSQL 就是这么做的。

### 习题 6（FSM 设计）

给 miniDB 设计一个简单的 FSM，要求：
- 支持查询"至少需要 X 字节空闲"的页
- 插入后更新该页的空闲量
- 删除后更新该页的空闲量

写出数据结构和关键函数签名（不必实现）。

### 习题 7（删除空间回收）

miniDB 的 `heap_delete` 只标记 slot 为 deleted，不回收空间。设计一个 `heap_vacuum` 函数，把已删除的 slot 真正清除，并整理页内碎片。

**提示**：对每个页，把未删除的 slot 向前紧凑排列，更新 slot 目录。如果一页全空了，可以从页链表里摘除（注意更新前驱页的 next）。

### 习题 8（表存储方式选择）

对于以下场景，你会选堆表还是 IOT？说明理由。

| 场景                                    | 选择 | 理由 |
|-----------------------------------------|------|------|
| 日志表，按时间追加，偶尔按时间范围查    |      |      |
| 用户表，按 user_id 查询为主             |      |      |
| 配置表，行数少，按 key 查               |      |      |
| 订单表，按 order_id 查 + 按 user_id 查  |      |      |

**参考答案**：
- 日志表：堆表（追加友好，顺序扫描快）
- 用户表：IOT（按主键查为主，省回表）
- 配置表：IOT（行少，主键查）
- 订单表：堆表（两个查询维度，IOT 只能优化主键，堆表 + 二级索引更灵活）

---

## 设计决策汇总

| 决策              | 选择                          | 理由                               |
|-------------------|-------------------------------|------------------------------------|
| 表存储方式        | 堆表（非 IOT）                | 教学清晰，复用 slotted page        |
| 字段类型          | 定长（INT32/INT64/FLOAT）     | 教学简化，布局简单                 |
| NULL 处理         | 位图                          | 紧凑，O(1) 读写                    |
| 堆页组织          | 单向链表（next_page）         | 顺序扫描，增长简单                 |
| 索引              | 独立 B+Tree（章3）            | 复用已有实现，职责分离             |
| 主键              | 单列 INT32                    | 教学简化                           |
| 空间管理          | 无 FSM，只用 last_page        | 教学简化，避免回收复杂度           |
| 删除              | 标记删除（slot deleted）      | 简单，不立即整理                   |
| 插入顺序          | 先 heap 后 index              | 保证查询不会读到"索引指向空数据"   |
| 删除顺序          | 先 index 后 heap              | 保证查询不会"索引指向已删除数据"   |

---

## 小结

本章把"一张表"从逻辑到物理完整拆解了一遍：

1. **Schema** 描述表的形状（列名、类型、可空性）。
2. **Tuple** 是一行的物理表示：NULL 位图 + 定长数据区。
3. **Heap** 是所有 tuple 的集合，用页链表组织，支持顺序扫描和按 RID 取。
4. **Table** 把 Heap 和 B+Tree 组合起来，提供按主键查、范围查、自动维护索引。
5. **插入** 先写堆表再写索引；**删除** 先删索引再删堆表。
6. **查询** 有两条路：全表扫描（O(n)）和索引查找（O(log n + k)），由选择率决定。

下一章会讲 **WAL 与崩溃恢复**——保证这些写入在断电后不丢失。

---

上一章：[章3 B+Tree 索引](03-btree.md) | 下一章：[章5 WAL 与崩溃恢复](05-wal-recovery.md)
