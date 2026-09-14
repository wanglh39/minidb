# 章6：查询性能调优

> 同一条 SQL，执行计划不同，性能可以差 1000 倍。本章讲透 EXPLAIN、执行计划节点、统计信息、Join 策略、子查询优化与常见反模式，并用 SQLite 复现 5 类真实慢查询的调优全过程。

## 为什么查询调优是后端工程师的核心技能

```sql
-- 同一个查询，两种执行计划
SELECT * FROM orders WHERE customer_id = 42;

-- 计划A：顺序扫描（全表 1000 万行）        →  耗时 2.3 秒
-- 计划B：索引扫描（B+Tree 定位 12 行）      →  耗时 0.2 毫秒
-- 差距：11500 倍
```

数据库内核的**优化器**（Optimizer）负责为 SQL 选择执行计划，但它不是万能的：

1. **统计信息可能过期**：优化器靠统计信息估算代价，统计过期则选错计划
2. **SQL 写法可能限制优化**：子查询、OR、函数包裹索引列等写法会让优化器无法选择最优计划
3. **索引可能缺失**：没有合适的索引，优化器只能全表扫描
4. **数据分布可能特殊**：倾斜数据、低基数列等场景下，优化器的通用代价模型不准

因此，**读懂执行计划、识别反模式、手动干预**是后端工程师的必备技能。

---

## EXPLAIN 详解

### PostgreSQL：EXPLAIN 与 EXPLAIN ANALYZE

```sql
-- EXPLAIN：只显示计划，不执行，基于统计信息估算代价
EXPLAIN SELECT * FROM orders WHERE customer_id = 42;

--  QUERY PLAN
--  Index Scan using idx_orders_customer on orders  (cost=0.42..8.44 rows=1 width=74)
--    Index Cond: (customer_id = 42)

-- EXPLAIN ANALYZE：真正执行，显示实际行数和耗时
EXPLAIN ANALYZE SELECT * FROM orders WHERE customer_id = 42;

--  QUERY PLAN
--  Index Scan using idx_orders_customer on orders  (cost=0.42..8.44 rows=1 width=74)
--    (actual time=0.015..0.016 rows=1 loops=1)
--    Index Cond: (customer_id = 42)
--  Planning Time: 0.083 ms
--  Execution Time: 0.021 ms
```

**输出字段解读**：

| 字段 | 含义 | 来源 |
|---|---|---|
| `cost=0.42..8.44` | 启动代价..总代价（以 `seq_page_cost=1.0` 为单位） | 估算 |
| `rows=1` | 预计输出行数 | 估算（基于统计信息） |
| `width=74` | 预计每行平均字节数 | 估算 |
| `actual time=0.015..0.016` | 实际启动耗时..实际完成耗时（毫秒） | 实测 |
| `rows=1`（actual 行） | 实际输出行数 | 实测 |
| `loops=1` | 该节点执行次数 | 实测 |
| `Planning Time` | 优化器生成计划耗时 | 实测 |
| `Execution Time` | 执行计划耗时 | 实测 |

> **关键**：`EXPLAIN` 的 `rows` 是估算值，`EXPLAIN ANALYZE` 的 `rows` 是实际值。两者差距大说明统计信息不准。

#### EXPLAIN 的详细选项

```sql
-- 显示完整计划 + 实际执行 + 输出统计 + 内存 + WAL
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT customer_id, count(*) FROM orders GROUP BY customer_id;

-- JSON 格式（便于程序解析）
EXPLAIN (ANALYZE, FORMAT JSON)
SELECT * FROM orders WHERE customer_id = 42;
```

| 选项 | 作用 |
|---|---|
| `ANALYZE` | 实际执行查询 |
| `VERBOSE` | 显示内部信息（如目标列名） |
| `BUFFERS` | 显示缓冲命中信息（需配合 ANALYZE） |
| `WAL` | 显示 WAL 记录数（需配合 ANALYZE） |
| `TIMING` | 关闭可加速 ANALYZE（`TIMING OFF`） |
| `FORMAT` | TEXT / JSON / YAML / XML |

### SQLite：EXPLAIN 与 EXPLAIN QUERY PLAN

SQLite 提供两个层次的 EXPLAIN：

```sql
-- EXPLAIN：输出 VDBE 字节码（虚拟机指令）
EXPLAIN SELECT * FROM orders WHERE customer_id = 42;

-- addr  opcode         p1  p2  p3  p4       p5  comment
-- 0     Init           0   12  0   NULL     0   NULL
-- 1     OpenRead       0   2   0   2        0   NULL
-- 2     OpenRead       1   3   0   k(2,,)   2   NULL
-- 3     Integer        42  1   0   NULL     0   NULL
-- 4     SeekGE         1   11  1   1        0   NULL
-- 5     IdxGT          1   11  1   1        0   NULL
-- ...

-- EXPLAIN QUERY PLAN：输出高级查询计划（推荐日常使用）
EXPLAIN QUERY PLAN SELECT * FROM orders WHERE customer_id = 42;

-- id  parent  notused  detail
-- 3   0       62       SEARCH orders USING INDEX idx_orders_customer (customer_id=?)
```

