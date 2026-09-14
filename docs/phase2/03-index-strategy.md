# 章3：索引策略实战

> 索引是数据库性能优化的第一利器。本章从索引的本质出发，对比五大索引类型，深入复合索引、覆盖索引、索引失效场景，并在 TPC-H 数据集上做对比实验，最后与阶段1 miniDB 的 B+Tree 实现做源码级对照。
>
> **本章学习路线**（建议按顺序阅读）：
> 1. 先理解索引是什么、为什么能加速（第1节）
> 2. 了解五大索引类型的差异，知道什么场景选什么（第2节）
> 3. 掌握复合索引的最左前缀原则，避免建了用不上（第3节）
> 4. 学会覆盖索引，让查询不回表（第4节）
> 5. 牢记索引失效的六大场景，避免踩坑（第5节）
> 6. 了解部分索引和表达式索引两个进阶武器（第6节）
> 7. 用 TPC-H 实验验证理论（第7节）
> 8. 学会索引维护操作（第8节）
> 9. 与 miniDB 对照，理解教学实现与生产实现的差距（第9节）
> 10. 避开常见误区（第10节）
> 11. 做习题巩固（第11节）

---

## 1. 索引基础回顾

### 1.1 什么是索引

**一句话定义**：索引是数据库为了加速查询，在表数据之外额外维护的**有序数据结构**。

**生活类比**：想象一本 1000 页的字典，如果没有目录和拼音索引，要查"数据库"这个词，只能从第 1 页翻到第 1000 页（全表扫描）。有了拼音索引，先在索引里找到 "shu" 开头的页码范围，直接翻到那一页（索引扫描）。

**数据库里的索引**：在原表（堆表，Heap）之外，额外维护一份有序的、按某（组）列排序的"目录"，让查询不必全表扫描（Sequential Scan），而是通过目录快速定位。

```
堆表（无序）                  索引（有序）
┌─────┬───────┬───────┐      ┌───────┬───────┐
│ TID │  col  │ other │      │  col  │  TID  │
├─────┼───────┼───────┤      ├───────┼───────┤
│  0  │  42   │ ...   │      │   1   │   5   │
│  1  │   7   │ ...   │      │   7   │   1   │
│  2  │  99   │ ...   │      │  18   │   3   │
│  3  │  18   │ ...   │      │  42   │   0   │
│  4  │  55   │ ...   │      │  55   │   4   │
│  5  │   1   │ ...   │      │  99   │   2   │
└─────┴───────┴───────┘      └───────┴───────┘
   查 col=42 要扫 6 行           查 col=42 → 二分 → 1 次 IO
```

**关键概念解释**（新手必读）：

| 术语 | 含义 | 类比 |
|---|---|---|
| **堆表（Heap）** | 数据按插入顺序物理存放，无序 | 书架上随便塞的书 |
| **TID（Tuple ID）** | 元组在磁盘上的物理位置（页号+偏移） | 书在书架上的坐标 |
| **索引项** | (键值, TID) 二元组 | 目录里的"词条 → 页码" |
| **全表扫描（Seq Scan）** | 从第 1 行扫到最后一行 | 一页页翻书 |
| **索引扫描（Index Scan）** | 先查索引拿 TID，再回表读数据 | 先查目录再翻页 |
| **回表（Table Lookup）** | 拿到 TID 后去堆表读完整行 | 翻到目录指的页 |

### 1.2 为什么索引能加速

**核心原理**：索引是有序的，可以用**二分查找**（B-Tree）或**哈希**（Hash）快速定位，把 O(N) 的扫描降到 O(log N) 甚至 O(1)。

**量化对比**（假设表有 N 行）：

| 查询方式 | 时间复杂度 | N=1,000,000 时的比较次数 |
|---|---|---|
| 全表扫描 | O(N) | 1,000,000 次 |
| B-Tree 索引 | O(log₂ N) | 约 20 次 |
| Hash 索引 | O(1) | 1 次 |

> **新手直觉**：100 万行的表，全表扫要比较 100 万次，B-Tree 索引只要 20 次，差了 5 万倍！这就是索引的威力。

**但索引不是免费的**，下面讲代价。

### 1.3 B-Tree 结构图解

B-Tree（平衡多路搜索树）是数据库最常用的索引结构。**为什么不用二叉树？** 因为二叉树太高（log₂ N），每层一次磁盘 IO，100 万行要 20 次 IO。B-Tree 每个节点有几百个孩子（叫"阶"），树高只有 3~4 层，IO 次数极少。

```
                 ┌─────────────────────┐
                 │     [40 | 80]       │         ← 根节点（内部节点）
                 └──────┬──────┬───────┘
                       <40   40-80   >80
                        /      |      \
              ┌──────────────┐ ┌──────────────────┐ ┌──────────────┐
              │ [10|20|30]   │ │ [40|50|60|70]    │ │ [80|90]      │  ← 叶子节点
              └──────────────┘ └──────────────────┘ └──────────────┘
                     ↕                ↕                    ↕
                  叶子间通过指针连接（B+Tree 特性，方便范围扫描）
```

**B-Tree vs B+Tree**（新手常混淆，这里讲清楚）：

| 特性 | B-Tree | B+Tree |
|---|---|---|
| 数据存哪 | 内部节点和叶子都存数据 | **只有叶子节点存数据** |
| 内部节点存什么 | 键 + 数据 | **只存键（路由）** |
| 叶子节点链表 | 无 | **有（双向链表，支持范围扫描）** |
| 范围扫描 | 要回到根节点 | **顺着叶子链表扫即可** |
| 树高 | 略低（内部节点也存数据） | 略高（内部节点只存键，能塞更多） |

> **PostgreSQL 和 SQLite 都用 B-Tree**（不是严格的 B+Tree，但叶子节点也通过内部节点间接连接，支持范围扫描）。miniDB 阶段1 实现的是 B+Tree（叶子有双向链表）。

**查找过程示例**（在上图中查 `col = 60`）：

```
1. 从根节点 [40 | 80] 开始
2. 60 在 40 和 80 之间 → 走中间指针
3. 到叶子节点 [40 | 50 | 60 | 70]
4. 在叶子内二分查找 → 找到 60
5. 返回 60 对应的 TID → 回表读数据
```

**范围查找示例**（查 `40 <= col <= 70`）：

```
1. 查 col=40 → 定位到叶子 [40|50|60|70] 的 40
2. 顺着叶子链表向右扫：40 → 50 → 60 → 70
3. 70 是上界，停止
4. 收集所有 TID → 批量回表
```

### 1.4 索引的代价

索引是**以空间换时间**，代价体现在四个维度：

| 代价维度 | 说明 | 量化 | 新手理解 |
|---|---|---|---|
| **存储空间** | 索引本身占磁盘空间 | B-Tree 约 1.2~1.5 × 键大小 | 每加一个索引，表的总存储就增加一截 |
| **写入放大** | INSERT/UPDATE/DELETE 要同步维护索引 | 每个索引 +1 次写 | 5 个索引的表，一次 INSERT 实际写 6 处 |
| **维护成本** | VACUUM/ANALYZE 要维护索引 | 死索引项回收 | PG 的 VACUUM 也要清理索引 |
| **选择错误** | 选错索引反而更慢 | 小表全表扫更快 | 100 行的表加索引，查询反而慢 |

**写入放大详解**（新手重点理解）：

```
没有索引时，INSERT 一行：
  → 写堆表 1 次 IO

有 3 个索引时，INSERT 一行：
  → 写堆表 1 次
  → 写索引1 1 次
  → 写索引2 1 次
  → 写索引3 1 次
  共 4 次 IO（4 倍写入放大）
```

**UPDATE 的额外问题**：

```sql
-- 假设 orders 表有 idx_user_id 和 idx_status 两个索引
UPDATE orders SET status = 'shipped' WHERE order_id = 100;
```

```
执行过程：
  1. 找到 order_id=100 的行（用主键索引）
  2. 修改 status 列
  3. 因为 status 在 idx_status 中，要更新 idx_status
  4. user_id 没变，idx_user_id 不用更新

关键：UPDATE 只更新索引中包含的列时，才需要维护该索引
```

> **核心权衡**：读多写少的列适合建索引；频繁更新的列建索引要谨慎。一个经验法则——**索引选择性 > 0.1**（即 `distinct_count / total_count > 0.1`）才值得建。

### 1.5 索引选择性

**选择性（Selectivity）** 是判断要不要建索引的核心指标：

```
选择性 = 不同值的数量 / 总行数
```

| 选择性 | 含义 | 适合建索引？ | 例子 |
|---|---|---|---|
| **接近 1.0** | 几乎每行都不同 | ✅ 非常适合 | 主键、邮箱、身份证号 |
| **0.1 ~ 0.5** | 有一定区分度 | ✅ 适合 | 城市、商品类别 |
| **< 0.01** | 大量重复值 | ❌ 不适合 | 性别、是否删除 |
| **= 1.0** | 唯一索引 | ✅ 最适合 | 主键、UNIQUE 约束 |

**计算示例**：

```sql
-- 计算 orders 表 status 列的选择性
SELECT 
    COUNT(DISTINCT status) AS distinct_vals,
    COUNT(*) AS total_rows,
    CAST(COUNT(DISTINCT status) AS FLOAT) / COUNT(*) AS selectivity
FROM orders;

-- 结果示例：
-- distinct_vals | total_rows | selectivity
--            5   |     15000  |    0.00033  ← 选择性极低，不建议单独建索引
```

