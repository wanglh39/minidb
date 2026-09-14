# 章9：执行引擎

> 优化器生成计划树后，谁来执行？本章实现 Volcano 迭代器模型——每个算子提供 `open/next/close` 接口，数据自底向上流动。

## Volcano 模型

```
Project(id)              ← root.next()
  └─ Filter(age > 28)     ← child.next()
       └─ SeqScan(users)  ← 从表读一行
```

**拉模式（Pull）**：根算子调用 `next()`，递归地从子算子拉取数据。

| 方法 | 作用 |
|---|---|
| `open()` | 初始化算子状态 |
| `next(out)` | 取下一行，返回 true/false |
| `close()` | 释放资源 |

### 为什么用迭代器模型？

| 模型 | 优点 | 缺点 |
|---|---|---|
| **Volcano（迭代器）** | 流式处理，内存占用低 | 函数调用开销 |
| **物化（Materialize）** | 简单 | 中间结果全驻内存 |
| **向量化（Vectorized）** | 批处理高效 | 实现复杂 |

教学项目选择 Volcano：经典、易理解、与 PostgreSQL/SQLite 一致。

## 数据格式

### 行

```c
typedef struct {
    int32_t values[EXEC_MAX_COLS];  // 列值（简化：全部 int32）
    int num_cols;
} exec_row_t;
```

### 表（数据源）

```c
typedef struct {
    char name[32];                  // 表名
    exec_col_meta_t cols[16];       // 列元数据
    int num_cols;
    exec_row_t *rows;               // 行数组
    int num_rows;
} exec_table_t;
```

> **简化说明**：教学版用内存数组代替磁盘读取。生产系统会从 Buffer Pool → Heap → Tuple 逐层读取。

## 算子实现

### SeqScan / IndexScan

```c
case PLAN_SEQ_SCAN:
case PLAN_INDEX_SCAN: {
    if (op->pos >= op->table->num_rows) return false;
    *out = op->table->rows[op->pos++];  // 返回下一行
    return true;
}
```

最简单的算子：维护一个位置指针，逐行返回。

### Filter

```c
case PLAN_FILTER: {
    exec_row_t row;
    while (executor_next(op->left, &row)) {        // 从子算子拉一行
        if (eval_predicate(&op->predicate, &row)) { // 满足条件？
            *out = row;
            return true;                            // 返回匹配的行
        }
        // 不满足，继续拉下一行
    }
    return false;  // 子算子耗尽
}
```

Filter 是一个**消费者**：它调用子算子的 `next()`，跳过不满足条件的行。

### Project

```c
case PLAN_PROJECT: {
    exec_row_t row;
    if (!executor_next(op->left, &row)) return false;
    // 只保留需要的列
    out->num_cols = op->proj_num_cols;
    for (int i = 0; i < op->proj_num_cols; i++)
        out->values[i] = row.values[op->proj_indices[i]];
    return true;
}
```

Project 从子算子取一行，然后只输出需要的列。

## 谓词求值

```c
static bool eval_predicate(const ast_expr_t *pred,
                           const exec_row_t *row,
                           const exec_table_t *table) {
    int idx = find_col_idx(table, pred->column);  // 找到列索引
    int32_t v = row->values[idx];                  // 取列值
    int32_t target = pred->value.int_val;          // 取比较值

    switch (pred->op) {
        case "=":  return v == target;
        case "!=": return v != target;
        case "<":  return v <  target;
        case ">":  return v >  target;
        case "<=": return v <= target;
        case ">=": return v >= target;
    }
}
```

## 完整执行流程

```c
// 1. 准备数据
exec_table_t users = { .name = "users", .num_cols = 3, ... };

// 2. 解析 SQL
parser_t *p = parser_create("SELECT id FROM users WHERE age > 28");
ast_stmt_t *stmt = parser_parse(p);

// 3. 优化
plan_node_t *plan = optimizer_optimize(stmt, catalog);

// 4. 执行
result_set_t *rs = executor_run(plan, &users, 1);

// 5. 查看结果
result_set_print(rs);
// 输出:
// id
// ---
// 2
// 3
// 5
// (3 rows)
```

### executor_run 内部

```c
result_set_t *executor_run(plan, tables, n) {
    operator_t *op = executor_build(plan, tables, n);  // 构建算子树
    executor_open(op);                                 // 初始化

    exec_row_t row;
    while (executor_next(op, &row))                    // 反复拉取
        rs->rows[rs->num_rows++] = row;               // 收集结果

    executor_close(op);
    executor_destroy(op);
    return rs;
}
```

## 执行示例

```
SQL: SELECT id FROM users WHERE age > 28

数据:
  users = [(1,25,85), (2,30,90), (3,35,75), (4,28,95), (5,40,60)]

计划树:
  Project(id)
    Filter(age > 28)
      SeqScan(users)

执行过程:
  Project.next() →
    Filter.next() →
      SeqScan.next() → (1,25,85) → age>28? NO
      SeqScan.next() → (2,30,90) → age>28? YES → 返回 (2,30,90)
    Project 取 col[0] → (2) → 返回
  Project.next() →
    Filter.next() →
      SeqScan.next() → (3,35,75) → age>28? YES → 返回 (3,35,75)
    Project 取 col[0] → (3) → 返回
  Project.next() →
    Filter.next() →
      SeqScan.next() → (4,28,95) → age>28? NO
      SeqScan.next() → (5,40,60) → age>28? YES → 返回 (5,40,60)
    Project 取 col[0] → (5) → 返回
  Project.next() →
    Filter.next() →
      SeqScan.next() → EOF → false
    → false
  → false

结果: [(2), (3), (5)]
```

## 结果集

```c
typedef struct {
    exec_row_t rows[1024];        // 结果行
    int num_rows;
    exec_col_meta_t cols[16];     // 列名
    int num_cols;
} result_set_t;
```

`result_set_print()` 以表格形式输出：

```
id | age | score
--- | --- | ---
1 | 25 | 85
2 | 30 | 90
(2 rows)
```

## 与工业级执行引擎的差距

| 特性 | miniDB | PostgreSQL |
|---|---|---|
| 模型 | Volcano | Volcano + 向量化 |
| 数据格式 | int32 数组 | 多类型 + 压缩 |
| Join | 未实现 | NestedLoop / Hash / Merge |
| 聚合 | 未实现 | HashAgg / SortAgg |
| 排序 | 未实现 | Sort 算子 |
| 并行 | 单线程 | 并行算子 |
| Pipeline | 无 | 流水线执行 |

## 文件清单

| 文件 | 职责 |
|---|---|
| `executor.h/c` | Volcano 算子 + 结果集 |
| `test_executor.c` | 10 个测试 |

## 测试覆盖

```
test_seq_scan_all          — 全表扫描返回所有行
test_filter_equal          — 等值过滤 (id = 3)
test_filter_less_than      — 范围过滤 (age < 30)
test_filter_greater_equal  — 范围过滤 (score >= 90)
test_project_single_col    — 投影单列 (SELECT id)
test_project_with_filter   — 投影 + 过滤组合
test_multiple_filters      — 多条件 AND 过滤
test_filter_no_match       — 无匹配返回空
test_filter_not_equal      — 不等过滤 (!=)
test_result_set_print      — 结果集打印
```

## 下一步

执行引擎就绪后，下一章实现**网络协议与 CLI**：让用户通过命令行输入 SQL，实时查看执行结果。