**`EXPLAIN QUERY PLAN` 输出字段**：

| 字段 | 含义 |
|---|---|
| `id` | 当前节点的编号 |
| `parent` | 父节点编号（构成计划树） |
| `notused` | 内部保留字段 |
| `detail` | 节点描述（最重要） |

**`detail` 中的关键关键字**：

| 关键字 | 含义 | 对应 PG 节点 |
|---|---|---|
| `SCAN` | 全表扫描 | Seq Scan |
| `SEARCH ... USING INDEX` | 索引查找 | Index Scan |
| `SEARCH ... USING COVERING INDEX` | 覆盖索引扫描（不回表） | Index Only Scan |
| `AUTOMATIC INDEX` | 自动创建临时索引 | 无（SQLite 特有） |
| `CONSTRAINT` | 约束检查 | 无 |
| `CORRELATED SCALAR SUBQUERY` | 相关子查询 | InitPlan / SubPlan |

```sql
-- 对比：无索引 vs 有索引
EXPLAIN QUERY PLAN SELECT * FROM orders WHERE customer_id = 42;
-- 无索引：SCAN orders                          ← 全表扫描
-- 有索引：SEARCH orders USING INDEX idx (c=?)  ← 索引查找
```

> **对比 miniDB**：miniDB 的优化器只输出"使用哪个索引"的简单信息，SQLite 输出完整的 VDBE 字节码或查询计划树。miniDB 没有 `EXPLAIN ANALYZE`（不实际执行），PG 的 `EXPLAIN ANALYZE` 是最强大的调优工具。

---

## 执行计划节点类型

### 节点树结构

执行计划是一棵**树**，自底向上执行，每个节点产生行流给父节点消费：

```
-- EXPLAIN SELECT c.name, count(*)
-- FROM customers c JOIN orders o ON c.id = o.customer_id
-- GROUP BY c.name ORDER BY count(*) DESC LIMIT 10;

         ┌── Limit (10)
         │
    └── Sort (count DESC)
        │
   └── HashAggregate (group by c.name)
       │
  └── Hash Join
      ├── Hash Cond: c.id = o.customer_id
      │
      ├── ┌── Seq Scan on orders o
      │   └── (构建 Hash 表，内表)
      │
      └── ┌── Seq Scan on customers c
          └── (探测 Hash 表，外表)
```

### 扫描节点

| 节点 | 触发条件 | 代价模型 | 说明 |
|---|---|---|---|
| **Seq Scan** | 无可用索引，或全表扫描更便宜 | `seq_page_cost × pages + cpu_tuple_cost × tuples` | 顺序读所有页 |
| **Index Scan** | 有索引且选择性好 | `random_page_cost × index_pages + cpu_tuple_cost × tuples` | 索引定位 + 回表取数据 |
| **Index Only Scan** | 索引包含所有查询列 | `random_page_cost × index_pages` | 不回表（需 Visibility Map） |
| **Bitmap Index Scan** | 多个索引条件 OR/AND | `random_page_cost × matched_pages` | 先构建位图，再批量取页 |
| **Bitmap Heap Scan** | 配合 Bitmap Index Scan | `seq_page_cost × pages` | 按位图取页 |
| **Tid Scan** | `WHERE ctid = '(0,1)'` | 极低 | 直接按物理位置取 |
| **Sample Scan** | `TABLESAMPLE` | 按采样率 | 用于统计采样 |

```sql
-- Seq Scan
EXPLAIN SELECT * FROM large_table WHERE non_indexed_col = 1;
-- Seq Scan on large_table  (cost=0.00..188346.00 rows=1 width=100)

-- Index Scan
EXPLAIN SELECT * FROM large_table WHERE indexed_col = 1;
-- Index Scan using idx on large_table  (cost=0.42..8.44 rows=1 width=100)

-- Index Only Scan（覆盖索引）
EXPLAIN SELECT indexed_col FROM large_table WHERE indexed_col = 1;
-- Index Only Scan using idx on large_table  (cost=0.42..4.44 rows=1 width=4)

-- Bitmap Scan（多条件）
EXPLAIN SELECT * FROM large_table WHERE col_a = 1 AND col_b = 2;
-- Bitmap Heap Scan on large_table
--   -> Bitmap And
--        -> Bitmap Index Scan on idx_a
--        -> Bitmap Index Scan on idx_b
```