```sql
-- 计算用户表 email 列的选择性
SELECT 
    COUNT(DISTINCT email) AS distinct_vals,
    COUNT(*) AS total_rows,
    CAST(COUNT(DISTINCT email) AS FLOAT) / COUNT(*) AS selectivity
FROM users;

-- 结果示例：
-- distinct_vals | total_rows | selectivity
--        10000  |     10000  |    1.0  ← 选择性完美，适合建唯一索引
```

---

## 2. 五大索引类型对比

PostgreSQL 内置五种索引访问方法（Access Method），SQLite 主要用 B-Tree（含 `WITHOUT ROWID` 聚簇变体）。下表是完整对比：

| 类型 | 结构 | 支持范围扫描 | 支持等值 | 典型场景 | 维护成本 | 索引大小 |
|---|---|---|---|---|---|---|
| **B-Tree** | 平衡多路搜索树 | ✅ 快 | ✅ | 通用，排序，`<` `>` `BETWEEN` | 中 | 中 |
| **Hash** | 哈希表（桶+链） | ❌ | ✅ 极快 | 纯等值查询（`=`） | 低 | 小 |
| **GiST** | 广义搜索树 | ✅ | ✅ | 几何、范围、全文、最近邻 | 高 | 大 |
| **GIN** | 倒排索引 | ✅ | ✅ | 数组包含、全文搜索 `tsvector` | 很高 | 大 |
| **BRIN** | 块范围摘要 | ✅ 近似 | ✅ 近似 | 时序、按物理顺序有序的大表 | 极低 | 极小 |

### 2.1 B-Tree（默认）

```
              [40 | 80]                ← 内部节点（键+指针）
             /    |    \
       [10 20 30] [40 50 60 70] [80 90]   ← 叶子节点（有序链表）
```

**原理详解**：
- 每个节点是一个**磁盘页**（PG 默认 8KB，SQLite 默认 4KB）
- 内部节点存键和指向子节点的指针，叶子节点存键和 TID
- 节点内键有序，可用二分查找
- 插入/删除时可能触发**节点分裂**或**合并**，保持树平衡

**适用场景**：
- 等值查询：`WHERE col = 100`
- 范围查询：`WHERE col BETWEEN 10 AND 20`
- 排序：`ORDER BY col`（索引本身有序，免排序）
- `IS NULL`、`IN`、`BETWEEN`、`LIKE 'prefix%'`（前缀确定）

**SQL 示例**：

```sql
-- PG / SQLite 默认都是 B-Tree
CREATE INDEX idx_user_id ON orders(user_id);

-- 查询能用上索引
SELECT * FROM orders WHERE user_id = 100;           -- 等值
SELECT * FROM orders WHERE user_id BETWEEN 1 AND 100; -- 范围
SELECT * FROM orders WHERE user_id IN (1, 5, 10);    -- IN
SELECT * FROM orders ORDER BY user_id;                -- 排序
```

**EXPLAIN 输出示例**：

```
SQLite:
  EXPLAIN QUERY PLAN SELECT * FROM orders WHERE user_id = 100;
  → SEARCH orders USING INDEX idx_user_id (user_id=?)

PostgreSQL:
  EXPLAIN SELECT * FROM orders WHERE user_id = 100;
  → Index Scan using idx_user_id on orders  (cost=0.29..8.31 rows=1)
```

### 2.2 Hash

```sql
-- PostgreSQL
CREATE INDEX idx_hash ON t(col) USING hash;
-- 仅支持 col = ? 查询；不支持范围、排序
```

**原理详解**：
- 对键值算哈希函数 `h(key)`，定位到桶（bucket）
- 桶内用链表解决冲突
- 查询时算 `h(key)` → 定位桶 → 桶内线性扫找匹配

```
哈希表结构：
  桶0 → [key=42, TID=5] → [key=99, TID=2] → null
  桶1 → null
  桶2 → [key=7, TID=1] → null
  桶3 → [key=18, TID=3] → [key=55, TID=4] → null

查 key=42：h(42)=0 → 桶0 → 找到 [42, 5] → 返回 TID=5
```

- **优势**：等值查询 O(1)，比 B-Tree O(log N) 略快
- **限制**：不支持范围扫描、排序、`IS NULL`、参与 JOIN
- **PG 10+**：WAL 日志支持，可复制（之前不推荐生产用）
- **SQLite**：不支持 Hash 索引

**什么时候用 Hash**：
- 查询**永远是等值**，不会有范围查询
- 键很长（B-Tree 比较慢，Hash 不需要比较）
- 内存足够装下哈希表

### 2.3 GiST（Generalized Search Tree）

```sql
-- 几何包含查询
CREATE INDEX idx_box ON regions USING gist (bbox);
SELECT * FROM regions WHERE bbox && box '(0,0),(1,1)';

-- 范围类型
CREATE INDEX idx_range ON events USING gist (during);
SELECT * FROM events WHERE during && tsrange('[2024-01-01,2024-02-01)');
```

**原理详解**：
- GiST 是一种**框架**，让你为自定义数据类型实现索引
- 核心是 `consistent(q, e)` 函数：判断查询 q 是否可能匹配索引项 e
- 支持非标准运算符：`<@` `@>` `&&` `<<` `>>` 等

**适用场景**：
- **几何查询**：PostGIS 的点、线、面包含/相交
- **范围类型**：`tsrange`、`int4range` 的时间/区间重叠
- **全文搜索**：`tsvector`（但 GIN 更常用）
- **最近邻**：`<->` 距离排序（KNN 查询）
- **层级数据**：`ltree` 树形结构

**示例：找与某区域相交的所有区域**：

```sql
-- 没有 GiST 索引：要检查每个区域是否相交（O(N)）
SELECT name FROM regions WHERE bbox && box '(100,100),(200,200)';
-- Seq Scan，100 万行要扫 100 万次

-- 有 GiST 索引：用 R-Tree 快速排除不相交的区域
CREATE INDEX idx_bbox ON regions USING gist (bbox);
SELECT name FROM regions WHERE bbox && box '(100,100),(200,200)';
-- Index Scan，只检查可能相交的几十个区域
```

### 2.4 GIN（Generalized Inverted Index）

```sql
-- 数组包含
CREATE INDEX idx_tags ON posts USING gin (tags);
SELECT * FROM posts WHERE tags @> ARRAY['db','index'];

-- 全文搜索
CREATE INDEX idx_fts ON docs USING gin (to_tsvector('chinese', body));
SELECT * FROM docs WHERE to_tsvector('chinese', body) @@ plainto_tsquery('索引 失效');
```

**原理详解**：
- GIN 是**倒排索引**（Inverted Index）：对每个元素记录包含它的行
- 适合"一个字段包含多个元素"的场景：数组、全文搜索

```
倒排索引结构（posts 表的 tags 列）：

原表：
  post_id | tags
  --------+---------------------
    1     | ['db', 'index']
    2     | ['db', 'sql']
    3     | ['index', 'btree']

倒排索引：
  元素    | 包含它的 post_id 列表
  --------+---------------------
  'db'    | [1, 2]
  'index' | [1, 3]
  'sql'   | [2]
  'btree' | [3]

查 tags @> ['db', 'index']：
  → 找 'db' 的列表 [1,2]
  → 找 'index' 的列表 [1,3]
  → 交集 [1] → 返回 post_id=1
```

- **适用**：`@>` `<@` `&&` `@@`（全文匹配）
- **代价**：构建慢（每元素一个倒排项），但查询极快
- **PG 特有**，SQLite 不支持（SQLite 的 FTS5 是独立的全文搜索模块）

### 2.5 BRIN（Block Range Index）

```sql
-- 时序大表，时间列物理有序
CREATE INDEX idx_brin ON logs USING brin (ts);
SELECT * FROM logs WHERE ts BETWEEN '2024-09-01' AND '2024-09-02';
```

**原理详解**：
- 每 128 个数据块（PG 默认 8KB × 128 = 1MB）存一个 `[min, max]` 摘要
- 查询时检查摘要，跳过 `[min, max]` 与查询范围不重叠的块
- **不是精确索引**，可能扫到不匹配的块，但能跳过绝大多数

```
BRIN 结构（logs 表，ts 列物理有序）：

数据块        ts 范围           BRIN 摘要
-----------   ---------------   -----------
块 0-127      09-01 ~ 09-03     [09-01, 09-03]
块 128-255    09-03 ~ 09-06     [09-03, 09-06]
块 256-383    09-06 ~ 09-10     [09-06, 09-10]
块 384-511    09-10 ~ 09-15     [09-10, 09-15]

查 ts BETWEEN '09-01' AND '09-04'：
  → 块 0-127   [09-01,09-03] 重叠 ✅ 扫描
  → 块 128-255 [09-03,09-06] 重叠 ✅ 扫描
  → 块 256-383 [09-06,09-10] 不重叠 ❌ 跳过
  → 块 384-511 [09-10,09-15] 不重叠 ❌ 跳过
  → 只扫 256 个块，跳过 256 个块（50% 跳过率）
```

- **优势**：索引极小（几 KB / GB 数据），维护几乎免费
- **前提**：列值在物理存储上**大致有序**（如时序追加写入）
- **适用**：日志表、时序数据，几个 TB 的大表

