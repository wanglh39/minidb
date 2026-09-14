# 章8：查询优化

> 同一条 SQL 可以有多种执行方式。`SELECT * FROM users WHERE id = 42` 既可以全表扫描，也可以用索引。优化器选择代价最小的方案。

## 优化流程

```
AST ──plan_from_ast──▶ 逻辑计划 ──启发式规则──▶ 优化计划 ──代价估算──▶ 物理计划
                          │                      │                      │
                     Scan/Filter/Project     IndexScan 替换        rows/cost 标注
```

## 三层抽象

| 层 | 作用 | 示例 |
|---|---|---|
| **逻辑计划** | 做什么（关系代数） | Scan(users) → Filter(id=42) |
| **物理计划** | 怎么做（具体算法） | SeqScan / IndexScan |
| **代价估算** | 做多快 | rows=1, cost=7.6 |

## Catalog（表统计信息）

优化器需要知道表的元数据来做决策：

```c
catalog_t *cat = catalog_create();
catalog_add_table(cat, "users",   10000, 100, true,  "id");      // 1万行，100页，有索引
catalog_add_table(cat, "orders",  50000, 500, true,  "user_id"); // 5万行，500页，有索引
catalog_add_table(cat, "products", 1000,  10, false, "");        // 1千行，10页，无索引
```

| 字段 | 用途 |
|---|---|
| `num_rows` | 估算 Filter 后的行数 |
| `num_pages` | 估算 SeqScan 代价 |
| `has_index` + `index_col` | 决定是否用 IndexScan |

## 计划节点

```c
typedef enum {
    PLAN_SEQ_SCAN,          // 顺序扫描
    PLAN_INDEX_SCAN,        // 索引扫描
    PLAN_FILTER,            // 过滤
    PLAN_PROJECT,           // 投影
    PLAN_NESTED_LOOP_JOIN,  // 嵌套循环连接
    PLAN_HASH_JOIN,         // 哈希连接
} plan_type_t;
```

计划是一棵树：

```
Project(id)                    ← 顶层：选择列
  └─ Filter(id =)              ← 中间：WHERE 条件
       └─ IndexScan(users)     ← 底层：数据访问
```

## AST → 逻辑计划

```c
plan_node_t *plan_from_ast(ast_stmt_t *stmt) {
    // 1. 创建 Scan 节点
    plan_node_t *scan = plan_create(PLAN_SEQ_SCAN);
    strcpy(scan->table_name, stmt->table);

    // 2. 为每个 WHERE 条件创建 Filter 节点（嵌套）
    for (int i = num_where - 1; i >= 0; i--)
        node = wrap_filter(node, stmt->where[i]);

    // 3. 如果不是 SELECT *，创建 Project 节点
    if (stmt->num_cols > 0)
        node = wrap_project(node, stmt->columns);

    return node;
}
```

### 示例

```
SQL: SELECT id FROM users WHERE id >= 10 AND age < 30

AST:
  type=SELECT, table="users"
  columns=["id"], num_cols=1
  where=[{id, >=, 10}, {age, <, 30}]

逻辑计划:
  Project(id)
    Filter(id >= 10)
      Filter(age < 30)
        SeqScan(users)
```

## 启发式优化：索引选择

**规则**：如果 Filter 的条件列有索引，将 SeqScan 替换为 IndexScan。

```c
static void try_index_scan(plan_node_t *plan, catalog_t *cat) {
    if (plan->type != PLAN_FILTER) return;

    // 穿透多层 Filter 找到底层的 SeqScan
    plan_node_t *node = plan->left;
    while (node && node->type == PLAN_FILTER)
        node = node->left;
    if (!node || node->type != PLAN_SEQ_SCAN) return;

    // 检查 Filter 条件列是否是索引列
    const catalog_entry_t *e = catalog_lookup(cat, node->table_name);
    if (e && e->has_index &&
        strcmp(plan->predicate.column, e->index_col) == 0) {
        node->type = PLAN_INDEX_SCAN;  // 替换！
    }
}
```

### 优化效果

```
优化前:                          优化后:
Filter(id >= 10)                 Filter(id >= 10)
  Filter(age < 30)                 Filter(age < 30)
    SeqScan(users)                   IndexScan(users) [idx:id]
cost = 100 + 10000*0.1 = 1100   cost = 7.6 + 100*0.1 = 17.6
```

## 代价模型

自底向上递归估算每个节点的 `estimated_rows` 和 `estimated_cost`：

| 算子 | 行数估算 | 代价估算 |
|---|---|---|
| **SeqScan** | num_rows | num_pages |
| **IndexScan** | num_rows × 0.01 | log₂(num_pages) + 1 |
| **Filter** | input_rows × selectivity | input_cost + input_rows × 0.1 |
| **Project** | input_rows | input_cost + input_rows × 0.05 |
| **NestedLoopJoin** | left × right × 0.1 | left_cost + left_rows × right_cost |
| **HashJoin** | left × right × 0.1 | left_cost + right_cost + left + right |

### 选择率（Selectivity）

```c
static double selectivity(const ast_expr_t *expr) {
    if (op == "=")  return 0.01;  // 等值：1% 匹配
    if (op == "!=") return 0.99;  // 不等：99% 匹配
    if (op == "<" || op == ">")  return 0.33;  // 范围：33% 匹配
    if (op == "<=" || op == ">=") return 0.33;
    return 0.5;  // 默认
}
```

### 代价比较示例

```
users 表：10000 行，100 页，有 id 索引

方案1: SeqScan → 全表扫描
  cost = 100 (页数)

方案2: IndexScan → 索引查找
  cost = log₂(100) + 1 = 7.6

→ 选 IndexScan (7.6 < 100)
```

## 完整使用示例

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

## 优化规则全景

| 规则 | 作用 | 状态 |
|---|---|---|
| **索引选择** | Filter 列有索引 → IndexScan | ✅ 已实现 |
| **谓词下推** | Filter 推到 Scan 附近 | 预留 |
| **投影裁剪** | 去掉不需要的列 | 预留 |
| **Join 顺序** | DP 选最优顺序 | 预留 |
| **常量折叠** | `1+1` → `2` | 未实现 |

## 与工业级优化器的差距

| 特性 | miniDB | PostgreSQL |
|---|---|---|
| 优化策略 | 启发式 | 启发式 + 代价 |
| Join 顺序 | 未实现 | 动态规划 / 遗传算法 |
| 统计信息 | 行数 + 页数 | 直方图 + MCV + 相关性 |
| 子查询优化 | 不支持 | 展开 + 去相关 |
| 自适应 | 无 | 自适应查询执行 |

## 文件清单

| 文件 | 职责 |
|---|---|
| `catalog.h/c` | 表元数据存储 |
| `logical_plan.h/c` | 计划节点定义 + 打印 |
| `optimizer.h/c` | 优化器入口 + 索引选择 + 代价估算 |
| `test_optimizer.c` | 10 个测试 |

## 测试覆盖

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

## 下一步

优化器生成物理计划后，下一章实现**执行引擎**：按 Volcano 模型自顶向下执行计划树，每个算子提供 `open/next/close` 接口。