### Join 节点

| 节点 | 时间复杂度 | 内存 | 适用场景 |
|---|---|---|---|
| **Nested Loop Join** | O(R × S) | O(1) | 外表小、内表有索引、结果集小 |
| **Hash Join** | O(R + S) | O(S) | 等值连接、内表可放入内存 |
| **Merge Join** | O(R + S) | O(1) | 等值连接、两侧已排序 |

> R = 外表行数，S = 内表行数

```sql
-- Nested Loop（外表小，内表有索引）
EXPLAIN SELECT * FROM small_table s JOIN large_table l ON s.id = l.small_id;
-- Nested Loop
--   -> Seq Scan on small_table s
--   -> Index Scan using idx on large_table l  (Index Cond: small_id = s.id)

-- Hash Join（等值连接，内表可放内存）
EXPLAIN SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id;
-- Hash Join
--   Hash Cond: (o.customer_id = c.id)
--   -> Seq Scan on orders o
--   -> Hash
--        -> Seq Scan on customers c

-- Merge Join（两侧有序）
EXPLAIN SELECT * FROM a JOIN b ON a.id = b.id;
-- Merge Join
--   Merge Cond: (a.id = b.id)
--   -> Index Scan using a_pkey on a
--   -> Index Scan using b_pkey on b
```

### 其他常见节点

| 节点 | 作用 | 示例 |
|---|---|---|
| **Sort** | 排序（内存或溢出到磁盘） | `ORDER BY`、Merge Join 前的排序 |
| **Aggregate** | 聚合（如 count/sum） | `SELECT count(*)` |
| **HashAggregate** | 哈希聚合（内存中按分组键哈希） | `GROUP BY` |
| **GroupAggregate** | 排序聚合（需先排序） | `GROUP BY` + `ORDER BY` |
| **Limit** | 截断行数 | `LIMIT N` |
| **Unique** | 去重 | `DISTINCT` |
| **Gather** | 并行汇总 | 并行查询 |
| **Gather Merge** | 并行有序汇总 | 并行 + 有序输出 |
| **Materialize** | 物化子结果到内存 | 子查询复用 |
| **WindowAgg** | 窗口函数 | `OVER (...)` |
| **CTE Scan** | 读取 CTE 结果 | `WITH ...` |
| **Subquery Scan** | 子查询输出 | 子查询作为表 |
| **Result** | 常量输出 | `SELECT 1` |

```sql
-- Sort + Limit
EXPLAIN SELECT * FROM orders ORDER BY amount DESC LIMIT 10;
-- Limit
--   -> Sort  (Sort Key: amount DESC)
--        -> Seq Scan on orders

-- HashAggregate
EXPLAIN SELECT customer_id, count(*) FROM orders GROUP BY customer_id;
-- HashAggregate
--   Group Key: customer_id
--   -> Seq Scan on orders

-- 并行查询（PG）
EXPLAIN SELECT count(*) FROM large_table;
-- Finalize Aggregate
--   -> Gather
--        -> Partial Aggregate
--             -> Parallel Seq Scan on large_table
```

### Sort 节点的代价

```sql
-- Sort 的代价与内存有关
SHOW work_mem;  -- 默认 4MB

-- 当排序数据 > work_mem 时，溢出到磁盘（临时文件）
EXPLAIN ANALYZE SELECT * FROM large_table ORDER BY random_col;
-- Sort  (cost=... rows=1000000 ...)
--   Sort Method: external merge  Disk: 31248kB   ← 溢出磁盘！
--   -> Seq Scan on large_table

-- 增大 work_mem 后
SET work_mem = '256MB';
EXPLAIN ANALYZE SELECT * FROM large_table ORDER BY random_col;
-- Sort  (cost=... rows=1000000 ...)
--   Sort Method: quicksort  Memory: 100000kB   ← 内存排序，更快
--   -> Seq Scan on large_table
```

> **调优要点**：`Sort Method: external merge` 出现时，考虑增大 `work_mem` 或加索引（有序输出可避免 Sort）。

---

## 统计信息

优化器靠统计信息估算每个操作的代价和输出行数。统计信息过期是计划变差的首要原因。

### PostgreSQL：pg_statistic

```sql
-- pg_statistic 存储列级统计信息（系统目录表）
SELECT starelid::regclass AS table, attname AS column,
       stadistinct,       -- 不同值数量（或负数表示比例）
       stanullfrac,       -- NULL 比例
       stawidth           -- 平均宽度（字节）
FROM pg_statistic s JOIN pg_attribute a
  ON a.attrelid = s.starelid AND a.attnum = s.staattnum
WHERE starelid = 'orders'::regclass;
```