**B-Tree vs BRIN 大小对比**（1 亿行日志表，ts 列 8 字节）：

| 索引类型 | 索引大小 | 查询速度 | 维护成本 |
|---|---|---|---|
| B-Tree | 约 2.4 GB | 快（精确） | 高（每次写入要维护） |
| BRIN | 约 80 KB | 中（近似，可能多扫几个块） | 极低（只更新摘要） |

### 2.6 选择决策树

```
查询是等值吗？
├─ 是 → 列值物理有序吗？
│       ├─ 是 → BRIN（大表）/ B-Tree（小表）
│       └─ 否 → Hash（纯等值）/ B-Tree（通用）
└─ 否 → 查询是范围/排序吗？
        ├─ 是 → B-Tree
        └─ 否 → 查询是包含/全文吗？
                ├─ 数组/全文 → GIN
                └─ 几何/范围 → GiST
```

**新手速查表**：

| 我要做的查询 | 推荐索引 |
|---|---|
| `WHERE id = 100` | B-Tree（默认） |
| `WHERE created_at > '2024-01-01'` | B-Tree |
| `WHERE name LIKE '张%'` | B-Tree（前缀确定） |
| `WHERE tags @> ['db']`（数组包含） | GIN |
| `WHERE body MATCH '索引'`（全文） | GIN（PG）/ FTS5（SQLite） |
| `WHERE bbox && box '(0,0),(1,1)'`（几何） | GiST |
| 1 亿行日志表按时间查 | BRIN |

---

## 3. 复合索引与最左前缀原则

### 3.1 复合索引的物理布局

复合索引 `(a, b, c)` 在 B-Tree 中按 `a` 升序、`a` 相同按 `b`、`b` 相同按 `c` 排序：

```
索引项顺序（a, b, c）:
(1,1,1) (1,1,2) (1,2,1) (1,2,3) (2,1,1) (2,1,5) (2,3,2) (3,1,1) ...

等价于：先按 a 分桶，桶内按 b 分桶，桶内按 c 排序

可视化：
  a=1 ┬─ b=1 ┬─ c=1
      │      └─ c=2
      └─ b=2 ┬─ c=1
             └─ c=3
  a=2 ┬─ b=1 ┬─ c=1
      │      └─ c=5
      └─ b=3 └─ c=2
  a=3 └─ b=1 └─ c=1
```

**为什么这样排序**：B-Tree 只能按一个顺序排列，复合索引把多列"拼"成一个元组键，按字典序排序。

### 3.2 最左前缀原则

**只有从最左列开始连续匹配，索引才能被使用。**

| 索引 | 查询 WHERE | 用索引？ | 说明 |
|---|---|---|---|
| `(a, b, c)` | `a = 1` | ✅ | 用 a |
| `(a, b, c)` | `a = 1 AND b = 2` | ✅ | 用 a, b |
| `(a, b, c)` | `a = 1 AND b = 2 AND c = 3` | ✅ | 用 a, b, c |
| `(a, b, c)` | `b = 2` | ❌ | 缺最左 a |
| `(a, b, c)` | `c = 3` | ❌ | 缺最左 a, b |
| `(a, b, c)` | `a = 1 AND c = 3` | ⚠️ | 只用 a，c 在索引内但无法过滤中间 b |
| `(a, b, c)` | `a = 1 AND b > 2 AND c = 3` | ⚠️ | 用 a, b；b 是范围，c 无法再用索引 |

> **关键**：范围条件（`>` `<` `BETWEEN` `LIKE 'x%'`）之后的列无法用索引继续定位。

**图解为什么缺最左列不行**：

```
索引 (a, b, c) 的 B-Tree：
  a=1 ┬─ b=1 ─ ...
      └─ b=2 ─ ...
  a=2 ┬─ b=1 ─ ...
      └─ b=3 ─ ...
  a=3 └─ b=1 ─ ...

查 b=2：b=2 的项分散在 a=1 和 a=2 等多个子树里
  → 无法从根节点定位 → 只能扫整个索引 → 优化器选择全表扫
```

**图解为什么范围条件后列失效**：

```
索引 (a, b, c)，查 a=1 AND b>2 AND c=3

定位 a=1 的子树：
  a=1 ┬─ b=1 ─ ...    ← b=1 不满足 b>2，跳过
      ├─ b=2 ─ ...    ← b=2 不满足 b>2，跳过
      ├─ b=3 ┬─ c=1   ← b=3 满足，但 c 在 b=3 桶内无序？不，c 有序
      │      ├─ c=3   ← 能用 c=3 吗？
      │      └─ c=5
      └─ b=5 ┬─ c=2
             └─ c=4

问题：b>2 匹配 b=3, b=5 等多个桶，每个桶内 c 有序，但桶间 c 无序
  → 无法用 c=3 一次性定位 → 只能在每个 b 桶内扫 c
  → 优化器只用 a, b 过滤，c 在内存里过滤
```

### 3.3 列顺序设计原则

```sql
-- 场景：订单表，常按 user_id 查，再按 created_at 排序
CREATE INDEX idx_user_time ON orders(user_id, created_at);

-- 好：能同时满足过滤 + 排序
SELECT * FROM orders WHERE user_id = 100 ORDER BY created_at;
-- Index Scan + Index Backward Scan（无需额外排序）

-- 坏：列顺序反了
CREATE INDEX idx_time_user ON orders(created_at, user_id);
SELECT * FROM orders WHERE user_id = 100 ORDER BY created_at;
-- 只能全表扫 + 排序
```

**设计法则**（新手牢记四条）：

1. **等值列在前，范围列在后**：`WHERE status='paid' AND created_at > '2024-01-01'` → `(status, created_at)`
2. **高选择性在前**：让索引更快收敛
3. **排序列最后**：利用索引有序性避免 Sort 算子
4. **不要盲目加列**：每多一列，索引更大、维护更慢

### 3.4 逐步示例：设计订单表索引

**场景**：订单表 `orders(id, user_id, status, total, created_at)`，有以下高频查询：

```sql
-- Q1：某用户的全部订单（最频繁）
SELECT * FROM orders WHERE user_id = 100 ORDER BY created_at DESC;

-- Q2：某用户某状态的订单
SELECT * FROM orders WHERE user_id = 100 AND status = 'paid';

-- Q3：某用户某时间段的订单
SELECT * FROM orders WHERE user_id = 100 AND created_at BETWEEN '2024-01-01' AND '2024-03-01';

-- Q4：某状态的所有订单（运营后台用）
SELECT * FROM orders WHERE status = 'pending';
```

**设计步骤**：

```
步骤1：分析所有查询的过滤条件
  Q1: user_id（等值）, created_at（排序）
  Q2: user_id（等值）, status（等值）
  Q3: user_id（等值）, created_at（范围）
  Q4: status（等值）

步骤2：找公共前缀
  Q1, Q2, Q3 都以 user_id 开头 → user_id 放第一列

步骤3：决定第二列
  Q1 需要按 created_at 排序 → created_at 适合放第二列
  Q3 需要按 created_at 范围过滤 → created_at 放第二列
  Q2 需要 status 过滤 → status 放第二列？

  冲突：Q1/Q3 要 created_at 第二，Q2 要 status 第二

步骤4：权衡
  方案A：(user_id, created_at, status)
    Q1 ✅ 完美（过滤+排序）
    Q2 ✅ 用 user_id，status 在第三列但中间 created_at 是等值？不，Q2 没有 created_at 条件
       → Q2 只能用 user_id，status 无法用索引（缺中间 created_at）
    Q3 ✅ 完美（过滤+范围）
  
  方案B：(user_id, status, created_at)
    Q1 ✅ 用 user_id，created_at 在第三列，但中间 status 缺条件 → 只用 user_id，排序要额外 Sort
    Q2 ✅ 完美（user_id + status）
    Q3 ✅ 用 user_id，created_at 在第三列，中间 status 缺条件 → 只用 user_id

步骤5：决定
  Q1 是最频繁的查询，优先优化 → 选方案A：(user_id, created_at, status)
  Q2 只能用 user_id，但 user_id 选择性高，扫几行后内存过滤 status 也快
  
  Q4 单独建索引：(status) 或部分索引 WHERE status='pending'
```

**最终方案**：

```sql
-- 主索引：覆盖 Q1, Q3，Q2 部分利用
CREATE INDEX idx_user_time_status ON orders(user_id, created_at, status);

-- Q4 的索引：status 选择性低，用部分索引
CREATE INDEX idx_pending ON orders(created_at) WHERE status = 'pending';
```

### 3.5 复合索引 vs 多个单列索引

**新手常见疑问**：为什么不建多个单列索引代替复合索引？

```sql
-- 方案A：复合索引
CREATE INDEX idx_ab ON t(a, b);
SELECT * FROM t WHERE a = 1 AND b = 2;  -- Index Scan，一次定位

-- 方案B：两个单列索引
CREATE INDEX idx_a ON t(a);
CREATE INDEX idx_b ON t(b);
SELECT * FROM t WHERE a = 1 AND b = 2;
-- BitmapAnd：分别用 idx_a 和 idx_b 扫，结果按位与
```

**对比**：

