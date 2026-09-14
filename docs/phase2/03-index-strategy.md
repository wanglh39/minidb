# 章3：索引策略实战

> 索引是数据库性能优化的第一利器。本章从索引的本质出发，对比五大索引类型，深入复合索引、覆盖索引、索引失效场景，并在 TPC-H 数据集上做对比实验，最后与阶段1 miniDB 的 B+Tree 实现做源码级对照。

## 索引的本质与代价

### 什么是索引

索引是**以空间换时间**的数据结构：在原表（堆表）之外，额外维护一份有序的、按某（组）列排序的"目录"，让查询不必全表扫描（Sequential Scan），而是通过目录快速定位。

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

### 索引的代价

| 代价维度 | 说明 | 量化 |
|---|---|---|
| **存储** | 索引本身占空间 | B-Tree 约 1.2~1.5 × 键大小 |
| **写入放大** | INSERT/UPDATE/DELETE 要同步维护索引 | 每个索引 +1 次写 |
| **维护** | VACUUM/ANALYZE 要维护索引 | 死索引项回收 |
| **选择错误** | 选错索引反而更慢 | 小表全表扫更快 |

> **核心权衡**：读多写少的列适合建索引；频繁更新的列建索引要谨慎。一个经验法则——**索引选择性 > 0.1**（即 `distinct_count / total_count > 0.1`）才值得建。

## 五大索引类型对比

PostgreSQL 内置五种索引访问方法（Access Method），SQLite 主要用 B-Tree（含 `WITHOUT ROWID` 聚簇变体）。下表是完整对比：

| 类型 | 结构 | 支持范围扫描 | 支持等值 | 典型场景 | 维护成本 |
|---|---|---|---|---|---|
| **B-Tree** | 平衡多路搜索树 | ✅ 快 | ✅ | 通用，排序，`<` `>` `BETWEEN` | 中 |
| **Hash** | 哈希表（桶+链） | ❌ | ✅ 极快 | 纯等值查询（`=`） | 低 |
| **GiST** | 广义搜索树 | ✅ | ✅ | 几何、范围、全文、最近邻 | 高 |
| **GIN** | 倒排索引 | ✅ | ✅ | 数组包含、全文搜索 `tsvector` | 很高 |
| **BRIN** | 块范围摘要 | ✅ 近似 | ✅ 近似 | 时序、按物理顺序有序的大表 | 极低 |

### B-Tree（默认）

```
              [40 | 80]                ← 内部节点（键+指针）
             /    |    \
       [10 20 30] [40 50 60 70] [80 90]   ← 叶子节点（有序链表）
```

- **适用**：等值、范围、排序、`IS NULL`、`IN`
- **PG**：`CREATE INDEX idx ON t(col);` 默认 B-Tree
- **SQLite**：所有索引都是 B-Tree（PG 风格的 B-Tree，非 B+Tree，但叶子也存键）

### Hash

```sql
-- PostgreSQL
CREATE INDEX idx_hash ON t(col) USING hash;
-- 仅支持 col = ? 查询；不支持范围、排序
```

- **优势**：等值查询 O(1)，比 B-Tree O(log N) 略快
- **限制**：不支持范围扫描、排序、`IS NULL`、参与 JOIN
- **PG 10+**：WAL 日志支持，可复制（之前不推荐生产用）

### GiST（Generalized Search Tree）

```sql
-- 几何包含查询
CREATE INDEX idx_box ON regions USING gist (bbox);
SELECT * FROM regions WHERE bbox && box '(0,0),(1,1)';

-- 范围类型
CREATE INDEX idx_range ON events USING gist (during);
SELECT * FROM events WHERE during && tsrange('[2024-01-01,2024-02-01)');
```

- **适用**：`<@` `@>` `&&` `<<` `>>` 等非标准运算符
- **典型**：PostGIS 几何索引、`tsrange` 时间范围、`ltree` 层级

### GIN（Generalized Inverted Index）

```sql
-- 数组包含
CREATE INDEX idx_tags ON posts USING gin (tags);
SELECT * FROM posts WHERE tags @> ARRAY['db','index'];

-- 全文搜索
CREATE INDEX idx_fts ON docs USING gin (to_tsvector('chinese', body));
SELECT * FROM docs WHERE to_tsvector('chinese', body) @@ plainto_tsquery('索引 失效');
```

- **适用**：`@>` `<@` `&&` `@@`（全文匹配）
- **代价**：构建慢（每元素一个倒排项），但查询极快

### BRIN（Block Range Index）

```sql
-- 时序大表，时间列物理有序
CREATE INDEX idx_brin ON logs USING brin (ts);
SELECT * FROM logs WHERE ts BETWEEN '2024-09-01' AND '2024-09-02';
```

- **原理**：每 128 个数据块存一个 `[min, max]` 摘要，查询时跳过不重叠的块
- **优势**：索引极小（几 KB / GB 数据），维护几乎免费
- **前提**：列值在物理存储上**大致有序**（如时序追加写入）