**最关键的统计信息：MCV（Most Common Values，最常见值）和直方图**：

```sql
-- 查看列的 MCV 和直方图（pg_stats 是友好视图）
SELECT * FROM pg_stats WHERE tablename = 'orders' AND attname = 'status';

-- tablename | attname | null_frac | n_distinct | most_common_vals | most_common_freqs | histogram_bounds
-- orders    | status  | 0          | 4          | {shipped,pending,paid,cancelled} | {0.4,0.3,0.2,0.1} | {...}
```

| 字段 | 含义 | 用途 |
|---|---|---|
| `null_frac` | NULL 比例 | 估算 `IS NULL` 选择度 |
| `n_distinct` | 不同值数量 | 估算 `GROUP BY` 输出行数 |
| `most_common_vals` | 最常见值 | 估算等值条件选择度 |
| `most_common_freqs` | 最常见值频率 | 配合 MCV |
| `histogram_bounds` | 直方图边界 | 估算范围条件选择度 |

**选择度估算示例**：

```sql
-- status = 'shipped' 的选择度
-- 从 MCV 查到 'shipped' 频率 = 0.4
-- 估算行数 = 表行数 × 0.4 = 1000000 × 0.4 = 400000

-- amount > 500 的选择度
-- 从直方图估算：500 落在第 7 个桶（共 10 桶）
-- 估算行数 = 表行数 × (10 - 7) / 10 = 1000000 × 0.3 = 300000
```

### ANALYZE 命令

```sql
-- 手动收集统计信息
ANALYZE orders;                    -- 单表
ANALYZE orders(customer_id);       -- 单列
ANALYZE;                           -- 全库

-- 控制采样率（默认 100，即 100 × 30000 行采样）
ALTER TABLE orders SET (autovacuum_analyze_scale_factor = 0.05);
SHOW default_statistics_target;    -- 默认 100，范围 1-10000

-- 提高统计精度（更精确但更慢）
ALTER TABLE orders ALTER COLUMN customer_id SET STATISTICS 1000;
ANALYZE orders(customer_id);
```

### SQLite：sqlite_stat1

```sql
-- SQLite 的 ANALYZE 生成 sqlite_stat1 表
ANALYZE;

-- sqlite_stat1 结构：(tbl, idx, stat)
SELECT * FROM sqlite_stat1;
-- tbl      | idx              | stat
-- orders   | idx_orders_cust  | 1000000 50
-- customers| sqlite_autoindex | 10000 1
```

**`stat` 字段格式**：`N D1 D2 D3 ...`
- `N` = 表总行数
- `D1` = 第一个索引列的不同值数量（近似）
- `D2` = 前两列组合的不同值数量
- 以此类推

```sql
-- stat = "1000000 50" 表示：
--   表有 1000000 行
--   customer_id 有 50 个不同值
--   估算 customer_id = 42 的行数 = 1000000 / 50 = 20000
```

> **对比 miniDB**：miniDB 的统计信息只记录表行数和索引的近似基数（阶段1章8），没有 MCV 和直方图。因此 miniDB 对倾斜数据的代价估算很不准。PG 的 `pg_statistic` 是工业级实现，MCV + 直方图 + 相关性（`pg_statistic.stakind`）三维统计。

### 统计信息过期导致的计划退化

```sql
-- 场景：表创建时只有 1000 行，统计信息记录"小表"
-- 之后插入了 10000000 行，但没 ANALYZE
-- 优化器仍以为是小表，选择 Nested Loop（O(R×S)）而非 Hash Join（O(R+S)）

-- 修复
ANALYZE large_table;  -- 重新收集统计
-- 优化器现在知道是大表，改用 Hash Join
```

---

## 慢查询识别

### PostgreSQL：pg_stat_statements

```sql
-- 启用 pg_stat_statements 扩展
CREATE EXTENSION pg_stat_statements;

-- 查看最慢的 10 个查询
SELECT query, calls, total_exec_time, mean_exec_time, rows
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 10;

-- 查看总耗时最多的查询
SELECT query, calls, total_exec_time
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 10;
```

### PostgreSQL：慢查询日志

```sql
-- 记录超过 100ms 的查询
ALTER SYSTEM SET log_min_duration_statement = 100;
SELECT pg_reload_conf();

-- 日志中会出现：
-- LOG: duration: 234.567 ms  statement: SELECT * FROM orders WHERE ...
```

### SQLite：计时

```sql
-- CLI 中开启计时
sqlite> .timer on
sqlite> SELECT * FROM orders WHERE customer_id = 42;
-- Run Time: real 0.002 user 0.001 sys 0.000
```