| 维度 | 复合索引 `(a, b)` | 两个单列索引 `idx_a + idx_b` |
|---|---|---|
| 查 `a=1 AND b=2` | ✅ 一次 Index Scan，最快 | ⚠️ 两次 Bitmap Scan + 按位与 |
| 查 `a=1` | ✅ Index Scan | ✅ Index Scan |
| 查 `b=2` | ❌ 用不上（缺最左 a） | ✅ Index Scan |
| 查 `a=1 ORDER BY b` | ✅ 索引有序，免排序 | ❌ 要额外 Sort |
| 存储空间 | 一份索引 | 两份索引 |
| 写入放大 | 1 倍 | 2 倍 |

**结论**：
- 如果经常**同时查 a 和 b** → 复合索引
- 如果经常**分别单独查 a 或 b** → 两个单列索引
- 如果两种都有 → 复合索引 + 单列索引（b）

---

## 4. 覆盖索引（Index-Only Scan）

### 4.1 原理

如果查询的列**全部在索引中**，就不必回表（不必读堆表对应 TID 的数据），直接从索引返回——即 **Index-Only Scan**。

**为什么回表慢**：
- 索引是有序的，索引项聚集在少数磁盘页
- 堆表是无序的，回表读的行分散在许多页 → **随机 IO**
- 随机 IO 比顺序 IO 慢 10~100 倍

```
普通 Index Scan（要回表）：
  索引页（有序）           堆表页（无序）
  ┌─────────┐            ┌─────────┐
  │ key=1 → TID=5  │ ───→ │ 页5: ... │  ← 随机 IO 1
  │ key=2 → TID=12 │ ───→ │ 页12: ...│  ← 随机 IO 2
  │ key=3 → TID=7  │ ───→ │ 页7: ... │  ← 随机 IO 3
  └─────────┘            └─────────┘
  3 次随机 IO（慢）

Index-Only Scan（不回表）：
  索引页（有序，含所需列）
  ┌─────────────────┐
  │ key=1, val=A    │  ← 直接返回，不读堆表
  │ key=2, val=B    │
  │ key=3, val=C    │
  └─────────────────┘
  0 次随机 IO（快）
```

```sql
-- PG
CREATE INDEX idx_cover ON orders(user_id, status, total);

-- 查询只引用索引列 → Index-Only Scan
SELECT status, total FROM orders WHERE user_id = 100;
-- 不必回表读 orders 的其他列

-- 查询引用了索引外的列 → Index Scan + 回表
SELECT status, total, remark FROM orders WHERE user_id = 100;
-- remark 不在索引中，必须回表
```

### 4.2 为什么覆盖索引快

**量化对比**（10 万行表，查 `user_id = 100`，匹配 100 行）：

| 方式 | IO 模式 | 次数 | 耗时 |
|---|---|---|---|
| 全表扫描 | 顺序 IO | 1000 页 | 10 ms |
| Index Scan + 回表 | 顺序 IO（索引）+ 随机 IO（回表） | 2 页 + 100 次随机 | 5 ms |
| Index-Only Scan | 顺序 IO（索引） | 2 页 | 0.1 ms |

> **新手直觉**：覆盖索引把 100 次随机 IO 变成 0 次，所以快 50 倍。

### 4.3 可见性映射（Visibility Map）

PG 的 Index-Only Scan 还要求**页中所有元组都对当前事务可见**（通过 visibility map 判断），否则仍需回表检查可见性。这意味着：

- 刚 VACUUM 过的表，Index-Only Scan 命中率高
- 频繁更新的表，visibility map 失效，Index-Only Scan 退化

```
查询 SELECT status, total FROM orders WHERE user_id = 100;

执行流程：
  1. 在 idx_cover 中找到 user_id=100 的索引项
  2. 检查对应堆页在 visibility map 中是否 all-visible？
     ├─ 是 → 直接返回索引中的 (status, total)  ✅ Index-Only
     └─ 否 → 回表读元组，检查可见性  ⚠️ 退化
```

**新手理解**：PG 用 MVCC 实现并发，每行有版本号。索引项不知道行的版本是否对当前事务可见，要靠 visibility map。如果页里所有行都对所有人可见（all-visible），索引项就能直接用。VACUUM 会更新 visibility map。

### 4.4 SQLite 的覆盖索引

```sql
-- SQLite EXPLAIN QUERY PLAN 出现 "COVERING INDEX" 即 Index-Only
CREATE INDEX idx_cover ON orders(user_id, status, total);

EXPLAIN QUERY PLAN
SELECT status, total FROM orders WHERE user_id = 100;
-- > SEARCH orders USING COVERING INDEX idx_cover (user_id=?)
```

SQLite 的覆盖索引更直接：只要查询的列都在索引里，就自动 Index-Only，没有 visibility map 的问题（SQLite 不用 MVCC）。

> **对比 miniDB**：阶段1 miniDB 的 B+Tree 索引只存键+TID，不支持覆盖索引（必须回表）。真实数据库的 B-Tree 索引可以 INCLUDE 额外列实现覆盖。

### 4.5 INCLUDE 语法（PG 11+）

```sql
-- 把不参与过滤但需要返回的列 INCLUDE 进索引
CREATE INDEX idx_user_include ON orders(user_id) INCLUDE (status, total);

-- user_id 用于过滤，status/total 用于返回，都不回表
SELECT status, total FROM orders WHERE user_id = 100;
```

INCLUDE 列**不参与索引排序**，只存叶子节点，维护成本低于完整复合索引。

**INCLUDE vs 复合索引对比**：

| 维度 | `(user_id, status, total)` | `(user_id) INCLUDE (status, total)` |
|---|---|---|
| 排序 | 按 user_id, status, total 排 | 只按 user_id 排 |
| 查 `user_id=100` | ✅ Index-Only | ✅ Index-Only |
| 查 `user_id=100 AND status='paid'` | ✅ 用两列过滤 | ⚠️ 只用 user_id，status 内存过滤 |
| `ORDER BY user_id, status` | ✅ 免排序 | ❌ 要额外 Sort |
| 索引大小 | 大（三列都排序） | 小（只 user_id 排序） |
| 维护成本 | 高 | 中 |

**选择建议**：
- 如果只为了**避免回表**，用 INCLUDE
- 如果还需要**多列过滤或排序**，用复合索引

### 4.6 覆盖索引的退化条件

**什么时候覆盖索引会退化成普通 Index Scan**：

| 条件 | 原因 | 解决 |
|---|---|---|
| 查询列不在索引中 | 必须回表读 | 把列加入索引或 INCLUDE |
| PG visibility map 失效 | 频繁更新，页不全可见 | VACUUM |
| 索引列被 UPDATE | 索引项要更新 | 避免更新索引列 |
| 表很小 | 优化器选择全表扫 | 不需要覆盖索引 |

**示例：退化场景**：

```sql
CREATE INDEX idx_user_status ON orders(user_id, status);

-- ✅ Index-Only：查询列都在索引里
SELECT status FROM orders WHERE user_id = 100;

-- ⚠️ 退化：total 不在索引里，要回表
SELECT status, total FROM orders WHERE user_id = 100;

-- ⚠️ 退化：SELECT * 要读所有列，必然回表
SELECT * FROM orders WHERE user_id = 100;
```

---

## 5. 索引失效场景

索引建了却用不上，是新手最常踩的坑。本节详解六大失效场景，每个都有 SQL 示例和 EXPLAIN 输出。

### 5.1 场景1：函数作用于索引列

```sql
CREATE INDEX idx_email ON users(email);

-- 失效：对列加了函数
SELECT * FROM users WHERE LOWER(email) = 'alice@x.com';
-- 不会用 idx_email，因为索引按 email 原值排序，不按 LOWER(email) 排序

-- 修复1：表达式索引
CREATE INDEX idx_email_lower ON users(LOWER(email));
SELECT * FROM users WHERE LOWER(email) = 'alice@x.com';  -- ✅

-- 修复2：不在列上加函数
SELECT * FROM users WHERE email = 'ALICE@X.COM';  -- ✅（若数据本身大写）
```

**EXPLAIN 输出对比**：

```
失效：
  EXPLAIN SELECT * FROM users WHERE LOWER(email) = 'alice@x.com';
  → Seq Scan on users  (cost=0.00..155.00 rows=1)
    Filter: (lower((email)::text) = 'alice@x.com'::text)

修复后：
  EXPLAIN SELECT * FROM users WHERE LOWER(email) = 'alice@x.com';
  → Index Scan using idx_email_lower on users  (cost=0.29..8.31 rows=1)
    Index Cond: (lower((email)::text) = 'alice@x.com'::text)
```

**为什么失效**：索引按 `email` 原值排序，`LOWER(email)` 改变了排序顺序，索引的有序性对 `LOWER(email)` 无效。

### 5.2 场景2：类型隐式转换

```sql
-- phone 是 VARCHAR，但查询传了数字
CREATE INDEX idx_phone ON users(phone);

SELECT * FROM users WHERE phone = 13800138000;  -- ❌ 隐式转 phone 为 numeric
SELECT * FROM users WHERE phone = '13800138000';  -- ✅
```

PG 一般不会隐式转字符串为数字（会报错），但 MySQL 会，是常见踩坑点。

**MySQL 下的失效**：

```sql
-- MySQL：phone 是 VARCHAR，传数字会隐式转换，索引失效
EXPLAIN SELECT * FROM users WHERE phone = 13800138000;
-- → type: ALL（全表扫），key: NULL

EXPLAIN SELECT * FROM users WHERE phone = '13800138000';
-- → type: ref，key: idx_phone
```