### 选择决策树

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

## 复合索引与最左前缀原则

### 复合索引的物理布局

复合索引 `(a, b, c)` 在 B-Tree 中按 `a` 升序、`a` 相同按 `b`、`b` 相同按 `c` 排序：

```
索引项顺序（a, b, c）:
(1,1,1) (1,1,2) (1,2,1) (1,2,3) (2,1,1) (2,1,5) (2,3,2) (3,1,1) ...

等价于：先按 a 分桶，桶内按 b 分桶，桶内按 c 排序
```

### 最左前缀原则

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

### 列顺序设计原则

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

**设计法则**

1. **等值列在前，范围列在后**：`WHERE status='paid' AND created_at > '2024-01-01'` → `(status, created_at)`
2. **高选择性在前**：让索引更快收敛
3. **排序列最后**：利用索引有序性避免 Sort 算子
4. **不要盲目加列**：每多一列，索引更大、维护更慢

## 覆盖索引（Index-Only Scan）

### 原理

如果查询的列**全部在索引中**，就不必回表（不必读堆表对应 TID 的数据），直接从索引返回——即 **Index-Only Scan**。

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

### 可见性映射（Visibility Map）

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

### SQLite 的覆盖索引

```sql
-- SQLite EXPLAIN QUERY PLAN 出现 "COVERING INDEX" 即 Index-Only
CREATE INDEX idx_cover ON orders(user_id, status, total);

EXPLAIN QUERY PLAN
SELECT status, total FROM orders WHERE user_id = 100;
-- > SEARCH orders USING COVERING INDEX idx_cover (user_id=?)
```

> **对比 miniDB**：阶段1 miniDB 的 B+Tree 索引只存键+TID，不支持覆盖索引（必须回表）。真实数据库的 B-Tree 索引可以 INCLUDE 额外列实现覆盖。

### INCLUDE 语法（PG 11+）

```sql
-- 把不参与过滤但需要返回的列 INCLUDE 进索引
CREATE INDEX idx_user_include ON orders(user_id) INCLUDE (status, total);

-- user_id 用于过滤，status/total 用于返回，都不回表
SELECT status, total FROM orders WHERE user_id = 100;
```

INCLUDE 列**不参与索引排序**，只存叶子节点，维护成本低于完整复合索引。

## 索引失效场景

### 场景1：函数作用于索引列

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

### 场景2：类型隐式转换

```sql
-- phone 是 VARCHAR，但查询传了数字
CREATE INDEX idx_phone ON users(phone);

SELECT * FROM users WHERE phone = 13800138000;  -- ❌ 隐式转 phone 为 numeric
SELECT * FROM users WHERE phone = '13800138000';  -- ✅
```

PG 一般不会隐式转字符串为数字（会报错），但 MySQL 会，是常见踩坑点。

### 场景3：LIKE 前导通配符

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

### 场景4：OR 条件

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

### 场景5：不等式与 NOT

```sql
CREATE INDEX idx_status ON orders(status);

SELECT * FROM orders WHERE status != 'paid';  -- ⚠️ 选择性低时优化器放弃
SELECT * FROM orders WHERE NOT (status = 'paid');  -- 同上
```

`!=` `<>` `NOT IN` 通常导致全表扫，因为命中行数可能占绝大多数，索引反而更慢。

### 场景6：索引列参与运算

```sql
CREATE INDEX idx_age ON users(age);

SELECT * FROM users WHERE age + 1 = 18;  -- ❌ 列在表达式里
SELECT * FROM users WHERE age = 17;  -- ✅ 把运算移到常量侧
```

### 失效场景速查表

| 场景 | 失效写法 | 修复写法 |
|---|---|---|
| 函数 | `WHERE f(col) = 1` | `WHERE col = f_inv(1)` 或表达式索引 |
| 类型转换 | `WHERE varchar_col = 123` | `WHERE varchar_col = '123'` |
| LIKE 前导% | `WHERE col LIKE '%x'` | trigram / 全文索引 |
| OR 跨列 | `WHERE a=1 OR b=2` | UNION ALL / BitmapOr |
| 不等式 | `WHERE col != 1` | 难修，考虑部分索引 |
| 列运算 | `WHERE col+1 = 2` | `WHERE col = 1` |

## 部分索引与表达式索引

### 部分索引（Partial Index）

只对满足条件的行建索引，索引更小、维护更省。

```sql
-- 只对未支付订单建索引（占全表 5%）
CREATE INDEX idx_unpaid ON orders(created_at) WHERE status = 'unpaid';

-- 查询必须包含相同条件才能用
SELECT * FROM orders WHERE status = 'unpaid' AND created_at > '2024-09-01';  -- ✅
SELECT * FROM orders WHERE created_at > '2024-09-01';  -- ❌ 条件不匹配
```

**适用**：稀疏状态（如 `is_deleted = false`）、热点子集（如 `region = 'CN'`）。

### 表达式索引

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

## TPC-H 数据集对比实验

### TPC-H 简介