```python
# Python 中用 time 测量
import time, sqlite3

conn = sqlite3.connect('test.db')
t0 = time.perf_counter()
rows = conn.execute("SELECT * FROM orders WHERE customer_id = 42").fetchall()
t1 = time.perf_counter()
print(f"耗时: {(t1-t0)*1000:.2f} ms, 行数: {len(rows)}")
```

---

## Join 优化

### 三种 Join 算法详解

#### 1. Nested Loop Join（嵌套循环连接）

```
for r in R:                          # 外表
    for s in S:                      # 内表
        if r.key == s.key:
            output(r, s)
```

**特点**：
- 时间复杂度 O(|R| × |S|)
- 内存 O(1)（无索引）或 O(1)（有索引时内表用索引查找，复杂度 O(|R| × log|S|)）
- **适合**：外表很小、内表有索引、非等值连接（`<`, `>`, `!=`）

```sql
-- PG 选择 Nested Loop 的场景
EXPLAIN SELECT * FROM small_lookup s JOIN big_fact b ON s.id = b.lookup_id;
-- Nested Loop
--   -> Seq Scan on small_lookup s          (rows=10)
--   -> Index Scan using idx on big_fact b  (Index Cond: lookup_id = s.id)
-- 总代价 ≈ 10 × log(10000000) ≈ 230 次索引查找
```

#### 2. Hash Join（哈希连接）

```
# Phase 1: Build（构建哈希表）
hash_table = {}
for s in S:                          # 内表（通常较小）
    hash_table[s.key].append(s)

# Phase 2: Probe（探测）
for r in R:                          # 外表
    for s in hash_table.get(r.key, []):
        output(r, s)
```

**特点**：
- 时间复杂度 O(|R| + |S|)
- 内存 O(|S|)（需放得下内表，否则分批 + 溢出磁盘）
- **只支持等值连接**（`=`）
- **适合**：等值连接、内表可放入内存（`work_mem`）、大表连接大表

```sql
-- PG 选择 Hash Join 的场景
EXPLAIN SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id;
-- Hash Join
--   Hash Cond: (o.customer_id = c.id)
--   -> Seq Scan on orders o              (rows=1000000)
--   -> Hash
--        -> Seq Scan on customers c      (rows=10000, 放入 Hash 表)
-- 总代价 ≈ 1000000 + 1000000 = 2000000 次操作
```

#### 3. Merge Join（归并连接）

```
# 前提：R 和 S 已按 key 排序
i, j = 0, 0
while i < len(R) and j < len(S):
    if R[i].key < S[j].key:
        i += 1
    elif R[i].key > S[j].key:
        j += 1
    else:
        output(R[i], S[j])  # 处理重复键
        i += 1; j += 1
```

**特点**：
- 时间复杂度 O(|R| + |S|)（需两侧有序）
- 内存 O(1)（如果已有序）或 O(|R| + |S|)（需排序）
- **只支持等值连接**
- **适合**：两侧已按连接键排序（如都有索引）、数据有序

```sql
-- PG 选择 Merge Join 的场景
EXPLAIN SELECT * FROM a JOIN b ON a.id = b.id;
-- Merge Join
--   Merge Cond: (a.id = b.id)
--   -> Index Scan using a_pkey on a    (有序，无需额外排序)
--   -> Index Scan using b_pkey on b    (有序，无需额外排序)
-- 总代价 ≈ 100000 + 100000 = 200000 次操作（且无排序开销）
```

### 三种 Join 对比

| 维度 | Nested Loop | Hash Join | Merge Join |
|---|---|---|---|
| **时间复杂度** | O(R×S) 或 O(R×log S) | O(R+S) | O(R+S) |
| **内存** | O(1) | O(S) | O(1) 或 O(R+S) |
| **连接类型** | 等值 + 非等值 | 仅等值 | 仅等值 |
| **需要排序** | 否 | 否 | 是（如未有序） |
| **外表小** | ⭐⭐⭐ 最优 | ⭐⭐ | ⭐ |
| **大表 join 大表** | ⭐ 最差 | ⭐⭐⭐ 最优 | ⭐⭐⭐ 最优 |
| **内表有索引** | ⭐⭐⭐ 最优 | ⭐ | ⭐⭐ |
| **两侧已有序** | ⭐ | ⭐⭐ | ⭐⭐⭐ 最优 |
| **非等值** | ⭐⭐⭐ 唯一选择 | ❌ | ❌ |
| **可并行** | 否（PG） | 是 | 是 |

### Join 顺序优化

多表 Join 时，Join 顺序对性能影响巨大。N 个表的 Join 顺序有 `N!` 种排列：

```sql
-- 4 表 Join：4! = 24 种顺序
SELECT * FROM a JOIN b ON ... JOIN c ON ... JOIN d ON ...;
```