**新手记忆**：查询条件的类型要和列类型一致，字符串列就传字符串。

### 5.3 场景3：LIKE 前导通配符

```sql
CREATE INDEX idx_name ON users(name);

SELECT * FROM users WHERE name LIKE 'ali%';  -- ✅ 可用索引（前缀确定）
SELECT * FROM users WHERE name LIKE '%ice';  -- ❌ 前导 % 无法定位
SELECT * FROM users WHERE name LIKE '%li%';  -- ❌

-- 修复：全文索引（PG 用 trigram 或 GIN tsvector）
CREATE EXTENSION pg_trgm;
CREATE INDEX idx_name_trgm ON users USING gin (name gin_trgm_ops);
SELECT * FROM users WHERE name LIKE '%li%';  -- ✅
```

**为什么前导 % 失效**：

```
B-Tree 索引按 name 排序：
  Alice
  Bob
  Charlie
  David
  ...

查 LIKE 'Ali%'：前缀 "Ali" 确定，定位到 "Alice" 附近，往右扫  ✅
查 LIKE '%ice'：后缀 "ice" 确定，但 B-Tree 按开头排序，无法定位  ❌
```

**EXPLAIN 输出**：

```
SQLite:
  EXPLAIN QUERY PLAN SELECT * FROM users WHERE name LIKE 'ali%';
  → SEARCH users USING INDEX idx_name (name>? AND name<?)

  EXPLAIN QUERY PLAN SELECT * FROM users WHERE name LIKE '%ice';
  → SCAN users  ← 全表扫
```

### 5.4 场景4：OR 条件

```sql
CREATE INDEX idx_a ON t(a);
CREATE INDEX idx_b ON t(b);

-- 可能失效：OR 两侧列不同，优化器可能放弃索引
SELECT * FROM t WHERE a = 1 OR b = 2;

-- 修复1：UNION ALL 改写
SELECT * FROM t WHERE a = 1
UNION
SELECT * FROM t WHERE b = 2;  -- ✅ 两次 Index Scan

-- 修复2：复合索引（若 a, b 经常一起 OR）
-- 修复3：PG BitmapOr（OR 两边的 BitmapAnd 结果按位或）
```

PG 优化器通常能把 `a=1 OR b=2` 转成 BitmapOr（两个 Bitmap Index Scan 按位或），但 MySQL 早期版本会全表扫。

**EXPLAIN 输出（PG）**：

```
EXPLAIN SELECT * FROM t WHERE a = 1 OR b = 2;
→ Bitmap Heap Scan on t
    Recheck Cond: ((a = 1) OR (b = 2))
    → BitmapOr
        → Bitmap Index Scan using idx_a (a = 1)
        → Bitmap Index Scan using idx_b (b = 2)
```

**BitmapOr 原理**：

```
idx_a 找 a=1：位图 [0,1,0,1,0,0,1,0]  （第 1,3,6 行匹配）
idx_b 找 b=2：位图 [0,0,1,0,1,0,0,1]  （第 2,4,7 行匹配）
按位或：      位图 [0,1,1,1,1,0,1,1]  （第 1,2,3,4,6,7 行匹配）
→ 回表读这些行
```

### 5.5 场景5：不等式与 NOT

```sql
CREATE INDEX idx_status ON orders(status);

SELECT * FROM orders WHERE status != 'paid';  -- ⚠️ 选择性低时优化器放弃
SELECT * FROM orders WHERE NOT (status = 'paid');  -- 同上
```

`!=` `<>` `NOT IN` 通常导致全表扫，因为命中行数可能占绝大多数，索引反而更慢。

**为什么 `!=` 通常失效**：

```
假设 orders 表 10000 行，status 有 5 种值：
  paid: 8000 行
  pending: 1000 行
  shipped: 800 行
  cancelled: 150 行
  refunded: 50 行

查 status != 'paid'：匹配 2000 行（20%）
  → 用索引要 2000 次随机 IO（回表）
  → 全表扫只要 1000 页顺序 IO
  → 优化器选全表扫
```

**修复思路**：

```sql
-- 如果非 paid 的几种状态常查，用 IN 代替 !=
SELECT * FROM orders WHERE status IN ('pending', 'shipped', 'cancelled', 'refunded');

-- 或者用部分索引
CREATE INDEX idx_not_paid ON orders(created_at) WHERE status != 'paid';
```

### 5.6 场景6：索引列参与运算

```sql
CREATE INDEX idx_age ON users(age);

SELECT * FROM users WHERE age + 1 = 18;  -- ❌ 列在表达式里
SELECT * FROM users WHERE age = 17;  -- ✅ 把运算移到常量侧
```

**为什么失效**：索引按 `age` 排序，不按 `age + 1` 排序。`age + 1 = 18` 等价于 `age = 17`，但优化器不一定能自动化简。

**更多示例**：

```sql
-- ❌ 列在运算里
SELECT * FROM orders WHERE amount * 1.1 > 100;
-- ✅ 移到常量侧
SELECT * FROM orders WHERE amount > 100 / 1.1;

-- ❌ 列在函数里
SELECT * FROM orders WHERE DATE(created_at) = '2024-09-14';
-- ✅ 改成范围
SELECT * FROM orders WHERE created_at >= '2024-09-14' 
  AND created_at < '2024-09-15';

-- ❌ 列在运算里
SELECT * FROM users WHERE id - 1 = 99;
-- ✅
SELECT * FROM users WHERE id = 100;
```

### 5.7 失效场景速查表

| 场景 | 失效写法 | 修复写法 | 原因 |
|---|---|---|---|
| 函数 | `WHERE f(col) = 1` | `WHERE col = f_inv(1)` 或表达式索引 | 索引按 col 排，不按 f(col) 排 |
| 类型转换 | `WHERE varchar_col = 123` | `WHERE varchar_col = '123'` | 隐式转换改变比较方式 |
| LIKE 前导% | `WHERE col LIKE '%x'` | trigram / 全文索引 | B-Tree 按前缀排序 |
| OR 跨列 | `WHERE a=1 OR b=2` | UNION ALL / BitmapOr | 单个索引无法覆盖两列 |
| 不等式 | `WHERE col != 1` | 难修，考虑部分索引 | 选择性低，全表扫更快 |
| 列运算 | `WHERE col+1 = 2` | `WHERE col = 1` | 索引按 col 排，不按 col+1 排 |

---

## 6. 部分索引与表达式索引

### 6.1 部分索引（Partial Index）

只对满足条件的行建索引，索引更小、维护更省。

```sql
-- 只对未支付订单建索引（占全表 5%）
CREATE INDEX idx_unpaid ON orders(created_at) WHERE status = 'unpaid';

-- 查询必须包含相同条件才能用
SELECT * FROM orders WHERE status = 'unpaid' AND created_at > '2024-09-01';  -- ✅
SELECT * FROM orders WHERE created_at > '2024-09-01';  -- ❌ 条件不匹配
```

**适用**：稀疏状态（如 `is_deleted = false`）、热点子集（如 `region = 'CN'`）。

**为什么部分索引更优**：

```
假设 orders 表 100 万行，其中 status='unpaid' 的只有 5 万行（5%）

普通索引 CREATE INDEX idx ON orders(created_at)：
  → 索引项 100 万，索引大小 24 MB
  → 每次写入都要维护

部分索引 CREATE INDEX idx ON orders(created_at) WHERE status='unpaid'：
  → 索引项 5 万，索引大小 1.2 MB（小 20 倍）
  → 只有 status='unpaid' 的行写入才维护
  → 查询 WHERE status='unpaid' AND created_at>... 更快（索引小，缓存命中率高）
```

**典型场景**：

```sql
-- 场景1：软删除，90% 行 is_deleted=true，常查未删除的
CREATE INDEX idx_active ON users(email) WHERE is_deleted = false;

-- 场景2：订单状态，常查待处理订单
CREATE INDEX idx_pending ON orders(created_at) WHERE status = 'pending';

-- 场景3：区域分流，常查某地区
CREATE INDEX idx_cn ON orders(user_id) WHERE region = 'CN';

-- 场景4：大表的历史数据，只索引近期
CREATE INDEX idx_recent ON logs(level) WHERE ts > '2024-01-01';
```

### 6.2 表达式索引

对表达式结果建索引，让带函数的查询也能走索引。

```sql
-- 经常按 LOWER(email) 查
CREATE INDEX idx_email_lower ON users(LOWER(email));
SELECT * FROM users WHERE LOWER(email) = 'alice@x.com';  -- ✅

-- 经常按日期提取月份查
CREATE INDEX idx_month ON orders(EXTRACT(MONTH FROM created_at));
SELECT * FROM orders WHERE EXTRACT(MONTH FROM created_at) = 9;  -- ✅
```

**代价**：写入时要计算表达式，比普通索引慢；表达式必须查询时**完全一致**才能命中。

**常见用法**：

```sql
-- 大小写不敏感查询
CREATE INDEX idx_name_lower ON users(LOWER(name));
SELECT * FROM users WHERE LOWER(name) = 'alice';

-- 提取日期部分
CREATE INDEX idx_order_date ON orders(DATE(created_at));
SELECT * FROM orders WHERE DATE(created_at) = '2024-09-14';

-- JSON 字段（PG）
CREATE INDEX idx_json ON events((data->>'type'));
SELECT * FROM events WHERE data->>'type' = 'login';

-- 计算列
CREATE INDEX idx_bmi ON persons(weight / POWER(height/100, 2));
SELECT * FROM persons WHERE weight / POWER(height/100, 2) > 25;
```