TPC-H 是决策支持系统的标准基准，包含 8 张表和 22 个查询。本章用简化版（3 表 + 5 查询）做索引对比。

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

### 实验设计

对 5 个标准查询，分别测：无索引、单列索引、复合索引、覆盖索引的耗时和执行计划。

| 查询 | 业务含义 | 关键过滤条件 | 适合的索引 |
|---|---|---|---|
| Q1 | 价目汇总 | `l_shipdate <= date` | `(l_shipdate)` |
| Q2 | 按客户查订单 | `c_custkey = ?` | `(c_custkey)` |
| Q3 | 按状态+日期查 | `o_status='F' AND o_orderdate < d` | `(o_status, o_orderdate)` |
| Q4 | 按日期范围+客户 | `o_orderdate BETWEEN d1 AND d2 AND c_region='ASIA'` | 复合+JOIN |
| Q5 | 按客户名前缀 | `c_name LIKE 'Customer_0000001%'` | `(c_name)` |

### 运行实验

```bash
cd phase2/03-index-strategy

# 1. 生成 TPC-H 数据
python generate_tpch.py
# → data/tpch.db (SQLite, ~10MB)

# 2. 运行索引实验
python index_experiments.py
# → 输出各场景 EXPLAIN 和耗时对比表
```

### 预期结论（实测数据，SF=0.01）

下表是 `index_experiments.py` 在 SQLite 上的实测结果（ms，5 次平均）。**注意：索引不是万能的**——Q1 单列索引反而比无索引慢，因为聚合查询要回表读多列，索引扫描+回表不如顺序全表扫。

| 查询 | 无索引 | 单列索引 | 复合索引 | 覆盖索引 | 结论 |
|---|---|---|---|---|---|
| Q1 聚合 | 21.6 | 27.9 (0.8x) | 21.1 (1.0x) | **8.0 (2.7x)** | 聚合查询单列索引收益为负，**覆盖索引**才有效 |
| Q2 点查 | 1.15 | **0.05 (22x)** | 0.05 (23x) | 0.05 (22x) | 点查受益最大，任何索引都极速 |
| Q3 范围+JOIN | 3.19 | 3.26 (1.0x) | 2.87 (1.1x) | **1.67 (1.9x)** | 结果集小时索引收益有限，覆盖索引最优 |
| Q4 日期范围+JOIN | 1.85 | 0.63 (2.9x) | 0.57 (3.2x) | **0.44 (4.2x)** | 覆盖索引对 JOIN 最有效 |
| Q5 LIKE+JOIN | 1.52 | 0.48 (3.2x) | 0.57 (2.7x) | **0.36 (4.2x)** | 前缀 LIKE 走索引，覆盖索引最优 |

**关键教训**：

1. **Q1 反直觉**：单列索引比无索引更慢！因为 Q1 是聚合（GROUP BY），过滤后仍返回 20% 行（1.2 万行），每行回表读 7 列做聚合。索引扫描（随机 IO）+ 回表 > 顺序全表扫。**解法：覆盖索引**，把聚合列都放进索引，免回表，快 2.7x。
2. **Q2 收益最大**：点查（`c_custkey = 100`）从 1.15ms 降到 0.05ms，22 倍。索引对高选择性点查最有效。
3. **Q3 收益最小**：结果集 1047 行（占 7%），全表扫 15000 行也不慢，索引收益有限。**教训：小表/高命中率查询不需要索引**。
4. **覆盖索引普遍最优**：Q1/Q3/Q4/Q5 都是覆盖索引最快，因为消除回表。
5. **复合索引未必比单列好**：Q5 复合（0.57ms）比单列（0.48ms）慢，因为复合索引更大，扫描成本更高。复合索引只在能用到多列过滤时才占优。

## 与 miniDB B+Tree 的对照

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

### 源码级启示

阶段1 实现 B+Tree 时遇到的真实问题，在 SQLite/PG 中如何解决：

1. **分裂抖动**：miniDB 用简单折半，可能反复分裂合并；PG 用 fillfactor（默认 90%）预留空间，减少分裂
2. **页内查找**：miniDB 用线性扫；SQLite/PG 用二分（键有序）
3. **大键处理**：miniDB 假设键定长；PG 用 TOAST 把超大键外存
4. **重复键**：miniDB 假设键唯一；PG B-Tree 允许重复（用 TID 作为隐式后缀排序）

## Python 实验：索引对比

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

## 文件清单

| 文件 | 内容 |
|---|---|
| `phase2/03-index-strategy/index_experiments.py` | SQLite 索引对比实验（无索引/B-Tree/复合/覆盖/失效） |
| `phase2/03-index-strategy/generate_tpch.py` | 生成简化 TPC-H 数据集 + Q1-Q5 执行 |
| `phase2/03-index-strategy/queries/tpch_queries.sql` | TPC-H Q1-Q5 标准 SQL |
| `phase2/03-index-strategy/data/tpch.db` | 生成的 SQLite 数据库（运行后产生） |

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