**PG 用动态规划**（默认）或**遗传算法**（`geqo`，表数 ≥ 12 时）搜索最优顺序：

```sql
-- 强制 Join 顺序（调试用，不推荐生产）
SET join_collapse_limit = 1;  -- 不重排 Join
SET enable_nestloop = off;    -- 禁用 Nested Loop
SET enable_hashjoin = off;    -- 禁用 Hash Join
SET enable_mergejoin = off;   -- 禁用 Merge Join

-- 查看当前 Join 顺序
EXPLAIN SELECT * FROM a JOIN b ON a.id = b.aid JOIN c ON b.id = c.bid;
```

**Join 顺序的经验法则**：

1. **小表驱动大表**：小表做外表（Nested Loop）
2. **过滤性强的表先 Join**：先做选择性高的过滤，减少中间结果
3. **有索引的表做内表**：Nested Loop 内表用索引加速
4. **等值连接优先 Hash/Merge**：大表之间用 Hash 或 Merge

---

## 子查询优化

### 子查询分类

| 类型 | 示例 | 特点 |
|---|---|---|
| **非相关子查询** | `WHERE x IN (SELECT y FROM t2)` | 子查询不引用外层表，可独立执行 |
| **相关子查询** | `WHERE x = (SELECT y FROM t2 WHERE t2.id = t1.id)` | 子查询引用外层表，每行执行一次 |
| **标量子查询** | `SELECT a, (SELECT max(b) FROM t2) FROM t1` | 返回单值 |
| **EXISTS 子查询** | `WHERE EXISTS (SELECT 1 FROM t2 WHERE t2.id = t1.id)` | 存在性检查 |

### 优化技术 1：子查询展开（Subquery Pull-up）

```sql
-- 原始：子查询
SELECT * FROM orders
WHERE customer_id IN (SELECT id FROM customers WHERE vip = true);

-- 优化器自动展开为 Join（更高效）
SELECT orders.* FROM orders
JOIN customers ON orders.customer_id = customers.id
WHERE customers.vip = true;

-- EXPLAIN 确认已展开
EXPLAIN SELECT * FROM orders
WHERE customer_id IN (SELECT id FROM customers WHERE vip = true);
-- Hash Join                    ← 已展开为 Join
--   Hash Cond: (orders.customer_id = customers.id)
--   -> Seq Scan on orders
--   -> Hash
--        -> Seq Scan on customers  (Filter: vip)
```

**SQLite 的处理**：

```sql
EXPLAIN QUERY PLAN SELECT * FROM orders
WHERE customer_id IN (SELECT id FROM customers WHERE vip = 1);
-- id  parent  notused  detail
-- 2   0       0        SEARCH orders USING INDEX idx_orders_customer (customer_id=?)
-- 6   2       0        SEARCH customers USING AUTOMATIC INDEX (id=?)
-- SQLite 也将其转为类似 Join 的执行方式
```

### 优化技术 2：相关子查询 → Join

```sql
-- 原始：相关子查询（每行执行一次子查询，N+1 问题）
SELECT c.name,
       (SELECT count(*) FROM orders o WHERE o.customer_id = c.id) AS order_count
FROM customers c;

-- 优化：改为 Join + Group
SELECT c.name, count(o.id) AS order_count
FROM customers c LEFT JOIN orders o ON o.customer_id = c.id
GROUP BY c.id, c.name;

-- 性能差异：
--   相关子查询：对每个 customer 执行一次 count → 10000 次查询
--   Join：一次扫描 orders + Hash 聚合 → 2 次扫描
```

### 优化技术 3：EXISTS vs IN vs JOIN

```sql
-- 三种写法等价（检查有订单的客户）
SELECT * FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id);
SELECT * FROM customers c WHERE c.id IN (SELECT customer_id FROM orders);
SELECT c.* FROM customers c JOIN (SELECT DISTINCT customer_id FROM orders) o ON c.id = o.customer_id;

-- 性能通常：JOIN ≈ EXISTS ≥ IN
-- 但现代优化器通常能将三者优化为相同计划
EXPLAIN SELECT * FROM customers c WHERE c.id IN (SELECT customer_id FROM orders);
-- Hash Join  (Hash Cond: ...)  ← 优化器已转为 Join
```

### 优化技术 4：避免标量子查询的 N+1

```sql
-- 反模式：N+1 查询（应用层）
for customer in customers:                # 1 次查询
    orders = query("SELECT * FROM orders WHERE customer_id = ?", customer.id)  # N 次查询
# 总共 N+1 次查询

-- 正确：一次查询
SELECT c.*, o.* FROM customers c LEFT JOIN orders o ON c.customer_id = c.id;
-- 1 次查询
```

---

## 常见性能反模式