**表达式索引的坑**：

```sql
CREATE INDEX idx_lower ON users(LOWER(name));

-- ✅ 命中：表达式完全一致
SELECT * FROM users WHERE LOWER(name) = 'alice';

-- ❌ 不命中：表达式不同（虽然语义一样）
SELECT * FROM users WHERE lower(name) = 'alice';  -- PG 区分大小写函数名？不，PG 不区分
SELECT * FROM users WHERE name ILIKE 'alice';      -- ILIKE 不走表达式索引
```

### 6.3 部分索引 + 表达式索引组合

```sql
-- 对未删除用户的邮箱小写建索引
CREATE INDEX idx_active_email ON users(LOWER(email)) WHERE is_deleted = false;

-- 查询
SELECT * FROM users WHERE is_deleted = false AND LOWER(email) = 'alice@x.com';
```

---

## 7. TPC-H 数据集对比实验

### 7.1 TPC-H 简介

TPC-H 是决策支持系统（DSS）的**标准基准测试**，由 TPC（Transaction Processing Performance Council）制定。包含 8 张表和 22 个标准查询，用于评估数据库在复杂分析查询下的性能。

本章用简化版（3 表 + 5 查询）做索引对比。

```
customer (1.5K 行, SF=0.01)
   │
   │  c_custkey
   ▼
orders (15K 行)        ← 每客户平均 10 单
   │
   │  o_orderkey
   ▼
lineitem (60K 行)      ← 每单平均 4 项
```

**表结构**：

```sql
-- customer 表
CREATE TABLE customer (
    c_custkey    INTEGER PRIMARY KEY,
    c_name       VARCHAR(25),
    c_region     VARCHAR(20),
    c_nation     VARCHAR(20)
);

-- orders 表
CREATE TABLE orders (
    o_orderkey   INTEGER PRIMARY KEY,
    o_custkey    INTEGER,    -- 外键 → customer.c_custkey
    o_status     CHAR(1),    -- 'P' pending, 'O' open, 'F' finished
    o_orderdate  DATE,
    o_total      REAL
);

-- lineitem 表
CREATE TABLE lineitem (
    l_orderkey   INTEGER,    -- 外键 → orders.o_orderkey
    l_linenumber INTEGER,
    l_shipdate   DATE,
    l_quantity   REAL,
    l_price      REAL,
    l_discount   REAL,
    PRIMARY KEY (l_orderkey, l_linenumber)
);
```

### 7.2 实验设计

对 5 个标准查询，分别测：无索引、单列索引、复合索引、覆盖索引的耗时和执行计划。

| 查询 | 业务含义 | 关键过滤条件 | 适合的索引 |
|---|---|---|---|
| Q1 | 价目汇总 | `l_shipdate <= date` | `(l_shipdate)` |
| Q2 | 按客户查订单 | `c_custkey = ?` | `(c_custkey)` |
| Q3 | 按状态+日期查 | `o_status='F' AND o_orderdate < d` | `(o_status, o_orderdate)` |
| Q4 | 按日期范围+客户 | `o_orderdate BETWEEN d1 AND d2 AND c_region='ASIA'` | 复合+JOIN |
| Q5 | 按客户名前缀 | `c_name LIKE 'Customer_0000001%'` | `(c_name)` |

### 7.3 Q1-Q5 查询 SQL

```sql
-- Q1：价目汇总（聚合查询）
SELECT 
    l_shipdate,
    SUM(l_quantity) AS total_qty,
    SUM(l_quantity * l_price) AS total_revenue,
    AVG(l_discount) AS avg_discount
FROM lineitem
WHERE l_shipdate <= '1998-09-02'
GROUP BY l_shipdate
ORDER BY l_shipdate;

-- Q2：按客户查订单（点查 + JOIN）
SELECT c_name, o_orderkey, o_orderdate, o_total
FROM customer JOIN orders ON c_custkey = o_custkey
WHERE c_custkey = 100
ORDER BY o_orderdate DESC;

-- Q3：按状态+日期查（范围 + JOIN）
SELECT c_name, o_orderkey, o_total
FROM customer JOIN orders ON c_custkey = o_custkey
WHERE o_status = 'F' AND o_orderdate < '1995-01-01'
ORDER BY o_total DESC;

-- Q4：按日期范围+区域（范围 + JOIN）
SELECT c_region, COUNT(*) AS order_count, SUM(o_total) AS revenue
FROM customer JOIN orders ON c_custkey = o_custkey
WHERE o_orderdate BETWEEN '1994-01-01' AND '1994-12-31'
  AND c_region = 'ASIA'
GROUP BY c_region;

-- Q5：按客户名前缀（LIKE + JOIN）
SELECT c_name, o_orderkey, o_total
FROM customer JOIN orders ON c_custkey = o_custkey
WHERE c_name LIKE 'Customer_0000001%'
ORDER BY o_orderdate;
```

### 7.4 运行实验

```bash
cd phase2/03-index-strategy

# 1. 生成 TPC-H 数据
python generate_tpch.py
# → data/tpch.db (SQLite, ~10MB)

# 2. 运行索引实验
python index_experiments.py
# → 输出各场景 EXPLAIN 和耗时对比表
```

### 7.5 预期结论（实测数据，SF=0.01）

下表是 `index_experiments.py` 在 SQLite 上的实测结果（ms，5 次平均）。**注意：索引不是万能的**——Q1 单列索引反而比无索引慢，因为聚合查询要回表读多列，索引扫描+回表不如顺序全表扫。

| 查询 | 无索引 | 单列索引 | 复合索引 | 覆盖索引 | 结论 |
|---|---|---|---|---|---|
| Q1 聚合 | 21.6 | 27.9 (0.8x) | 21.1 (1.0x) | **8.0 (2.7x)** | 聚合查询单列索引收益为负，**覆盖索引**才有效 |
| Q2 点查 | 1.15 | **0.05 (22x)** | 0.05 (23x) | 0.05 (22x) | 点查受益最大，任何索引都极速 |
| Q3 范围+JOIN | 3.19 | 3.26 (1.0x) | 2.87 (1.1x) | **1.67 (1.9x)** | 结果集小时索引收益有限，覆盖索引最优 |
| Q4 日期范围+JOIN | 1.85 | 0.63 (2.9x) | 0.57 (3.2x) | **0.44 (4.2x)** | 覆盖索引对 JOIN 最有效 |
| Q5 LIKE+JOIN | 1.52 | 0.48 (3.2x) | 0.57 (2.7x) | **0.36 (4.2x)** | 前缀 LIKE 走索引，覆盖索引最优 |

### 7.6 实验结果详细分析

**关键教训**：

1. **Q1 反直觉**：单列索引比无索引更慢！因为 Q1 是聚合（GROUP BY），过滤后仍返回 20% 行（1.2 万行），每行回表读 7 列做聚合。索引扫描（随机 IO）+ 回表 > 顺序全表扫。**解法：覆盖索引**，把聚合列都放进索引，免回表，快 2.7x。

2. **Q2 收益最大**：点查（`c_custkey = 100`）从 1.15ms 降到 0.05ms，22 倍。索引对高选择性点查最有效。

3. **Q3 收益最小**：结果集 1047 行（占 7%），全表扫 15000 行也不慢，索引收益有限。**教训：小表/高命中率查询不需要索引**。

4. **覆盖索引普遍最优**：Q1/Q3/Q4/Q5 都是覆盖索引最快，因为消除回表。

5. **复合索引未必比单列好**：Q5 复合（0.57ms）比单列（0.48ms）慢，因为复合索引更大，扫描成本更高。复合索引只在能用到多列过滤时才占优。

**索引策略选择流程图**：

```
查询是点查（等值）吗？
├─ 是 → 建单列索引，收益最大
└─ 否 → 查询是聚合/多列返回吗？
        ├─ 是 → 建覆盖索引，避免回表
        └─ 否 → 查询有多列过滤吗？
                ├─ 是 → 建复合索引（注意列顺序）
                └─ 否 → 单列索引可能足够
```

### 7.7 EXPLAIN 输出示例

**Q2 点查的 EXPLAIN（有索引 vs 无索引）**：

```
无索引：
  EXPLAIN QUERY PLAN
  SELECT c_name, o_orderkey, o_orderdate, o_total
  FROM customer JOIN orders ON c_custkey = o_custkey
  WHERE c_custkey = 100;
  → SCAN customer
  → SEARCH orders USING AUTOMATIC COVERING INDEX (o_custkey=?)

有索引：
  CREATE INDEX idx_cust ON orders(o_custkey);
  EXPLAIN QUERY PLAN ...
  → SEARCH customer USING PRIMARY KEY (c_custkey=?)
  → SEARCH orders USING INDEX idx_cust (o_custkey=?)
```

---

## 8. 索引维护

### 8.1 创建索引

```sql
-- 基本创建
CREATE INDEX idx_name ON orders(user_id);

-- 并发创建（PG，不阻塞写入）
CREATE INDEX CONCURRENTLY idx_name ON orders(user_id);

-- 指定表空间
CREATE INDEX idx_name ON orders(user_id) TABLESPACE fast_disk;

-- 带填充因子（PG，预留空间减少分裂）
CREATE INDEX idx_name ON orders(user_id) WITH (fillfactor = 90);
```

