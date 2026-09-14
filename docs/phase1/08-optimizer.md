# 章8：查询优化器

> 同一条 SQL 可以有多种执行方式。`SELECT * FROM users WHERE id = 42` 既可以全表扫描，也可以走索引。优化器的职责就是：在所有可能的执行方案里，挑出代价最小的那一个。
>
> 本章是 miniDB phase1 优化器模块的完整讲解，面向第一次接触查询优化的读者。我们会从"为什么需要优化器"讲起，逐步建立关系代数、逻辑/物理计划、代价模型、索引选择、Join 顺序、统计信息等概念，最后逐行解读 `phase1/src/optimizer/` 下的 C 代码，并附上习题。

## 目录

1. [为什么需要优化器](#1-为什么需要优化器)
2. [关系代数基础](#2-关系代数基础)
3. [逻辑计划 vs 物理计划](#3-逻辑计划-vs-物理计划)
4. [启发式优化](#4-启发式优化)
5. [代价模型详解](#5-代价模型详解)
6. [索引选择](#6-索引选择)
7. [Join 顺序](#7-join-顺序)
8. [统计信息](#8-统计信息)
9. [代码逐行解读](#9-代码逐行解读)
10. [与真实数据库对比](#10-与真实数据库对比)
11. [习题](#11-习题)
12. [文件清单与测试覆盖](#12-文件清单与测试覆盖)
13. [下一步](#13-下一步)

---

## 1. 为什么需要优化器

### 1.1 同一条 SQL，多种执行方式

假设我们有一张 `users` 表，10000 行，存储在 100 个磁盘页上，`id` 列上有 B+ 树索引。现在执行：

```sql
SELECT * FROM users WHERE id = 42;
```

这条 SQL 至少有两种执行方式：

| 方案 | 做法 | 大致代价 |
|---|---|---|
| **方案 A：全表扫描** | 从第 1 页读到第 100 页，逐行比较 `id` 是否等于 42 | 100 次 page read |
| **方案 B：索引扫描** | 在 B+ 树上查找 `id=42`，拿到指针后直接读对应行 | log₂100 + 1 ≈ 7.6 次 page read |

方案 B 比方案 A 快约 13 倍。如果表有 1000 万行，差距会变成约 1000 倍。**优化器就是自动帮你选方案 B 的那个组件**。

### 1.2 代价差异示例：一个稍复杂的查询

```sql
SELECT name FROM users WHERE age >= 18 AND age <= 25 AND city = 'Beijing';
```

可能的执行方式（部分）：

| 方案 | 计划 | 说明 |
|---|---|---|
| 1 | SeqScan → Filter(age∈[18,25]) → Filter(city='Beijing') → Project(name) | 全表扫，两个过滤逐行判断 |
| 2 | IndexScan(age) → Filter(city='Beijing') → Project(name) | 若 age 有索引，先按范围取行 |
| 3 | IndexScan(city) → Filter(age∈[18,25]) → Project(name) | 若 city 有索引，先按等值取行 |
| 4 | IndexScan(city) ∩ IndexScan(age) → Project(name) | 索引交集（miniDB 未实现） |

每种方案的代价都不同，优化器需要估算每种方案的代价并选最优的。

### 1.3 优化器在数据库中的位置

```
SQL 文本
   │
   ▼
┌──────────┐
│  Parser  │  词法 + 语法分析
└──────────┘
   │
   ▼  AST（抽象语法树）
┌──────────────┐
│  Optimizer   │  ← 本章主角
│  (优化器)    │
└──────────────┘
   │
   ▼  物理执行计划
┌──────────────┐
│  Executor    │  执行引擎
└──────────────┘
   │
   ▼
查询结果
```

优化器接收 AST，输出物理执行计划。执行引擎按计划树自顶向下（或自底向上）调用每个算子的 `open/next/close` 接口，最终产出结果行。

### 1.4 优化器的两大流派

| 流派 | 代表 | 思路 |
|---|---|---|
| **启发式（规则）优化** | 早期 System R、miniDB | 用一组"总是有益"的规则改写计划，如谓词下推、投影裁剪。不依赖代价。 |
| **代价优化** | PostgreSQL、Oracle | 枚举多种等价计划，用代价模型估算每种代价，选最小的。 |
| **混合** | 现代数据库 | 先用启发式规则做"显然有益"的改写，再用代价模型在剩余空间里搜索。 |

miniDB phase1 采用**启发式 + 简单代价估算**的混合方式：先用规则把 SeqScan 替换成 IndexScan，再用代价模型给每个节点标注 `rows` 和 `cost`。

### 1.5 优化器要回答的三个问题

1. **做什么**（逻辑计划）：把 SQL 翻译成关系代数表达式，比如 `π_name(σ_age<30(users))`。
2. **怎么做**（物理计划）：为每个关系代数算子选具体算法，比如 SeqScan 还是 IndexScan，NestedLoopJoin 还是 HashJoin。
3. **做多快**（代价估算）：估算每种方案的行数和代价，挑出最优的。

本章后续小节依次回答这三个问题。

---

## 2. 关系代数基础

关系代数是优化器的"母语"。SQL 在优化器内部会被翻译成关系代数表达式，然后在关系代数上做等价改写。

### 2.1 三大基本算子

| 符号 | 名称 | 含义 | SQL 对应 |
|---|---|---|---|
| **σ** (sigma) | 选择 (Selection) | 选出满足条件的行 | `WHERE` |
| **π** (pi) | 投影 (Projection) | 选出需要的列 | `SELECT col1, col2` |
| **⋈** (bowtie) | 连接 (Join) | 按条件拼接两张表 | `FROM a JOIN b ON ...` |

### 2.2 选择 σ

σ_条件(R) 表示从关系 R 中选出满足"条件"的行。

```
R:
| id | name | age |
|  1 | Alice| 20  |
|  2 | Bob  | 30  |
|  3 | Carol| 25  |

σ_age>22(R):
| id | name | age |
|  2 | Bob  | 30  |
|  3 | Carol| 25  |
```

对应 SQL：`SELECT * FROM R WHERE age > 22;`

**选择率（selectivity）**：σ 之后行数与原行数的比值。上例选择率 = 2/3 ≈ 0.67。选择率是代价估算的核心输入，第 5 节详述。

### 2.3 投影 π

π_列(R) 表示从关系 R 中只保留指定列。

```
π_name,age(R):
| name | age |
| Alice| 20  |
| Bob  | 30  |
| Carol| 25  |
```

对应 SQL：`SELECT name, age FROM R;`

注意：关系代数里的投影**默认去重**（因为关系是集合），但 SQL 的 `SELECT` 默认**不去重**，要加 `DISTINCT` 才去重。miniDB 沿有实现去重。

### 2.4 连接 ⋈

R ⋈_条件 S 表示把 R 和 S 中满足"条件"的行拼起来。

```
R:                S:
| id | name |     | id | city |
|  1 | Alice|     |  1 | NYC  |
|  2 | Bob  |     |  2 | LA   |

R ⋈_R.id=S.id S:
| id | name | city |
|  1 | Alice| NYC  |
|  2 | Bob  | LA   |
```

对应 SQL：`SELECT * FROM R JOIN S ON R.id = S.id;`

### 2.5 组合：一个 SQL 的关系代数表示

```sql
SELECT name FROM users WHERE age < 30 AND city = 'Beijing';
```

翻译成关系代数（从内到外读）：

```
π_name( σ_city='Beijing'( σ_age<30( users ) ) )
```

读法：先对 `users` 做 `σ_age<30`（过滤年龄），再对结果做 `σ_city='Beijing'`（过滤城市），最后 `π_name`（只取 name 列）。

### 2.6 关键等价律：优化的理论基础

优化器之所以能改写计划，是因为关系代数有一组**等价律**——左右两边结果完全相同，但执行代价可能差很多。

| 定律 | 形式 | 直觉 |
|---|---|---|
| **交换律（选择）** | σ_p1(σ_p2(R)) = σ_p2(σ_p1(R)) | 两个过滤谁先谁后结果一样 |
| **合并律（选择）** | σ_p1(σ_p2(R)) = σ_p1∧p2(R) | 两个过滤可合并成一个 |
| **分配律（选择对连接）** | σ_p(R⋈S) = σ_p(R)⋈S （p 只涉及 R 的列） | 过滤可以推到连接之前 ← **谓词下推** |
| **投影级联** | π_L(π_M(R)) = π_L(R) （L⊆M） | 外层投影决定最终列 |
| **连接交换** | R⋈S = S⋈R | Join 顺序可换 ← **Join 顺序优化** |

其中"谓词下推"和"Join 顺序"是优化器最强大的两个武器，第 4、7 节详述。

### 2.7 小结

| 概念 | 一句话 |
|---|---|
| 关系代数 | SQL 的数学表示，优化器在它上面做等价改写 |
| σ 选择 | 对应 WHERE，按条件过滤行 |
| π 投影 | 对应 SELECT 列表，按列裁剪 |
| ⋈ 连接 | 对应 JOIN，按条件拼表 |
| 等价律 | 改写计划的理论依据，结果不变代价可能变 |

---

## 3. 逻辑计划 vs 物理计划

### 3.1 两层抽象

优化器把"做什么"和"怎么做"分开：

| 层 | 名称 | 回答的问题 | 示例 |
|---|---|---|---|
| **逻辑计划** | Logical Plan | 做什么（关系代数） | Scan(users) → Filter(id=42) → Project(id) |
| **物理计划** | Physical Plan | 怎么做（具体算法） | IndexScan(users) [idx:id] → Filter → Project |

同一个逻辑计划可以对应多个物理计划。比如逻辑上的 `Scan(users)` 物理上可以是 `SeqScan` 或 `IndexScan`；逻辑上的 `Join` 物理上可以是 `NestedLoopJoin`、`HashJoin`、`MergeJoin`。

### 3.2 miniDB 的简化处理

工业级数据库（PostgreSQL、Oracle）会显式区分逻辑计划和物理计划，先做逻辑优化再做物理优化。miniDB phase1 为了简化，**把两层合并在同一棵计划树里**，节点类型直接用物理算子名（`PLAN_SEQ_SCAN`、`PLAN_INDEX_SCAN` 等），优化过程就是"在树上做改写"。

```c
// logical_plan.h
typedef enum {
    PLAN_SEQ_SCAN = 0,          // 顺序扫描（物理）
    PLAN_INDEX_SCAN = 1,        // 索引扫描（物理）
    PLAN_FILTER = 2,            // 过滤（逻辑+物理合一）
    PLAN_PROJECT = 3,           // 投影（逻辑+物理合一）
    PLAN_NESTED_LOOP_JOIN = 4,  // 嵌套循环连接（物理）
    PLAN_HASH_JOIN = 5,         // 哈希连接（物理）
} plan_type_t;
```

### 3.3 计划是一棵树

每个计划节点有 `left` 和 `right` 两个子节点，形成一棵二叉树。叶子节点是 Scan（数据访问），中间节点是 Filter/Project/Join（数据处理）。

```
Project(id)                    ← 顶层：选择列
  └─ Filter(id =)              ← 中间：WHERE 条件
       └─ IndexScan(users)     ← 底层：数据访问
```

读法：从叶子往上读。先 IndexScan 取出 users 中 id=42 的行，再 Filter（其实 IndexScan 已经过滤了，这里 Filter 是冗余的，但 miniDB 保留它），最后 Project 只保留 id 列。

### 3.4 plan_node_t 结构详解

```c
struct plan_node {
    plan_type_t type;               // 节点类型

    plan_node_t *left;              // 左子树（大多数算子的输入）
    plan_node_t *right;             // 右子树（Join 的右输入，其他为 NULL）

    char table_name[PLAN_MAX_NAME]; // Scan 节点：表名
    char index_col[PLAN_MAX_NAME];  // IndexScan 节点：索引列名

    ast_expr_t predicate;           // Filter 节点：谓词表达式
    bool has_predicate;             // 是否有谓词

    char columns[PLAN_MAX_COLS][PLAN_MAX_NAME]; // Project 节点：输出列名
    int num_cols;                               // 列数
    bool select_all;                            // 是否 SELECT *

    char join_left_col[PLAN_MAX_NAME];  // Join 节点：左表连接列
    char join_right_col[PLAN_MAX_NAME]; // Join 节点：右表连接列

    double estimated_rows;   // 估算输出行数（代价模型填写）
    double estimated_cost;   // 估算执行代价（代价模型填写）
};
```

| 字段 | 哪类节点用 | 含义 |
|---|---|---|
| `table_name` | SeqScan / IndexScan | 要扫描的表名 |
| `index_col` | IndexScan | 走哪个索引列 |
| `predicate` / `has_predicate` | Filter | WHERE 条件 |
| `columns` / `num_cols` / `select_all` | Project | SELECT 列表 |
| `join_left_col` / `join_right_col` | Join | 连接条件列 |
| `estimated_rows` / `estimated_cost` | 所有节点 | 代价模型估算结果 |

### 3.5 三层抽象回顾

| 层 | 作用 | 示例 |
|---|---|---|
| **逻辑计划** | 做什么（关系代数） | Scan(users) → Filter(id=42) |
| **物理计划** | 怎么做（具体算法） | SeqScan / IndexScan |
| **代价估算** | 做多快 | rows=1, cost=7.6 |

### 3.6 优化流程总览

```
AST ──plan_from_ast──▶ 逻辑计划 ──启发式规则──▶ 优化计划 ──代价估算──▶ 物理计划
                          │                      │                      │
                     Scan/Filter/Project     IndexScan 替换        rows/cost 标注
```

对应 `optimizer.c` 里的 `optimizer_optimize` 函数：

```c
plan_node_t *optimizer_optimize(ast_stmt_t *stmt, catalog_t *cat) {
    plan_node_t *plan = plan_from_ast(stmt);        // 1. AST → 逻辑计划
    if (!plan) return NULL;
    opt_pushdown_predicates(plan);                  // 2. 谓词下推（预留）
    opt_choose_index(plan, cat);                    // 3. 索引选择
    estimate_costs(plan, cat);                      // 4. 代价估算
    return plan;
}
```

四步：① 翻译 ② 改写 ③ 物理算子选择 ④ 代价标注。

---

## 4. 启发式优化

启发式优化用一组"几乎总是有益"的规则改写计划，不需要估算代价。本节介绍两条最重要的规则：**谓词下推**和**投影裁剪**。

### 4.1 谓词下推（Predicate Pushdown）

**规则**：把 Filter 尽量推到靠近 Scan 的位置，越早过滤行数越少，后续算子处理的数据量越小。

**为什么有效**：考虑

```sql
SELECT * FROM users JOIN orders ON users.id = orders.user_id WHERE users.age > 18;
```

不下推的计划：

```
Filter(age > 18)
  └─ Join(users.id = orders.user_id)
       ├─ Scan(users)      ← 先把 10000 行 users 全读出来
       └─ Scan(orders)     ← 再把 50000 行 orders 全读出来
```

Join 要处理 10000 × 50000 = 5 亿次比较。

下推后的计划：

```
Join(users.id = orders.user_id)
  ├─ Filter(age > 18)      ← 先把 users 过滤到比如 3000 行
  │    └─ Scan(users)
  └─ Scan(orders)
```

Join 只处理 3000 × 50000 = 1.5 亿次比较，快 3 倍多。

**理论基础**：选择对连接的分配律 —— `σ_p(R⋈S) = σ_p(R)⋈S`（当 p 只涉及 R 的列时）。

### 4.2 谓词下推的 miniDB 状态

```c
void opt_pushdown_predicates(plan_node_t *plan) {
    (void)plan;   // 预留，当前为空实现
}
```

miniDB phase1 **没有实现**谓词下推，因为 phase1 还不支持 Join（Join 节点定义了但 `plan_from_ast` 不会生成）。当后续 phase 加入 Join 时，这里会填上真正的下推逻辑。

### 4.3 投影裁剪（Projection Pruning）

**规则**：只保留上层真正需要的列，去掉无用列。减少每行的宽度和 I/O 量。

**示例**：

```sql
SELECT name FROM users WHERE age > 18;
```

不裁剪：Scan 读出 users 的所有列（id, name, age, city, email, ...），Filter 用 age，Project 取 name。中间过程搬运了大量无用列。

裁剪后：Scan 只读 name 和 age 两列（Filter 需要 age，Project 需要 name），其他列从一开始就不读。

**列裁剪对列存数据库**（如 ClickHouse、Doris）效果尤其明显，因为只读需要的列文件即可。行存数据库（如 PostgreSQL）受益较小，因为整行一起读，但能减少网络传输和后续处理开销。

### 4.4 投影裁剪的 miniDB 状态

miniDB phase1 **没有实现**列裁剪，因为底层存储是整行读取，没有列裁剪的收益。预留为后续列存扩展。

### 4.5 已实现的启发式规则：索引选择

miniDB phase1 唯一真正实现的启发式规则是**索引选择**：如果 Filter 的条件列上有索引，把底层的 SeqScan 替换成 IndexScan。详见第 6 节。

### 4.6 启发式规则的优缺点

| 优点 | 缺点 |
|---|---|
| 实现简单，不需要代价模型 | 不一定总是最优（有时全表扫比索引扫快） |
| 总是有益或几乎总是有益 | 规则之间可能冲突，需要定顺序 |
| 速度快，优化本身开销小 | 无法处理"取决于数据分布"的决策 |

正因为启发式有局限，才需要代价模型（第 5 节）来在剩余空间里做精细选择。

### 4.7 优化规则全景

| 规则 | 作用 | 状态 |
|---|---|---|
| **索引选择** | Filter 列有索引 → IndexScan | ✅ 已实现 |
| **谓词下推** | Filter 推到 Scan 附近 | 预留（空实现） |
| **投影裁剪** | 去掉不需要的列 | 预留 |
| **Join 顺序** | DP 选最优顺序 | 预留 |
| **常量折叠** | `1+1` → `2` | 未实现 |
| **子查询展开** | 子查询变 Join | 未实现 |

---

## 5. 代价模型详解

代价模型是优化器的"秤"——给每个计划打一个分数（代价），分数越低越好。本节详解 miniDB 的代价模型：选择率、基数估算、代价公式。

### 5.1 代价是什么

miniDB 的代价单位是**磁盘页读取次数**（page read）。原因：

- 磁盘 I/O 是数据库最慢的操作，比 CPU 慢 10⁵ 倍。
- 优化器主要优化 I/O，CPU 代价次要。
- 页数容易估算（catalog 里存了 `num_pages`）。

工业级代价模型还会考虑 CPU 时间、网络传输、内存占用等，但核心都是 I/O。

### 5.2 选择率（Selectivity）

选择率 = 过滤后行数 / 过滤前行数。它是估算 Filter 输出行数的关键。

miniDB 的选择率表（`optimizer.c:67`）：

```c
static double selectivity(const ast_expr_t *expr) {
    if (strcmp(expr->op, "=") == 0)  return 0.01;   // 等值：1% 匹配
    if (strcmp(expr->op, "!=") == 0) return 0.99;   // 不等：99% 匹配
    if (strcmp(expr->op, "<") == 0 || strcmp(expr->op, ">") == 0) return 0.33;  // 范围：33%
    if (strcmp(expr->op, "<=") == 0 || strcmp(expr->op, ">=") == 0) return 0.33;
    return 0.5;   // 默认：50%
}
```

| 操作符 | 选择率 | 直觉 |
|---|---|---|
| `=` | 0.01 | 等值查询通常只命中很少行（假设 1%） |
| `!=` | 0.99 | 不等几乎命中所有行 |
| `<` / `>` | 0.33 | 范围查询命中约 1/3 |
| `<=` / `>=` | 0.33 | 同上 |
| 其他 | 0.5 | 保守估计一半 |

**为什么等值用 1%**：假设列有 100 个不同值，均匀分布，每个值占 1%。真实数据库会用 `1 / NDV`（NDV = 不同值数量）来估算，更精确。miniDB 简化为固定 0.01。

**为什么范围用 33%**：假设数据均匀分布，`x < v` 平均命中 50%，但实际查询往往偏向两端（比如 `age > 60` 命中很少），33% 是一个折中。

### 5.3 基数估算（Cardinality Estimation）

基数 = 某个算子输出的行数。自底向上递归估算。

| 算子 | 行数估算公式 | 直觉 |
|---|---|---|
| **SeqScan** | `num_rows` | 全表扫，输出所有行 |
| **IndexScan** | `num_rows × 0.01` | 索引扫，假设命中 1% |
| **Filter** | `input_rows × selectivity` | 按选择率缩减 |
| **Project** | `input_rows` | 投影不改行数（不去重） |
| **NestedLoopJoin** | `left × right × 0.1` | 假设 10% 的行能匹配 |
| **HashJoin** | `left × right × 0.1` | 同上（行数估算与算法无关） |

**示例**：`users` 表 10000 行，执行 `SELECT * FROM users WHERE age > 30;`

```
SeqScan(users):  rows = 10000
Filter(age > 30): rows = 10000 × 0.33 = 3300
```

### 5.4 代价公式

| 算子 | 代价公式 | 直觉 |
|---|---|---|
| **SeqScan** | `num_pages` | 读所有页 |
| **IndexScan** | `log₂(num_pages) + 1` | B+ 树查找深度 + 1 次数据页读取 |
| **Filter** | `input_cost + input_rows × 0.1` | 子树代价 + 每行判断谓词的代价 |
| **Project** | `input_cost + input_rows × 0.05` | 子树代价 + 每行裁列的代价（比 Filter 轻） |
| **NestedLoopJoin** | `left_cost + left_rows × right_cost` | 对左表每行扫一遍右表 |
| **HashJoin** | `left_cost + right_cost + left_rows + right_rows` | 各扫一次 + 建哈希表 |

**为什么 Filter 每行代价是 0.1**：判断一个谓词（比如 `age > 30`）比读一页磁盘便宜得多，用 0.1 表示"很轻的 CPU 开销"。

**为什么 NestedLoopJoin 是 `left_rows × right_cost`**：嵌套循环对左表每一行都要把右表完整扫一遍，所以右表代价被放大了 `left_rows` 倍。这正是 NestedLoopJoin 在大表上慢的根本原因。

**为什么 HashJoin 是 `left + right`**：哈希连接只需各扫一次表（建哈希表 + 探测），代价与行数线性相关，远优于 NestedLoopJoin。

### 5.5 代价计算示例 1：单表过滤

**场景**：`users` 表 10000 行、100 页、`id` 有索引。

```sql
SELECT * FROM users WHERE id = 42;
```

**方案 A：SeqScan → Filter**

```
SeqScan(users):  rows = 10000, cost = 100
Filter(id =):    rows = 10000 × 0.01 = 100
                  cost = 100 + 10000 × 0.1 = 1100
```

**方案 B：IndexScan → Filter**

```
IndexScan(users): rows = 10000 × 0.01 = 100
                   cost = log₂(100) + 1 ≈ 7.6
Filter(id =):     rows = 100 × 0.01 = 1
                   cost = 7.6 + 100 × 0.1 = 17.6
```

**对比表**：

| 方案 | 行数 | 代价 | 胜出 |
|---|---|---|---|
| SeqScan → Filter | 1 | 1100 | |
| IndexScan → Filter | 1 | 17.6 | ✅ |

IndexScan 快约 62 倍。优化器选 IndexScan。

### 5.6 代价计算示例 2：范围查询

**场景**：同上 `users` 表。

```sql
SELECT * FROM users WHERE age > 30;
```

`age` 没有索引，只能 SeqScan。

```
SeqScan(users):  rows = 10000, cost = 100
Filter(age >):   rows = 10000 × 0.33 = 3300
                  cost = 100 + 10000 × 0.1 = 1100
```

如果给 `age` 加索引呢？

```
IndexScan(users): rows = 10000 × 0.01 = 100   ← 注意：IndexScan 行数固定用 0.01
                   cost = 7.6
Filter(age >):    rows = 100 × 0.33 = 33
                   cost = 7.6 + 100 × 0.1 = 17.6
```

⚠️ 这里有个**简化缺陷**：miniDB 的 IndexScan 行数固定用 `num_rows × 0.01`，不管谓词是等值还是范围。真实数据库会区分：等值索引扫命中 1 行，范围索引扫命中取决于范围大小。miniDB 的简化会导致范围查询的行数估算偏小。

### 5.7 代价计算示例 3：两表 Join

**场景**：`users` 10000 行 100 页，`orders` 50000 行 500 页，都无索引。

```sql
SELECT * FROM users JOIN orders ON users.id = orders.user_id;
```

**方案 A：NestedLoopJoin（users 在外）**

```
SeqScan(users):  rows = 10000, cost = 100
SeqScan(orders): rows = 50000, cost = 500
NestedLoopJoin:  rows = 10000 × 50000 × 0.1 = 50,000,000
                  cost = 100 + 10000 × 500 = 5,000,100
```

**方案 B：NestedLoopJoin（orders 在外）**

```
SeqScan(orders): rows = 50000, cost = 500
SeqScan(users):  rows = 10000, cost = 100
NestedLoopJoin:  rows = 50,000,000
                  cost = 500 + 50000 × 100 = 5,000,500
```

**方案 C：HashJoin**

```
HashJoin: rows = 50,000,000
          cost = 100 + 500 + 10000 + 50000 = 60,600
```

**对比表**：

| 方案 | 代价 | 胜出 |
|---|---|---|
| NestedLoop (users 外) | 5,000,100 | |
| NestedLoop (orders 外) | 5,000,500 | |
| HashJoin | 60,600 | ✅ |

HashJoin 完胜！代价差 82 倍。这就是为什么大表 Join 一定要用 HashJoin。

### 5.8 代价计算示例 4：Join + 过滤

**场景**：同上，加过滤。

```sql
SELECT * FROM users JOIN orders ON users.id = orders.user_id WHERE users.age > 30;
```

**不下推**：

```
Filter(age > 30)
  └─ HashJoin
       ├─ SeqScan(users):  rows=10000, cost=100
       └─ SeqScan(orders): rows=50000, cost=500
HashJoin: rows = 10000 × 50000 × 0.1 = 50,000,000
          cost = 100 + 500 + 10000 + 50000 = 60,600
Filter:   rows = 50,000,000 × 0.33 = 16,500,000
          cost = 60,600 + 50,000,000 × 0.1 = 5,060,600
```

**下推**（Filter 推到 users 上）：

```
HashJoin
  ├─ Filter(age > 30)
  │    └─ SeqScan(users): rows=10000, cost=100
  │   Filter: rows = 10000 × 0.33 = 3300
  │            cost = 100 + 10000 × 0.1 = 1100
  └─ SeqScan(orders): rows=50000, cost=500
HashJoin: rows = 3300 × 50000 × 0.1 = 16,500,000
          cost = 1100 + 500 + 3300 + 50000 = 54,900
```

**对比**：

| 方案 | 代价 |
|---|---|
| 不下推 | 5,060,600 |
| 下推 | 54,900 |

下推快 92 倍！这就是谓词下推的威力。

### 5.9 代价模型的局限

miniDB 的代价模型很粗糙，主要简化：

| 简化 | 真实数据库的做法 |
|---|---|
| 选择率固定（0.01/0.33/0.5） | 用统计信息（直方图、MCV）算精确选择率 |
| IndexScan 行数固定 1% | 按谓词类型和范围估算 |
| Join 选择率固定 0.1 | 按连接列的 NDV 估算 |
| 不考虑内存 | 区分内存能放下 / 放不下，影响 HashJoin 代价 |
| 不考虑缓存 | 命中缓存的页代价远低于磁盘读 |

第 8 节介绍真实数据库如何用统计信息改进这些估算。

---

## 6. 索引选择

### 6.1 什么时候用索引

索引不是万能的。以下情况索引有利：

| 情况 | 为什么有利 |
|---|---|
| 等值查询 `id = 42` | 索引直接定位，命中极少行 |
| 高选择性范围查询 `id BETWEEN 1 AND 10` | 命中行少，索引扫比全表扫快 |
| 排序 `ORDER BY id`（id 有索引） | 索引本身有序，免排序 |

以下情况索引不利（全表扫更快）：

| 情况 | 为什么不利 |
|---|---|
| 查询命中大部分行（如 `age > 18`） | 索引扫要随机读，全表扫是顺序读，顺序读快得多 |
| 小表 | 表就几页，全表扫代价本来就低，索引查找的开销不划算 |
| 谓词列无索引 | 没得选 |

### 6.2 miniDB 的索引选择规则

miniDB 用一个简单的启发式规则：**如果 Filter 的条件列正好是表的索引列，就把 SeqScan 替换成 IndexScan**。不考虑命中行数、表大小等因素。

代码在 `optimizer.c:42`：

```c
static void try_index_scan(plan_node_t *plan, catalog_t *cat) {
    if (plan->type != PLAN_FILTER || !plan->has_predicate) return;

    // 穿透多层 Filter 找到底层 SeqScan
    plan_node_t *node = plan->left;
    while (node && node->type == PLAN_FILTER)
        node = node->left;
    if (!node || node->type != PLAN_SEQ_SCAN) return;

    // 查 catalog 看表有没有索引
    const catalog_entry_t *e = catalog_lookup(cat, node->table_name);
    if (!e || !e->has_index) return;

    // 谓词列 == 索引列 → 替换
    if (strcmp(plan->predicate.column, e->index_col) == 0) {
        node->type = PLAN_INDEX_SCAN;
        strncpy(node->index_col, e->index_col, PLAN_MAX_NAME - 1);
    }
}
```

逻辑分四步：
1. 只在 Filter 节点上触发。
2. 穿透连续的 Filter 找到底层 Scan。
3. 查 catalog 确认表有索引。
4. 谓词列名等于索引列名 → 把 SeqScan 改写成 IndexScan。

### 6.3 穿透多层 Filter 的必要性

考虑 `SELECT * FROM users WHERE id >= 10 AND age < 30;`，逻辑计划：

```
Filter(id >= 10)
  └─ Filter(age < 30)
       └─ SeqScan(users)
```

`try_index_scan` 在外层 `Filter(id >= 10)` 上调用时，`plan->left` 是 `Filter(age < 30)`，不是 SeqScan。所以需要 while 循环穿透到 `SeqScan(users)`，再检查 `id` 是否是索引列。

### 6.4 优化效果示例

```
优化前:                          优化后:
Filter(id >= 10)                 Filter(id >= 10)
  └─ Filter(age < 30)              └─ Filter(age < 30)
       └─ SeqScan(users)                └─ IndexScan(users) [idx:id]
cost = 100 + 10000*0.1 = 1100   cost = 7.6 + 100*0.1 = 17.6
```

代价从 1100 降到 17.6，快 62 倍。

### 6.5 索引选择的局限

miniDB 的规则过于简单，可能做出错误选择：

**反例：大范围查询**

```sql
SELECT * FROM users WHERE id >= 1;   -- 几乎命中所有行
```

miniDB 会选 IndexScan（因为 `id` 是索引列），但实际上全表扫更快——索引扫要随机读 10000 个数据页，全表扫只需顺序读 100 页。

真实数据库会估算命中行数，若超过约 5%~10% 就改用全表扫。这叫"索引跳跃扫描"的决策。

### 6.6 索引选择 vs 代价选择

miniDB 的索引选择是**纯规则**的（有索引就用），没有比较 SeqScan 和 IndexScan 的代价。代价模型只是在选完之后给计划打分，不参与"选不选索引"的决策。

更合理的做法是：同时构造 SeqScan 和 IndexScan 两个计划，用代价模型比较，选代价低的。miniDB 没这么做是为了简化，但 `test_index_scan_cheaper_than_seq` 测试会验证 IndexScan 的代价确实更低。

---

## 7. Join 顺序

### 7.1 为什么 Join 顺序影响巨大

三表 Join：

```sql
SELECT * FROM A JOIN B ON ... JOIN C ON ...;
```

Join 顺序有几种？（假设只考虑左深树，即每次 Join 一个新表）

| 表数 | 左深树顺序数 | 通用公式 |
|---|---|---|
| 2 | 2 | 2! = 2 |
| 3 | 6 | 3! = 6 |
| 4 | 24 | 4! = 24 |
| 5 | 120 | 5! = 120 |
| 10 | 3,628,800 | 10! ≈ 360 万 |

不同顺序的代价可能差几个数量级。

### 7.2 顺序影响代价的示例

三张表：

| 表 | 行数 |
|---|---|
| A | 100 |
| B | 10000 |
| C | 1000000 |

假设每个 Join 选择率 0.1，用 NestedLoopJoin。

**顺序 1：A → B → C**

```
A ⋈ B:  rows = 100 × 10000 × 0.1 = 100,000
(A ⋈ B) ⋈ C:  rows = 100,000 × 1,000,000 × 0.1 = 10,000,000,000
```

**顺序 2：C → B → A**

```
C ⋈ B:  rows = 1,000,000 × 10,000 × 0.1 = 1,000,000,000
(C ⋈ B) ⋈ A:  rows = 1,000,000,000 × 100 × 0.1 = 10,000,000,000
```

最终行数一样，但**中间结果差 10000 倍**！顺序 1 的中间结果只有 10 万行，顺序 2 的中间结果有 10 亿行。中间结果越大，后续 Join 的代价越高、内存占用越大。

**原则**：优先 Join 小表和选择性高的表，让中间结果尽量小。

### 7.3 动态规划思路（System R 算法）

枚举所有顺序是 N! 量级，10 表就 360 万种，不可行。System R 提出动态规划（DP）算法，把复杂度降到 2^N。

**思路**：对表的每个子集，记录"以最小代价 Join 这个子集"的方案。逐步扩大子集，直到包含所有表。

**示例**：表 {A, B, C}，DP 表如下（cost(i) 表示子集 i 的最优代价）：

| 子集 | 最优计划 | 代价 |
|---|---|---|
| {A} | Scan(A) | cost(A) |
| {B} | Scan(B) | cost(B) |
| {C} | Scan(C) | cost(C) |
| {A,B} | min( A⋈B, B⋈A ) | min(...) |
| {A,C} | min( A⋈C, C⋈A ) | min(...) |
| {B,C} | min( B⋈C, C⋈B ) | min(...) |
| {A,B,C} | min( {A,B}⋈C, {A,C}⋈B, {B,C}⋈A ) | min(...) |

每个子集的最优代价由更小的子集组合而来，这就是 DP 的状态转移。

**复杂度**：N 个表有 2^N 个子集，每个子集转移 O(N)，总复杂度 O(N × 2^N)。N=10 时约 1 万次，远小于 360 万。

### 7.4 bushy tree vs 左深树

- **左深树**：每次 Join 一个新表，形状像左斜的树。顺序数 N!。System R 原版用左深树。
- **bushy tree**：允许任意树形，如 (A⋈B)⋈(C⋈D)。顺序数更多，但可能找到更优计划。现代数据库多支持 bushy tree。

### 7.5 遗传算法（GEQO）

表数很多（如 >12）时，2^N 也爆炸。PostgreSQL 用遗传算法（GEQO）近似搜索：随机生成一些顺序，交叉变异保留代价低的，迭代若干代取最优。不保证全局最优，但实践中效果好。

### 7.6 miniDB 的 Join 顺序状态

miniDB phase1 **没有实现** Join 顺序优化，因为 phase1 不支持 Join。Join 节点（`PLAN_NESTED_LOOP_JOIN`、`PLAN_HASH_JOIN`）已定义，代价公式已写好，但 `plan_from_ast` 不会生成 Join 节点。后续 phase 加入 Join 时，这里会接上 DP 算法。

### 7.7 Join 算法选择

除了顺序，还要为每个 Join 选算法：

| 算法 | 适用场景 | 代价 |
|---|---|---|
| **NestedLoopJoin** | 小表、无索引、任意条件 | O(left × right) |
| **HashJoin** | 大表、等值条件、内存够 | O(left + right) |
| **MergeJoin** | 两表都按连接列有序 | O(left + right) |

miniDB 定义了 NestedLoopJoin 和 HashJoin 两种，但选择逻辑未实现（预留）。第 5.7 节的示例展示了 HashJoin 对大表的巨大优势。

---

## 8. 统计信息

代价估算的准确性取决于统计信息。miniDB 只存了行数和页数，真实数据库存得丰富得多。

### 8.1 miniDB 的统计信息

```c
typedef struct {
    char name[CAT_MAX_NAME];
    int  num_rows;            // 行数
    int  num_pages;           // 页数
    bool has_index;           // 有无索引
    char index_col[CAT_MAX_NAME];  // 索引列
} catalog_entry_t;
```

只有四项：行数、页数、有无索引、索引列。选择率只能用固定值（0.01/0.33/0.5），估算很粗。

### 8.2 真实数据库的统计信息

以 PostgreSQL 为例，`pg_statistic` 表存每列的：

| 统计项 | 含义 | 用途 |
|---|---|---|
| **NDV** (Number of Distinct Values) | 不同值数量 | 估算等值选择率 = 1/NDV |
| **NULL 比例** | NULL 值占比 | 估算 `IS NULL` 选择率 |
| **直方图** (Histogram) | 值分布的等频直方图 | 估算范围查询选择率 |
| **MCV** (Most Common Values) | 最常见值及其频率 | 估算高频值的选择率 |
| **相关性** (Correlation) | 物理顺序与逻辑顺序的相关度 | 估算索引扫的随机 I/O 代价 |

### 8.3 直方图（Histogram）

**等频直方图**：把数据排序后分成 K 个桶，每桶装相同数量的行，记录每桶的最小值和最大值。

**示例**：`age` 列有 100 行，分 4 桶：

```
桶1: [18, 25]  ← 25 行
桶2: [26, 35]  ← 25 行
桶3: [36, 50]  ← 25 行
桶4: [51, 80]  ← 25 行
```

查询 `age > 40`：跨桶 3 的部分和整个桶 4。估算：

```
桶3 命中比例 = (50 - 40) / (50 - 36) = 10/14 ≈ 0.71
桶3 命中行数 = 25 × 0.71 ≈ 18
总命中 = 18 + 25 = 43
选择率 = 43 / 100 = 0.43
```

比 miniDB 固定用 0.33 精确得多。

### 8.4 MCV（Most Common Values）

直方图对高频值不友好（一个值可能占好几桶）。MCV 单独记录最常见值及其频率。

**示例**：`city` 列，MCV 表：

| 值 | 频率 |
|---|---|
| 'Beijing' | 0.30 |
| 'Shanghai' | 0.20 |
| 'Guangzhou' | 0.15 |

查询 `city = 'Beijing'`：直接查 MCV，选择率 = 0.30。比 `1/NDV` 精确（NDV 可能 100，1/NDV=0.01，差 30 倍）。

### 8.5 统计信息的收集

- **ANALYZE 命令**（PostgreSQL）：扫描表或采样，计算上述统计信息，存入 `pg_statistic`。
- **自动收集**：很多数据库在数据变动达到阈值时自动更新统计信息。
- **采样**：大表全扫太慢，通常采样部分行估算。

miniDB 没有 ANALYZE，统计信息在 `catalog_add_table` 时由用户手动传入行数和页数。

### 8.6 统计信息过期的危害

统计信息过期会导致代价估算错误，优化器选错计划。常见症状：查询突然变慢（"计划回归"）。

**对策**：
- 定期 ANALYZE。
- 某些数据库支持**自适应查询执行**（如 Spark AQE）：执行过程中发现实际行数与估算差太多，中途切换 Join 算法。

### 8.7 统计信息对比表

| 特性 | miniDB | PostgreSQL |
|---|---|---|
| 行数 | ✅ | ✅ |
| 页数 | ✅ | ✅ |
| 索引信息 | ✅ | ✅ |
| NDV | ❌ | ✅ |
| 直方图 | ❌ | ✅ |
| MCV | ❌ | ✅ |
| 相关性 | ❌ | ✅ |
| 自动收集 | ❌ | ✅ |

---

## 9. 代码逐行解读

本节逐行解读 `phase1/src/optimizer/` 下的四个文件。

### 9.1 catalog.h —— 表元数据定义

```c
#ifndef MINIDB_CATALOG_H
#define MINIDB_CATALOG_H

#include <stdint.h>
#include <stdbool.h>

#define CAT_MAX_TABLES 16     // 最多 16 张表
#define CAT_MAX_NAME 32       // 表名/列名最长 31 字符

typedef struct {
    char name[CAT_MAX_NAME];        // 表名
    int  num_rows;                  // 行数
    int  num_pages;                 // 页数
    bool has_index;                 // 是否有索引
    char index_col[CAT_MAX_NAME];  // 索引列名
} catalog_entry_t;                  // 单张表的元数据

typedef struct {
    catalog_entry_t tables[CAT_MAX_TABLES];  // 表数组
    int num_tables;                          // 当前表数
} catalog_t;                                 // 整个 catalog

// 接口函数
catalog_t *catalog_create(void);
void       catalog_destroy(catalog_t *c);
void       catalog_add_table(catalog_t *c, const char *name,
                             int num_rows, int num_pages,
                             bool has_index, const char *index_col);
const catalog_entry_t *catalog_lookup(const catalog_t *c, const char *name);

#endif
```

**要点**：
- `CAT_MAX_TABLES = 16`：固定数组，最多 16 张表，够教学用。
- `catalog_entry_t`：一张表的统计信息，优化器决策的依据。
- `catalog_lookup`：按表名查表，返回指针或 NULL。

### 9.2 catalog.c —— 元数据存储实现

```c
catalog_t *catalog_create(void) {
    return calloc(1, sizeof(catalog_t));   // 零初始化
}

void catalog_destroy(catalog_t *c) {
    free(c);
}

void catalog_add_table(catalog_t *c, const char *name,
                        int num_rows, int num_pages,
                        bool has_index, const char *index_col) {
    if (c->num_tables >= CAT_MAX_TABLES) return;   // 满了不加
    catalog_entry_t *e = &c->tables[c->num_tables++];  // 取下一个空位
    strncpy(e->name, name, CAT_MAX_NAME - 1);      // 复制表名
    e->num_rows  = num_rows;
    e->num_pages = num_pages;
    e->has_index = has_index;
    if (index_col)
        strncpy(e->index_col, index_col, CAT_MAX_NAME - 1);
    else
        e->index_col[0] = '\0';
}

const catalog_entry_t *catalog_lookup(const catalog_t *c, const char *name) {
    for (int i = 0; i < c->num_tables; i++) {       // 线性查找
        if (strcmp(c->tables[i].name, name) == 0)
            return &c->tables[i];
    }
    return NULL;                                    // 没找到
}
```

**要点**：
- `calloc` 零初始化，`num_tables` 一开始就是 0。
- `strncpy` 限长复制，防止溢出。
- `catalog_lookup` 线性查找，O(N)，N≤16 够用。

### 9.3 logical_plan.h —— 计划节点定义

```c
typedef enum {
    PLAN_SEQ_SCAN = 0,          // 顺序扫描
    PLAN_INDEX_SCAN = 1,        // 索引扫描
    PLAN_FILTER = 2,            // 过滤
    PLAN_PROJECT = 3,           // 投影
    PLAN_NESTED_LOOP_JOIN = 4,  // 嵌套循环连接
    PLAN_HASH_JOIN = 5,         // 哈希连接
} plan_type_t;

typedef struct plan_node plan_node_t;

struct plan_node {
    plan_type_t type;               // 节点类型
    plan_node_t *left;              // 左子树
    plan_node_t *right;             // 右子树
    char table_name[PLAN_MAX_NAME];    // Scan: 表名
    char index_col[PLAN_MAX_NAME];     // IndexScan: 索引列
    ast_expr_t predicate;              // Filter: 谓词
    bool has_predicate;
    char columns[PLAN_MAX_COLS][PLAN_MAX_NAME];  // Project: 列
    int num_cols;
    bool select_all;
    char join_left_col[PLAN_MAX_NAME];   // Join: 左列
    char join_right_col[PLAN_MAX_NAME];  // Join: 右列
    double estimated_rows;       // 估算行数
    double estimated_cost;      // 估算代价
};

plan_node_t *plan_create(plan_type_t type);
void         plan_destroy(plan_node_t *p);
void         plan_print(const plan_node_t *p, int indent);
```

**要点**：
- 用 `typedef struct plan_node plan_node_t;` 前向声明，因为结构体自引用（`left`/`right` 是同类型指针）。
- 一个结构体装下所有算子的所有字段，不同算子用不同字段。简单但浪费空间，工业级会用联合体或继承。
- `estimated_rows` 和 `estimated_cost` 在代价估算阶段填写。

### 9.4 logical_plan.c —— 计划节点实现

```c
plan_node_t *plan_create(plan_type_t type) {
    plan_node_t *p = calloc(1, sizeof(plan_node_t));   // 零初始化
    p->type = type;
    return p;
}

void plan_destroy(plan_node_t *p) {
    if (!p) return;
    plan_destroy(p->left);    // 递归释放左子树
    plan_destroy(p->right);   // 递归释放右子树
    free(p);                  // 释放自己
}
```

`plan_destroy` 是后序遍历释放：先释放子节点，再释放自己。注意递归深度，很深的计划树可能栈溢出（miniDB 教学版不担心）。

```c
static const char *type_str(plan_type_t t) {
    switch (t) {
        case PLAN_SEQ_SCAN:        return "SeqScan";
        case PLAN_INDEX_SCAN:      return "IndexScan";
        case PLAN_FILTER:          return "Filter";
        case PLAN_PROJECT:         return "Project";
        case PLAN_NESTED_LOOP_JOIN: return "NestedLoopJoin";
        case PLAN_HASH_JOIN:       return "HashJoin";
        default: return "Unknown";
    }
}
```

把枚举转成字符串，用于打印。

```c
void plan_print(const plan_node_t *p, int indent) {
    if (!p) return;
    print_indent(indent);                    // 缩进
    printf("%s", type_str(p->type));         // 节点类型名

    switch (p->type) {
        case PLAN_SEQ_SCAN:
        case PLAN_INDEX_SCAN:
            printf("(%s)", p->table_name);   // 表名
            if (p->type == PLAN_INDEX_SCAN)
                printf(" [idx:%s]", p->index_col);  // 索引列
            break;
        case PLAN_FILTER:
            if (p->has_predicate)
                printf("(%s %s)", p->predicate.column, p->predicate.op);
            break;
        case PLAN_PROJECT:
            if (p->select_all) printf("(*)");
            else {
                printf("(");
                for (int i = 0; i < p->num_cols; i++)
                    printf("%s%s", i ? "," : "", p->columns[i]);
                printf(")");
            }
            break;
        case PLAN_NESTED_LOOP_JOIN:
        case PLAN_HASH_JOIN:
            printf("(%s=%s)", p->join_left_col, p->join_right_col);
            break;
    }

    printf("  [rows=%.0f cost=%.1f]\n", p->estimated_rows, p->estimated_cost);

    if (p->left)  plan_print(p->left, indent + 1);   // 递归打印子树
    if (p->right) plan_print(p->right, indent + 1);
}
```

`plan_print` 是前序遍历打印：先打印自己，再打印子节点。`indent` 控制缩进，让树形一目了然。

### 9.5 optimizer.h —— 优化器接口

```c
plan_node_t *optimizer_optimize(ast_stmt_t *stmt, catalog_t *cat);  // 主入口

plan_node_t *plan_from_ast(ast_stmt_t *stmt);      // AST → 逻辑计划
void         opt_pushdown_predicates(plan_node_t *plan);  // 谓词下推（预留）
void         opt_choose_index(plan_node_t *plan, catalog_t *cat);  // 索引选择
void         estimate_costs(plan_node_t *plan, catalog_t *cat);    // 代价估算
```

四个函数对应优化流程的四步。

### 9.6 optimizer.c —— 优化器实现（核心）

#### 9.6.1 plan_from_ast：AST → 逻辑计划

```c
plan_node_t *plan_from_ast(ast_stmt_t *stmt) {
    if (!stmt) return NULL;

    // 1. 创建 Scan 节点（最底层）
    plan_node_t *scan = plan_create(PLAN_SEQ_SCAN);
    strncpy(scan->table_name, stmt->table, PLAN_MAX_NAME - 1);

    plan_node_t *node = scan;

    // 2. 为每个 WHERE 条件创建 Filter 节点，从后往前包裹
    if (stmt->num_where > 0) {
        for (int i = stmt->num_where - 1; i >= 0; i--) {
            plan_node_t *f = plan_create(PLAN_FILTER);
            f->predicate = stmt->where[i];
            f->has_predicate = true;
            f->left = node;      // 新 Filter 包裹之前的节点
            node = f;
        }
    }

    // 3. 如果不是 SELECT *，创建 Project 节点（最顶层）
    if (stmt->type == AST_SELECT && !stmt->select_all && stmt->num_cols > 0) {
        plan_node_t *proj = plan_create(PLAN_PROJECT);
        proj->select_all = false;
        proj->num_cols = stmt->num_cols;
        for (int i = 0; i < stmt->num_cols; i++)
            strncpy(proj->columns[i], stmt->columns[i], PLAN_MAX_NAME - 1);
        proj->left = node;
        node = proj;
    }

    return node;
}
```

**执行过程示例**：

```
SQL: SELECT id FROM users WHERE id >= 10 AND age < 30

AST:
  type=SELECT, table="users"
  columns=["id"], num_cols=1
  where=[{id, >=, 10}, {age, <, 30}]

步骤1: scan = SeqScan(users), node = scan
步骤2: i=1: f1=Filter(age<30), f1->left=scan,        node=f1
       i=0: f0=Filter(id>=10), f0->left=f1,           node=f0
步骤3: proj=Project(id), proj->left=f0,               node=proj

结果:
  Project(id)
    └─ Filter(id >= 10)
         └─ Filter(age < 30)
              └─ SeqScan(users)
```

**为什么从后往前包裹**：`where` 数组里 `where[0]` 是第一个条件（`id>=10`），我们希望它在外层（先执行？其实顺序无所谓，但这样打印出来和 SQL 书写顺序一致）。

#### 9.6.2 opt_pushdown_predicates：谓词下推（预留）

```c
void opt_pushdown_predicates(plan_node_t *plan) {
    (void)plan;   // 什么都不做
}
```

`(void)plan;` 是为了消除"未使用参数"的编译警告。真正的下推逻辑在后续 phase 实现。

#### 9.6.3 try_index_scan：单点索引替换

```c
static void try_index_scan(plan_node_t *plan, catalog_t *cat) {
    if (plan->type != PLAN_FILTER || !plan->has_predicate) return;

    // 穿透多层 Filter 找底层 SeqScan
    plan_node_t *node = plan->left;
    while (node && node->type == PLAN_FILTER)
        node = node->left;
    if (!node || node->type != PLAN_SEQ_SCAN) return;

    // 查 catalog
    const catalog_entry_t *e = catalog_lookup(cat, node->table_name);
    if (!e || !e->has_index) return;

    // 谓词列 == 索引列 → 替换
    if (strcmp(plan->predicate.column, e->index_col) == 0) {
        node->type = PLAN_INDEX_SCAN;
        strncpy(node->index_col, e->index_col, PLAN_MAX_NAME - 1);
    }
}
```

**关键点**：
- 只在 Filter 节点触发，因为索引选择是"Filter 的条件列有索引"。
- while 循环穿透连续 Filter，因为 Filter 可能多层嵌套。
- 只检查**一个**谓词列是否等于索引列，不支持复合索引、不支持多列谓词。
- 替换是**原地修改**：直接改 `node->type`，不改树结构。

#### 9.6.4 opt_choose_index：全树索引选择

```c
void opt_choose_index(plan_node_t *plan, catalog_t *cat) {
    if (!plan) return;
    try_index_scan(plan, cat);                // 当前节点尝试
    opt_choose_index(plan->left, cat);        // 递归左子树
    opt_choose_index(plan->right, cat);       // 递归右子树
}
```

前序遍历整棵树，每个 Filter 节点都尝试索引替换。

#### 9.6.5 selectivity：选择率函数

```c
static double selectivity(const ast_expr_t *expr) {
    if (strcmp(expr->op, "=") == 0)  return 0.01;
    if (strcmp(expr->op, "!=") == 0) return 0.99;
    if (strcmp(expr->op, "<") == 0 || strcmp(expr->op, ">") == 0) return 0.33;
    if (strcmp(expr->op, "<=") == 0 || strcmp(expr->op, ">=") == 0) return 0.33;
    return 0.5;
}
```

按操作符返回固定选择率。详见第 5.2 节。

#### 9.6.6 estimate_costs：代价估算（核心）

```c
void estimate_costs(plan_node_t *plan, catalog_t *cat) {
    if (!plan) return;

    estimate_costs(plan->left, cat);    // 先算子树（后序遍历）
    estimate_costs(plan->right, cat);

    switch (plan->type) {
        case PLAN_SEQ_SCAN: {
            const catalog_entry_t *e = catalog_lookup(cat, plan->table_name);
            if (e) {
                plan->estimated_rows = e->num_rows;
                plan->estimated_cost = e->num_pages;
            } else {
                plan->estimated_rows = 100;    // 表不在 catalog，用默认值
                plan->estimated_cost = 10;
            }
            break;
        }
        case PLAN_INDEX_SCAN: {
            const catalog_entry_t *e = catalog_lookup(cat, plan->table_name);
            if (e) {
                plan->estimated_rows = e->num_rows * 0.01;     // 命中 1%
                plan->estimated_cost = log2(e->num_pages > 0 ? e->num_pages : 1) + 1;
            } else {
                plan->estimated_rows = 1;
                plan->estimated_cost = 3;
            }
            break;
        }
        case PLAN_FILTER: {
            double sel = plan->has_predicate ? selectivity(&plan->predicate) : 1.0;
            plan->estimated_rows = plan->left->estimated_rows * sel;
            plan->estimated_cost = plan->left->estimated_cost
                                 + plan->left->estimated_rows * 0.1;
            break;
        }
        case PLAN_PROJECT: {
            plan->estimated_rows = plan->left->estimated_rows;
            plan->estimated_cost = plan->left->estimated_cost
                                 + plan->left->estimated_rows * 0.05;
            break;
        }
        case PLAN_NESTED_LOOP_JOIN: {
            plan->estimated_rows = plan->left->estimated_rows * plan->right->estimated_rows * 0.1;
            plan->estimated_cost = plan->left->estimated_cost
                                 + plan->left->estimated_rows * plan->right->estimated_cost;
            break;
        }
        case PLAN_HASH_JOIN: {
            plan->estimated_rows = plan->left->estimated_rows * plan->right->estimated_rows * 0.1;
            plan->estimated_cost = plan->left->estimated_cost + plan->right->estimated_cost
                                 + plan->left->estimated_rows + plan->right->estimated_rows;
            break;
        }
    }
}
```

**关键点**：
- **后序遍历**：先递归算子树，再用子树结果算自己。保证算父节点时子节点的 `estimated_rows/cost` 已就绪。
- **SeqScan**：行数 = 表行数，代价 = 表页数。
- **IndexScan**：行数 = 表行数 × 0.01（固定 1%），代价 = log₂(页数) + 1（B+ 树深度 + 1 次数据页读）。
- **Filter**：行数 = 输入行数 × 选择率，代价 = 输入代价 + 输入行数 × 0.1（每行判断谓词的开销）。
- **Project**：行数不变，代价 = 输入代价 + 输入行数 × 0.05（裁列比谓词判断更轻）。
- **NestedLoopJoin**：行数 = 左 × 右 × 0.1，代价 = 左代价 + 左行数 × 右代价（对左每行扫一遍右）。
- **HashJoin**：行数同上，代价 = 左代价 + 右代价 + 左行数 + 右行数（各扫一次 + 建哈希表）。

#### 9.6.7 optimizer_optimize：主入口

```c
plan_node_t *optimizer_optimize(ast_stmt_t *stmt, catalog_t *cat) {
    plan_node_t *plan = plan_from_ast(stmt);        // 1. AST → 逻辑计划
    if (!plan) return NULL;
    opt_pushdown_predicates(plan);                  // 2. 谓词下推（预留）
    opt_choose_index(plan, cat);                    // 3. 索引选择
    estimate_costs(plan, cat);                      // 4. 代价估算
    return plan;
}
```

四步顺序执行，返回最终计划。调用方拿到计划后可以 `plan_print` 打印，或交给执行引擎执行。

### 9.7 完整调用示例

```c
// 1. 准备 Catalog
catalog_t *cat = catalog_create();
catalog_add_table(cat, "users", 10000, 100, true, "id");

// 2. 解析 SQL
parser_t *p = parser_create("SELECT id FROM users WHERE id = 42");
ast_stmt_t *stmt = parser_parse(p);

// 3. 优化
plan_node_t *plan = optimizer_optimize(stmt, cat);

// 4. 查看计划
plan_print(plan, 0);
// 输出:
// Project(id)  [rows=1 cost=17.7]
//   Filter(id =)  [rows=1 cost=17.6]
//     IndexScan(users) [idx:id]  [rows=100 cost=7.6]

// 5. 清理
plan_destroy(plan);
free(stmt);
parser_destroy(p);
catalog_destroy(cat);
```

**输出解读**：
- `IndexScan(users) [idx:id]`：走 id 索引扫描 users，估算输出 100 行（10000 × 0.01），代价 7.6（log₂100 + 1）。
- `Filter(id =)`：过滤，输出 1 行（100 × 0.01），代价 17.6（7.6 + 100 × 0.1）。
- `Project(id)`：投影，输出 1 行，代价 17.7（17.6 + 1 × 0.05）。

---

## 10. 与真实数据库对比

### 10.1 优化策略对比

| 特性 | miniDB | PostgreSQL | Oracle |
|---|---|---|---|
| 优化策略 | 启发式 + 简单代价 | 启发式 + 代价 | 启发式 + 代价 + 自学习 |
| 计划枚举 | 不枚举 | DP（左深+bushy） | DP + 遗传 |
| Join 顺序 | 未实现 | System R DP | DP + 并行 |
| Join 算法选择 | 未实现 | 代价比较 | 代价比较 |
| 统计信息 | 行数+页数 | 直方图+MCV+相关性 | 直方图+MCV+采样 |
| 子查询优化 | 不支持 | 展开+去相关 | 展开+去相关+合并 |
| 自适应 | 无 | AQE（PG 14+） | 自适应执行 |
| 并行计划 | 无 | 支持 | 支持 |

### 10.2 代价模型对比

| 维度 | miniDB | PostgreSQL |
|---|---|---|
| 代价单位 | 页读取次数 | 随机页 + 顺序页 + CPU 行 |
| SeqScan 代价 | num_pages | random_page_cost × pages |
| IndexScan 代价 | log₂(pages)+1 | 随机读 + 顺序读 + CPU |
| Filter 代价 | rows × 0.1 | cpu_operator_cost × rows |
| Join 代价 | 固定公式 | 按算法分别建模 |
| 缓存 | 不考虑 | 命中缓存的页代价低 |

PostgreSQL 的 `random_page_cost` 默认 4.0，`seq_page_cost` 默认 1.0，`cpu_tuple_cost` 默认 0.01。这些参数可以调，适应不同硬件。

### 10.3 统计信息对比

详见第 8.7 节。

### 10.4 优化器架构对比

| 架构 | 代表 | 特点 |
|---|---|---|
| **规则+代价两阶段** | miniDB、早期 System R | 先规则改写，再代价搜索 |
| **Volcano/Cascades** | SQL Server、Orca、PG 逐步采用 | 统一规则和代价，自顶向下搜索，Memo 数据结构 |
| **ML 辅助** | Oracle、SageDB | 用机器学习预测代价、选择计划 |

miniDB 属于最简单的"规则+代价两阶段"，且代价部分只标注不选择。是教学用的最小可用优化器。

### 10.5 miniDB 简化的合理性

miniDB 的简化对教学是合理的：

| 简化 | 为什么合理 |
|---|---|
| 不枚举计划 | 单表查询没有枚举空间 |
| 固定选择率 | 教学重点在流程不在精度 |
| 不考虑缓存 | 引入缓存会大幅增加复杂度 |
| 不支持子查询 | phase1 范围有限 |

理解了 miniDB 的优化器，再读 PostgreSQL 的 `src/backend/optimizer/` 会顺畅很多——核心思路一样，只是统计信息更丰富、枚举更彻底、代价模型更精细。

---

## 11. 习题

### 11.1 基础题

**题 1**：把下面 SQL 翻译成关系代数。

```sql
SELECT age FROM users WHERE city = 'Beijing' AND age > 18;
```

<details><summary>答案</summary>

```
π_age( σ_age>18( σ_city='Beijing'( users ) ) )
```

或合并谓词：

```
π_age( σ_city='Beijing' ∧ age>18( users ) )
```

</details>

**题 2**：`users` 表 10000 行 100 页，`id` 有索引。估算下面 SQL 的行数和代价（用 miniDB 的代价模型）。

```sql
SELECT * FROM users WHERE id = 42;
```

<details><summary>答案</summary>

IndexScan（id 有索引）：

```
IndexScan: rows = 10000 × 0.01 = 100, cost = log₂(100) + 1 ≈ 7.6
Filter:    rows = 100 × 0.01 = 1,    cost = 7.6 + 100 × 0.1 = 17.6
```

</details>

**题 3**：同上表，估算下面 SQL 的行数和代价。

```sql
SELECT * FROM users WHERE age > 30;
```

<details><summary>答案</summary>

`age` 无索引，SeqScan：

```
SeqScan: rows = 10000, cost = 100
Filter:  rows = 10000 × 0.33 = 3300, cost = 100 + 10000 × 0.1 = 1100
```

</details>

### 11.2 进阶题

**题 4**：三表 Join，A 100 行、B 10000 行、C 1000000 行，NestedLoopJoin，选择率 0.1。比较顺序 A→B→C 和 C→B→A 的中间结果行数。

<details><summary>答案</summary>

A→B→C：
```
A⋈B: 100 × 10000 × 0.1 = 100,000
(A⋈B)⋈C: 100,000 × 1,000,000 × 0.1 = 10,000,000,000
```

C→B→A：
```
C⋈B: 1,000,000 × 10,000 × 0.1 = 1,000,000,000
(C⋈B)⋈A: 1,000,000,000 × 100 × 0.1 = 10,000,000,000
```

最终行数相同，但中间结果 A→B→C 是 10 万，C→B→A 是 10 亿，差 10000 倍。A→B→C 远优。

</details>

**题 5**：为什么 miniDB 的 IndexScan 行数固定用 `num_rows × 0.01`，而不按谓词类型区分？这样有什么问题？

<details><summary>答案</summary>

简化教学。问题是范围查询（如 `id BETWEEN 1 AND 10`）的命中行数可能远多于 1%，用 0.01 会严重低估，导致后续算子估算错误。真实数据库会按范围大小估算。

</details>

**题 6**：给下面 SQL 设计两种执行计划，分别估算代价，说明哪种更优。

```sql
SELECT name FROM users WHERE age > 60 AND city = 'Beijing';
```

假设 `users` 10000 行 100 页，`city` 有索引，`age` 无索引。

<details><summary>答案</summary>

方案 A：SeqScan → Filter(age>60) → Filter(city=) → Project

```
SeqScan:        rows=10000, cost=100
Filter(age>60): rows=3300,  cost=100 + 10000×0.1 = 1100
Filter(city=):  rows=33,    cost=1100 + 3300×0.1 = 1430
Project:        rows=33,    cost=1430 + 33×0.05 ≈ 1432
```

方案 B：IndexScan(city) → Filter(age>60) → Project

```
IndexScan:      rows=100,   cost=7.6
Filter(age>60): rows=33,    cost=7.6 + 100×0.1 = 17.6
Project:        rows=33,    cost=17.6 + 33×0.05 ≈ 19.3
```

方案 B 远优（19.3 vs 1432）。miniDB 的 `opt_choose_index` 会选方案 B。

</details>

### 11.3 思考题

**题 7**：miniDB 的 `try_index_scan` 只检查一个谓词列。如果 SQL 有 `WHERE id = 42 AND age > 30`，`id` 有索引，会发生什么？画出优化后的计划。

<details><summary>答案</summary>

外层 Filter 是 `id = 42`，穿透内层 `Filter(age > 30)` 找到 SeqScan，发现 `id` 是索引列，替换为 IndexScan。计划：

```
Filter(id =)
  └─ Filter(age >)
       └─ IndexScan(users) [idx:id]
```

注意：只有 `id` 的过滤"对应"到了 IndexScan，`age > 30` 仍是普通 Filter。这是合理的——索引只帮了 `id` 的过滤。

</details>

**题 8**：如果 `users` 表只有 5 行 1 页，`id` 有索引，`SELECT * FROM users WHERE id = 42` 该用 IndexScan 还是 SeqScan？miniDB 会选哪个？应该选哪个？

<details><summary>答案</summary>

- miniDB 会选 IndexScan（规则：有索引就用）。
- 实际应该选 SeqScan：表就 1 页，全表扫代价 1；索引扫要查 B+ 树再读数据页，代价 ≥ 2。小表上索引不划算。
- 这暴露了 miniDB 纯规则选择的缺陷——应该用代价模型比较。

</details>

**题 9**：为什么 `estimate_costs` 用后序遍历（先递归子树再算自己），而不是前序？

<details><summary>答案</summary>

因为父节点的代价公式依赖子节点的 `estimated_rows` 和 `estimated_cost`。必须先算出子节点，才能算父节点。后序遍历正好保证"子节点先于父节点计算"。

</details>

**题 10**：给 miniDB 加谓词下推，需要修改哪些函数？大致思路是什么？

<details><summary>答案</summary>

修改 `opt_pushdown_predicates`。思路：
1. 遍历计划树，找到 Filter 节点。
2. 检查 Filter 的谓词只涉及哪个子树的列。
3. 把 Filter 移极推到那个子树的根部（越过不相关的算子）。
4. 注意保持等价：只能越过不改变谓词列的算子（如 Project 不裁掉该列、Join 不改变该列）。

需要 catalog 提供列所属表的信息，当前 catalog 没有，要扩展。

</details>

### 11.4 编程题

**题 11**：修改 `selectivity` 函数，让等值查询的选择率 = `1 / NDV`（NDV 从 catalog 取）。需要扩展 `catalog_entry_t` 加 `ndv` 字段。

**题 12**：实现一个简化的谓词下推：只处理 `Filter → Join → Scan` 形式，把只涉及左 Scan 列的 Filter 推到 Join 的左分支。

**题 13**：加一个 `opt_choose_join_algo` 函数，在 HashJoin 和 NestedLoopJoin 之间按代价选择。

---

## 12. 文件清单与测试覆盖

### 12.1 文件清单

| 文件 | 行数 | 职责 |
|---|---|---|
| `catalog.h` | 30 | 表元数据结构定义 + 接口声明 |
| `catalog.c` | 34 | 元数据存储实现（create/add/lookup） |
| `logical_plan.h` | 48 | 计划节点结构定义 + 接口声明 |
| `logical_plan.c` | 70 | 计划节点实现（create/destroy/print） |
| `optimizer.h` | 15 | 优化器接口声明 |
| `optimizer.c` | 139 | 优化器实现（from_ast/索引选择/代价估算） |
| `test_optimizer.c` | — | 10 个测试 |

### 12.2 测试覆盖

```
test_seq_scan_cost           — SeqScan 代价正确
test_index_scan_selection     — 有索引时选 IndexScan
test_filter_selectivity       — Filter 选择率估算
test_project_on_top           — Project 在顶层
test_multiple_filters         — 多个 Filter 嵌套 + 索引穿透
test_index_scan_cheaper_than_seq — IndexScan 代价比 SeqScan 低
test_catalog_lookup           — Catalog 查找
test_plan_print               — 计划打印
test_no_index_stays_seq_scan  — 无索引时保持 SeqScan
test_large_table_index_better — 大表索引优势明显
```

### 12.3 模块依赖

```
optimizer.c
  ├── catalog.h    (查表元数据)
  ├── logical_plan.h (计划节点操作)
  └── ast.h        (谓词表达式)
```

优化器是 AST 和 Catalog 的消费者，产出计划树给执行引擎。

---

## 13. 下一步

优化器生成物理计划后，下一章实现**执行引擎**：按 Volcano 模型自顶向下执行计划树，每个算子提供 `open/next/close` 接口。

```
优化器输出:               执行引擎:
Project(id)               Project.next() → 调 Filter.next()，取 id 列
  └─ Filter(id =)         Filter.next() → 调 IndexScan.next()，判断谓词
       └─ IndexScan       IndexScan.next() → 走 B+ 树取下一行
```

执行引擎把"计划"变成"动作"，最终产出查询结果行。

---

## 附录 A：关系代数速查表

| 符号 | 名称 | SQL | miniDB 节点 |
|---|---|---|---|
| σ_p(R) | 选择 | `WHERE p` | PLAN_FILTER |
| π_L(R) | 投影 | `SELECT L` | PLAN_PROJECT |
| R ⋈ S | 连接 | `JOIN` | PLAN_*_JOIN |
| R ∪ S | 并 | `UNION` | 未实现 |
| R ∩ S | 交 | `INTERSECT` | 未实现 |
| R − S | 差 | `EXCEPT` | 未实现 |
| R × S | 笛卡尔积 | `FROM R, S` | NestedLoopJoin 无条件 |

## 附录 B：代价公式速查表

| 算子 | 行数 | 代价 |
|---|---|---|
| SeqScan | num_rows | num_pages |
| IndexScan | num_rows × 0.01 | log₂(num_pages) + 1 |
| Filter | in_rows × sel | in_cost + in_rows × 0.1 |
| Project | in_rows | in_cost + in_rows × 0.05 |
| NestedLoopJoin | L × R × 0.1 | L_cost + L_rows × R_cost |
| HashJoin | L × R × 0.1 | L_cost + R_cost + L_rows + R_rows |

## 附录 C：选择率速查表

| 操作符 | 选择率 |
|---|---|
| `=` | 0.01 |
| `!=` | 0.99 |
| `<` / `>` | 0.33 |
| `<=` / `>=` | 0.33 |
| 其他 | 0.5 |

## 附录 D：术语表

| 术语 | 英文 | 含义 |
|---|---|---|
| 优化器 | Optimizer | 选最优执行计划的组件 |
| 逻辑计划 | Logical Plan | 用关系代数描述的"做什么" |
| 物理计划 | Physical Plan | 用具体算法描述的"怎么做" |
| 代价 | Cost | 执行计划的估算开销 |
| 选择率 | Selectivity | 过滤后行数 / 过滤前行数 |
| 基数 | Cardinality | 某算子输出的行数 |
| 谓词下推 | Predicate Pushdown | 把 Filter 推近 Scan |
| 投影裁剪 | Projection Pruning | 去掉无用列 |
| 直方图 | Histogram | 值分布的统计 |
| MCV | Most Common Values | 最常见值及频率 |
| NDV | Number of Distinct Values | 不同值数量 |
| Catalog | — | 表元数据存储 |
| 启发式 | Heuristic | 用经验规则改写 |
| 动态规划 | Dynamic Programming | DP 搜索最优 Join 顺序 |
| 左深树 | Left-Deep Tree | 每次 Join 一个新表的树形 |
| bushy tree | — | 任意树形的 Join |

---

> 本文档基于 `phase1/src/optimizer/` 源码编写，共解读 6 个源文件（约 336 行 C 代码）。所有代价示例均可用 miniDB 的代价模型复现。