### 反模式 1：SELECT \*

```sql
-- 反模式
SELECT * FROM orders WHERE customer_id = 42;
-- 返回 20 列，包括大文本列，浪费 IO 和网络带宽

-- 正确：只取需要的列（可能命中覆盖索引）
SELECT id, order_date, amount FROM orders WHERE customer_id = 42;
-- 如果有 (customer_id, id, order_date, amount) 覆盖索引 → Index Only Scan
```

### 反模式 2：无索引的全表扫描

```sql
-- 反模式
SELECT * FROM orders WHERE customer_id = 42;  -- 无索引 → Seq Scan

-- 正确
CREATE INDEX idx_orders_customer ON orders(customer_id);
-- 现在 → Index Scan
```

**判断是否需要索引的依据**：

| 列的选择度 | 建议索引 | 说明 |
|---|---|---|
| > 30% 不同值 | ⭐ 建索引 | 高选择度，索引效果好 |
| 10%-30% | 视查询频率 | 中等选择度 |
| < 10% 不同值 | 不建索引（或建位图） | 低选择度，索引不如全表扫描 |

### 反模式 3：N+1 查询

```python
# 反模式：N+1
customers = conn.execute("SELECT id FROM customers").fetchall()  # 1 次
for c in customers:
    orders = conn.execute("SELECT * FROM orders WHERE customer_id = ?", (c[0],)).fetchall()  # N 次

# 正确：JOIN 一次查完
rows = conn.execute("""
    SELECT c.id, o.* FROM customers c
    LEFT JOIN orders o ON o.customer_id = c.id
""").fetchall()
```

### 反模式 4：不当的 OR 条件

```sql
-- 反模式：OR 可能导致无法用索引
SELECT * FROM orders WHERE customer_id = 42 OR status = 'pending';
-- 可能退化为 Seq Scan（两个条件分别有索引，但 OR 难以同时利用）

-- 正确1：UNION ALL（各自用索引）
SELECT * FROM orders WHERE customer_id = 42
UNION
SELECT * FROM orders WHERE status = 'pending' AND customer_id != 42;

-- 正确2：Bitmap Or（PG 可能自动选择）
EXPLAIN SELECT * FROM orders WHERE customer_id = 42 OR status = 'pending';
-- Bitmap Heap Scan on orders
--   -> Bitmap Or
--        -> Bitmap Index Scan on idx_customer
--        -> Bitmap Index Scan on idx_status
```

### 反模式 5：函数包裹索引列导致索引失效

```sql
-- 反模式：函数包裹列 → 索引失效
SELECT * FROM orders WHERE DATE(order_time) = '2024-01-15';
-- Seq Scan（即使 order_time 有索引）

-- 正确1：改为范围查询
SELECT * FROM orders
WHERE order_time >= '2024-01-15' AND order_time < '2024-01-16';
-- Index Scan

-- 正确2：建表达式索引
CREATE INDEX idx_order_date ON orders (DATE(order_time));
SELECT * FROM orders WHERE DATE(order_time) = '2024-01-15';
-- Index Scan using idx_order_date
```

### 反模式 6：隐式类型转换导致索引失效

```sql
-- 反模式：列是 VARCHAR，查询用 INTEGER → 隐式转换 → 索引失效
SELECT * FROM orders WHERE order_no = 12345;        -- order_no 是 TEXT
-- Seq Scan

-- 正确：类型匹配
SELECT * FROM orders WHERE order_no = '12345';
-- Index Scan
```

### 反模式 7：统计信息过期

```sql
-- 反模式：大量写入后未 ANALYZE
INSERT INTO orders SELECT ...;  -- 插入 100 万行
-- 不 ANALYZE，优化器仍以为表只有 1000 行 → 选错计划

-- 正确
ANALYZE orders;
```

### 反模式 8：Join 顺序不当

```sql
-- 反模式：大表先 Join
SELECT * FROM huge_fact h JOIN small_dim d ON h.dim_id = d.id WHERE d.active = 1;
-- 如果优化器先 Join h × d（无过滤），中间结果巨大

-- 正确：先过滤小表
SELECT * FROM small_dim d JOIN huge_fact h ON h.dim_id = d.id WHERE d.active = 1;
-- 优化器先过滤 d（100 行），再 Nested Loop 到 h 的索引
-- PG 通常能自动重排，但某些情况需要手动干预
```

### 反模式 9：深分页