**CONCURRENTLY 详解**：

```sql
-- 普通 CREATE INDEX：锁表，期间不能写入
CREATE INDEX idx ON orders(user_id);  -- 阻塞 INSERT/UPDATE/DELETE

-- CONCURRENTLY：不锁表，但耗时更长，可能失败
CREATE INDEX CONCURRENTLY idx ON orders(user_id);
-- 注意：失败后索引处于 invalid 状态，要 DROP 重建
```

**大表建索引的注意事项**：

```sql
-- 1. 在低峰期建索引
-- 2. 用 CONCURRENTLY（PG）避免锁表
-- 3. 建完跑 ANALYZE 更新统计信息
CREATE INDEX CONCURRENTLY idx ON orders(user_id);
ANALYZE orders;

-- 4. SQLite 建索引时会锁表，大表要在维护窗口做
```

### 8.2 重建索引

索引会因频繁更新产生**碎片**（bloat），需要定期重建。

```sql
-- PG 重建单个索引
REINDEX INDEX idx_name;

-- PG 重建表的所有索引
REINDEX TABLE orders;

-- PG 并发重建（不锁表，PG 12+）
REINDEX INDEX CONCURRENTLY idx_name;

-- SQLite 重建（SQLite 不支持 REINDEX INDEX，要重建表）
-- 方法1：VACUUM 重建所有索引
VACUUM;

-- 方法2：删除并重建索引
DROP INDEX idx_name;
CREATE INDEX idx_name ON orders(user_id);
```

**什么时候要重建索引**：

| 场景 | 原因 | 频率 |
|---|---|---|
| 频繁 UPDATE/DELETE | 索引项分裂、死元组 | 每周/每月 |
| 大批量 DELETE 后 | 索引稀疏 | 删除后立即 |
| 查询变慢 | 索引碎片导致 IO 增多 | 监控发现时 |
| pg_stat_user_indexes 显示 scans=0 | 索引没用过 | 删除而非重建 |

**查看索引碎片（PG）**：

```sql
-- 查看索引大小和碎片率
SELECT 
    schemaname, relname, indexrelname,
    pg_size_pretty(pg_relation_size(indexrelid)) AS size,
    idx_scan,         -- 索引被使用次数
    idx_tup_read,     -- 索引项读取数
    idx_tup_fetch     -- 回表数
FROM pg_stat_user_indexes
ORDER BY pg_relation_size(indexrelid) DESC;
```

### 8.3 删除索引

```sql
-- 删除索引
DROP INDEX idx_name;

-- PG 并发删除（不锁表）
DROP INDEX CONCURRENTLY idx_name;
```

**删除前检查**：

```sql
-- PG：查索引是否还在被用
SELECT idx_scan FROM pg_stat_user_indexes WHERE indexrelname = 'idx_name';
-- idx_scan = 0 说明从未用过，可以删

-- PG：查索引是否被约束依赖
SELECT * FROM pg_constraint WHERE conindid = 'idx_name'::regclass;
-- 如果被依赖，不能直接删
```

### 8.4 ANALYZE 与统计信息

**ANALYZE** 更新表的统计信息，优化器靠它选择执行计划。

```sql
-- 手动分析表
ANALYZE orders;

-- 分析指定列
ANALYZE orders(user_id, status);

-- PG 自动分析（autovacuum 默认开启）
-- 当改动行数 > 10% 总行数时自动触发
```

**为什么 ANALYZE 重要**：

```
统计信息包含：
  - 表的总行数
  - 每列的不同值数（distinct）
  - 每列的分布直方图
  - 索引的选择性

优化器用这些信息决定：
  - 用哪个索引？→ 选选择性高的
  - 用索引还是全表扫？→ 估算成本
  - JOIN 顺序？→ 估算结果集大小

如果统计信息过期，优化器可能选错计划：
  - 大量 INSERT 后没 ANALYZE → 优化器以为表还小，选全表扫
  - 大量 DELETE 后没 ANALYZE → 优化器以为表还大，选索引扫（但实际全表扫更快）
```

**手动 vs 自动**：

| 方式 | 触发时机 | 适合 |
|---|---|---|
| 手动 ANALYZE | 你主动执行 | 大批量导入后 |
| autovacuum（PG） | 改动 > 10% 时自动 | 日常 OLTP |
| SQLite 无自动 | 需要手动 | 每次大批量改动后 |

### 8.5 VACUUM（PG 特有）

```sql
-- VACUUM：回收死元组空间（不还给 OS）
VACUUM orders;

-- VACUUM FULL：回收空间并还给 OS（锁表）
VACUUM FULL orders;

-- VACUUM ANALYZE：回收 + 更新统计
VACUUM ANALYZE orders;
```

**为什么 PG 需要 VACUUM**：

```
PG 用 MVCC 实现并发：
  UPDATE orders SET status='paid' WHERE id=1;
  → 不修改原行，而是插入新行
  → 旧行标记为 dead，但还占空间
  → VACUUM 回收 dead 元组

如果不 VACUUM：
  - 表越来越大（dead 元组累积）
  - 查询变慢（要跳过 dead 元组）
  - 索引膨胀（索引项指向 dead 元组）
```

---

## 9. 与 miniDB B+Tree 的对照

阶段1 章3 实现了固定阶 B+Tree，本章对比真实数据库的索引实现：

| 维度 | miniDB B+Tree | SQLite B-Tree | PostgreSQL B-Tree |
|---|---|---|---|
| **节点阶** | 固定（编译期） | 动态（按页大小） | 动态（按页大小） |
| **叶子里存** | 键 + TID | 键 + RowID | 键 + TID |
| **叶子链表** | 有（双向） | 有 | 无（靠内部节点遍历） |
| **分裂策略** | 简单折半 | 平衡分裂 | 平衡分裂（fillfactor） |
| **并发控制** | 单线程 | WAL + latch | LRU + crabbing latch |
| **覆盖索引** | ❌ 不支持 | ✅（索引含所有列） | ✅ + INCLUDE |
| **复合索引** | ❌ 单列 | ✅ | ✅ + 列顺序优化 |
| **部分索引** | ❌ | ✅ | ✅ |
| **表达式索引** | ❌ | ✅ | ✅ |
| **其他类型** | 仅 B+Tree | 仅 B-Tree | B-Tree/Hash/GiST/GIN/BRIN/SP-GiST |
| **VACUUM** | 无（原地更新） | 无（原地更新） | 需要（MVCC 死元组） |

### 9.1 源码级启示

阶段1 实现 B+Tree 时遇到的真实问题，在 SQLite/PG 中如何解决：

1. **分裂抖动**：miniDB 用简单折半，可能反复分裂合并；PG 用 fillfactor（默认 90%）预留空间，减少分裂
2. **页内查找**：miniDB 用线性扫；SQLite/PG 用二分（键有序）
3. **大键处理**：miniDB 假设键定长；PG 用 TOAST 把超大键外存
4. **重复键**：miniDB 假设键唯一；PG B-Tree 允许重复（用 TID 作为隐式后缀排序）

### 9.2 功能差异详解

**复合索引实现差异**：

```
miniDB：键是单值，比较函数 compare(a, b)
  → 要支持复合索引，需改成元组比较 compare((a1,b1), (a2,b2))

SQLite/PG：键可以是多列元组，按字典序比较
  → CREATE INDEX idx ON t(a, b, c) 自然支持
  → 比较函数：(a1,b1,c1) < (a2,b2,c3) ⟺ a1<a2 OR (a1=a2 AND b1<b2) OR ...
```

**覆盖索引实现差异**：

```
miniDB：索引项 = (key, TID)
  → 查询要读其他列 → 必须用 TID 回表
  → 无法支持覆盖索引

SQLite/PG：索引项 = (key1, key2, ..., TID, include_cols...)
  → 查询列都在索引里 → 直接返回 include_cols
  → 支持覆盖索引
```

**并发控制差异**：

```
miniDB：单线程，无并发问题

SQLite：WAL + latch
  → 写入时加 latch，防止并发修改
  → WAL 日志保证崩溃恢复

PG：LRU + crabbing latch（蟹步锁）
  → 从根到叶子，每层先锁子节点再放父节点（像螃蟹走路）
  → 防止死锁，允许并发读写
```

---

## 10. 常见误区

### 10.1 误区1：索引越多越好

**错误观念**：给每个列都加索引，查询就快了。

**真相**：索引有代价，太多索引会拖慢写入、浪费空间、甚至让优化器选错。

```
反面例子：
  表 5 个列，给每列都建索引（5 个单列索引）
  
  INSERT 一行：要写 6 处（堆表 + 5 个索引），写入慢 6 倍
  UPDATE 某列：要更新该列的索引，可能还要更新包含该列的复合索引
  
  查询 SELECT * FROM t WHERE a=1 AND b=2：
    优化器可能选 idx_a 或 idx_b，但无法同时用两个（除非 BitmapAnd）
    不如一个复合索引 (a, b) 有效
```

**正确做法**：
- 只给高频查询的列建索引
- 优先复合索引覆盖多个查询
- 定期检查未使用的索引并删除

**查看未使用的索引（PG）**：

```sql
SELECT 
    relname, indexrelname, idx_scan
FROM pg_stat_user_indexes
WHERE idx_scan = 0  -- 从未使用
ORDER BY pg_relation_size(indexrelid) DESC;
```