```sql
-- 反模式：OFFSET 很大时，要扫描并丢弃前 N 行
SELECT * FROM orders ORDER BY id LIMIT 20 OFFSET 1000000;
-- 需要扫描 1000020 行，丢弃前 1000000 行

-- 正确1：游标分页（keyset pagination）
SELECT * FROM orders WHERE id > ? ORDER BY id LIMIT 20;
-- 只需从 id 开始扫描 20 行

-- 正确2：记住上一页最后一个 id
last_id = 0
page = conn.execute("SELECT * FROM orders WHERE id > ? ORDER BY id LIMIT 20", (last_id,)).fetchall()
last_id = page[-1].id
```

---

## 与 miniDB 优化器对照

| 维度 | miniDB（阶段1章8） | SQLite | PostgreSQL |
|---|---|---|---|
| **优化器类型** | 启发式 + 简单代价 | 基于代价（CBO） | 基于代价 + 遗传算法 |
| **统计信息** | 表行数 + 索引基数 | `sqlite_stat1`（行数 + 不同值数） | `pg_statistic`（MCV + 直方图 + 相关性） |
| **Join 算法** | Nested Loop only | Nested Loop + Hash（内部） | Nested Loop + Hash + Merge |
| **Join 顺序** | 按书写顺序 | 基于代价重排 | DP（<12 表）/ GEQO（≥12 表） |
| **子查询优化** | 无 | 部分展开 | 完整展开 + 消除 + 解相关 |
| **EXPLAIN** | 简单文本 | VDBE 字节码 / QUERY PLAN | 计划树 + 代价 + ANALYZE |
| **索引选择** | 有索引就用 | 基于代价选择 | 基于代价选择 + Bitmap |
| **并行** | 无 | 无（单线程） | 并行 Seq Scan / Join / Aggregate |

> **核心差异**：miniDB 的优化器是教学版，只实现了启发式规则（"有索引就用索引"、"小表驱动大表"）和简单代价模型。真实数据库的优化器是工业级，统计信息丰富、Join 算法多样、子查询优化完整。理解 miniDB 的优化器是理解真实优化器的基础——核心思想一致：**用统计信息估算代价，选代价最小的计划**。

---

## Python 实验代码

### 文件清单

| 文件 | 内容 |
|---|---|
| `explain_demo.py` | EXPLAIN 使用演示：建测试数据、对比有/无索引的执行计划、解读 SCAN vs SEARCH |
| `slow_query_demo.py` | 5 个慢查询案例：全表扫描→索引、N+1→JOIN、OR→UNION、统计过期→ANALYZE、Join 顺序 |
| `join_strategies.py` | Join 策略对比：不同数据量下 SQLite 的 Join 计划选择与耗时 |

### 运行方式

```bash
cd phase2/06-query-tuning
uv run python explain_demo.py
uv run python slow_query_demo.py
uv run python join_strategies.py
```

---

## 习题

1. **EXPLAIN 解读**：对以下查询，画出执行计划树并解释每个节点的作用：
   ```sql
   SELECT c.name, count(*) AS cnt
   FROM customers c JOIN orders o ON c.id = o.customer_id
   WHERE o.status = 'shipped' AND c.region = 'CN'
   GROUP BY c.name
   HAVING count(*) > 10
   ORDER BY cnt DESC LIMIT 20;
   ```

2. **统计信息实验**：创建一张表，插入 10 万行（某列 90% 的值相同，10% 各不相同），运行 `ANALYZE`，查看 `pg_stats` 的 `most_common_vals` 和 `histogram_bounds`，解释优化器如何估算 `WHERE col = 'common_value'` 的行数。

3. **Join 策略验证**：构造三组数据（外表小内表大、两表都大且无序、两表都大且有序），分别用 `EXPLAIN` 观察 PG 选择的 Join 算法，验证是否符合"Hash Join 适合大表、Merge Join 适合有序、Nested Loop 适合小外表"的规则。

4. **子查询优化**：写一个相关子查询，用 `EXPLAIN ANALYZE` 测量其耗时；改写为 Join + Group，再次测量；对比两者性能并解释差异。

5. **反模式复现**：用 Python + SQLite 复现"函数包裹索引列导致索引失效"反模式：建索引、写两种查询（函数包裹 vs 范围查询），用 `EXPLAIN QUERY PLAN` 和计时验证性能差异。

6. **深分页优化**：构造 100 万行数据，对比 `OFFSET 999980 LIMIT 20` 和 `WHERE id > ? LIMIT 20` 的性能差异，用计时量化差距。

7. **miniDB 对照**：miniDB 的优化器只支持 Nested Loop Join，分析在什么场景下这会导致严重性能问题，以及 miniDB 可以如何改进（加入 Hash Join 的最小实现需要哪些组件）。

8. **ANALYZE 影响**：创建表并插入数据，先不 ANALYZE 执行一个查询并记录计划；插入大量新数据后不 ANALYZE 再次执行，对比计划是否变化；ANALYZE 后再执行，观察计划是否修正。