### 10.2 误区2：为什么不自动加索引

**错误观念**：数据库应该自动给所有列加索引。

**真相**：数据库不知道你要怎么查，自动加索引可能全错。

```
数据库不自动加索引的原因：
  1. 不知道查询模式：哪些列常查？常一起查？常排序？
  2. 索引有代价：自动加会拖慢写入
  3. 索引选择依赖数据分布：选择性低的列不该加
  4. 复合索引的列顺序很关键：数据库猜不到

例外：
  - 主键自动建索引（UNIQUE 索引）
  - UNIQUE 约束自动建索引
  - 外键在 PG 不自动建索引（MySQL InnoDB 会）
```

### 10.3 误区3：选择性计算错误

**错误观念**：列有不同值就适合建索引。

**真相**：要看选择性（distinct / total），低选择性的列建索引没用。

```sql
-- 反面例子：性别列
SELECT COUNT(DISTINCT gender), COUNT(*) FROM users;
-- 2, 1000000 → 选择性 0.000002

CREATE INDEX idx_gender ON users(gender);
SELECT * FROM users WHERE gender = 'M';
-- 优化器选择全表扫，因为索引要回表 50 万次，比全表扫慢
```

**正确做法**：

```sql
-- 先算选择性
SELECT 
    COUNT(DISTINCT col) AS distinct_vals,
    COUNT(*) AS total,
    CAST(COUNT(DISTINCT col) AS FLOAT) / COUNT(*) AS selectivity
FROM t;

-- selectivity > 0.1 才值得建索引
-- selectivity < 0.01 不要建索引
-- 0.01 ~ 0.1 看查询频率决定
```

### 10.4 误区4：EXPLAIN 里看到 Index Scan 就放心了

**错误观念**：执行计划里有 Index Scan 就说明索引有效。

**真相**：Index Scan 可能比 Seq Scan 更慢（随机 IO + 回表）。

```sql
-- 反面例子
EXPLAIN SELECT * FROM orders WHERE status != 'paid';
-- → Index Scan using idx_status  (cost=0.42..1234.56 rows=20000)
--   看起来用了索引，但 cost=1234.56 很高

EXPLAIN SELECT * FROM orders WHERE status != 'paid';
-- → Seq Scan on orders  (cost=0.00..567.89 rows=20000)
--   全表扫 cost=567.89 更低

-- 优化器会选 cost 低的，但如果你强制走索引反而更慢
```

**正确做法**：看 EXPLAIN 的 cost 和实际耗时，不要只看算子名。

### 10.5 误区5：索引能优化所有查询

**错误观念**：慢查询加个索引就好了。

**真相**：有些查询索引帮不了。

| 查询类型 | 索引能帮吗 | 原因 |
|---|---|---|
| `SELECT * FROM t` | ❌ | 没有过滤条件 |
| `SELECT COUNT(*) FROM t` | ❌ | 要数所有行 |
| `WHERE col != 1`（高命中率） | ❌ | 命中行太多 |
| `WHERE f(col) = 1`（不可逆函数） | ⚠️ | 要表达式索引 |
| `ORDER BY random()` | ❌ | 随机排序无法用索引 |
| 聚合 `GROUP BY` 大量组 | ⚠️ | 索引帮助有限 |

### 10.6 误区速查表

| 误区 | 真相 | 正确做法 |
|---|---|---|
| 索引越多越好 | 写入变慢、空间浪费 | 只给高频查询列建 |
| 数据库自动加索引 | 不知道查询模式 | 手动分析后建 |
| 有不同值就建索引 | 要看选择性 | selectivity > 0.1 才建 |
| Index Scan 一定快 | 可能比 Seq Scan 慢 | 看 cost 和实际耗时 |
| 索引能优化所有查询 | 有些查询索引帮不了 | 分析慢查询根因 |

---

## 11. Python 实验：索引对比

完整脚本见 `phase2/03-index-strategy/index_experiments.py`，核心流程：

```python
import sqlite3, time

conn = sqlite3.connect(":memory:")
conn.execute("PRAGMA journal_mode=WAL")

# 建表 + 10万行
conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, a INT, b INT, c TEXT, d REAL)")
# ... 批量插入 ...

def bench(sql, n=100):
    t0 = time.perf_counter()
    for _ in range(n):
        conn.execute(sql).fetchall()
    return (time.perf_counter() - t0) / n * 1000  # ms

# 1. 无索引
print("无索引:", bench("SELECT * FROM t WHERE a = 500"))

# 2. 加 B-Tree 索引
conn.execute("CREATE INDEX idx_a ON t(a)")
print("B-Tree:", bench("SELECT * FROM t WHERE a = 500"))

# 3. EXPLAIN
print(conn.execute("EXPLAIN QUERY PLAN SELECT * FROM t WHERE a = 500").fetchall())
```

### 典型输出

```
实验1：无索引 vs B-Tree（10万行，等值 a=500）
  等值 无索引        7.6 ms    SCAN t
  等值 B-Tree        0.15 ms   SEARCH t USING INDEX idx_a     (52x)
  范围 无索引       11.1 ms    SCAN t
  范围 B-Tree        7.9 ms    SEARCH t USING INDEX idx_a (a>? AND a<?)  (1.4x)

实验2：复合索引 (a,b,c) 最左前缀
  ✅ a=500 AND b=250          0.006 ms   USING INDEX idx_abc (a=? AND b=?)
  ✅ a=500                    0.18  ms   USING INDEX idx_abc (a=?)
  ✅ b=250 (ANY(a))           4.5   ms   USING INDEX idx_abc (ANY(a) AND b=?)  ← 部分利用
  ❌ c LIKE 'str_0001%'      11.2  ms   SCAN t  ← 缺最左 a,b
  ✅ a=500 AND c LIKE ...     0.03  ms   USING INDEX idx_abc (a=?)

实验3：覆盖索引
  idx(a) 非覆盖 SELECT a,b    0.090 ms   USING INDEX idx_a_only
  idx(a,b) 覆盖 SELECT a,b    0.069 ms   USING COVERING INDEX idx_a_b_cover  (1.3x)
  idx(a,b) 退化 SELECT a,b,d  0.119 ms   USING INDEX idx_a_b_cover  ← d 不在索引，回表

实验4：索引失效
  ✅ LIKE 'str_0001%' (覆盖)  走索引
  ❌ LIKE '%0001%'            SCAN t  ← 前导 %
  ✅ OR a=1 OR b=2            MULTI-INDEX OR  ← SQLite 优化为 BitmapOr
  ❌ a + 1 = 501              SCAN t  ← 列在表达式
  ❌ a != 500                 SCAN t  ← 不等式选择性低
  ❌ UPPER(c) = 'STR_...'     SCAN t  ← 函数作用于列
  ✅ UPPER(c) 表达式索引       走索引 idx_c_upper
```

---

## 文件清单

| 文件 | 内容 |
|---|---|
| `phase2/03-index-strategy/index_experiments.py` | SQLite 索引对比实验（无索引/B-Tree/复合/覆盖/失效） |
| `phase2/03-index-strategy/generate_tpch.py` | 生成简化 TPC-H 数据集 + Q1-Q5 执行 |
| `phase2/03-index-strategy/queries/tpch_queries.sql` | TPC-H Q1-Q5 标准 SQL |
| `phase2/03-index-strategy/data/tpch.db` | 生成的 SQLite 数据库（运行后产生） |

---

## 习题

1. **类型选择**：对一个 1 亿行的时序日志表，按 `ts` 范围查询，选 B-Tree 还是 BRIN？为什么？估算两种索引的大小。

2. **最左前缀**：索引 `(a, b, c, d)`，下列查询各能用上几列？
   - `WHERE a=1 AND b=2 AND c=3 AND d=4`
   - `WHERE a=1 AND c=3`
   - `WHERE a=1 AND b>2 AND c=3`
   - `WHERE b=2 AND c=3`

3. **覆盖索引设计**：表 `orders(id, user_id, status, total, created_at, remark)`，高频查询 `SELECT status, total FROM orders WHERE user_id=? AND status='paid'`。设计一个覆盖索引，并写出 EXPLAIN 预期。

4. **失效修复**：下列查询走不了索引，给出修复方案：
   ```sql
   SELECT * FROM users WHERE DATE(created_at) = '2024-09-14';
   SELECT * FROM products WHERE name LIKE '%手机%';
   SELECT * FROM orders WHERE amount * 1.1 > 100;
   ```

5. **TPC-H 实验**：运行 `index_experiments.py`，记录 Q1-Q5 在四种索引策略下的耗时，画成柱状图，分析哪个查询收益最大、哪个收益最小，解释原因。

6. **对照 miniDB**：阶段1 miniDB 的 B+Tree 不支持复合索引，若要扩展，最小改动是什么？提示：键的比较函数从单值改为元组字典序。

7. **部分索引**：表 `tasks(id, status, assigned_to)`，90% 任务是 `status='done'`，常查 `status='pending' AND assigned_to=?`。设计部分索引，估算比普通索引小多少倍。

8. **选择性计算**：给定一个表 `products(id, category, brand, price)`，100 万行。已知 `category` 有 50 个不同值，`brand` 有 10000 个不同值，`price` 几乎每行不同。计算每列的选择性，判断哪些列适合单独建索引，哪些适合复合索引。
