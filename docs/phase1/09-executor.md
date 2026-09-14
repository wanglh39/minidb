# 章9：执行引擎

> 优化器生成计划树后，谁来执行？本章实现 Volcano 迭代器模型——每个算子提供 `open/next/close` 接口，数据自底向上流动。

## 本章学习路线

| 节 | 主题 | 面向问题 |
|---|---|---|
| 9.1 | 执行引擎的职责 | 计划树如何变成结果？ |
| 9.2 | Volcano 模型详解 | open/next/close 是什么？ |
| 9.3 | 迭代器模式 | 为什么数据库用迭代器？ |
| 9.4 | 数据格式 | 行和表在内存里长什么样？ |
| 9.5 | 各算子详解 | Scan/Filter/Project 怎么工作？ |
| 9.6 | 表达式求值 | WHERE 条件怎么判断真假？ |
| 9.7 | 数据流 | 一行数据如何从底层流到顶层？ |
| 9.8 | 向量化执行 | 为什么 Volcano 慢？怎么变快？ |
| 9.9 | 代码逐行解读 | executor.h/c 每一行在做什么？ |
| 9.10 | 内存管理 | 结果集怎么收集？为什么有上限？ |
| 9.11 | 与真实数据库对比 | miniDB 离 SQLite/PostgreSQL 多远？ |
| 9.12 | 习题 | 你真的理解了吗？ |

---

## 9.1 执行引擎的职责

### 9.1.1 执行引擎在整个系统中处于什么位置？

回忆一下前面几章，一条 SQL 从字符串到结果，要经过四个阶段：

```
SQL 字符串
   │  ① 词法/语法分析（第6-7章）
   ▼
AST 抽象语法树
   │  ② 语义分析 + 逻辑计划 + 优化（第8章）
   ▼
计划树 (plan_node_t)
   │  ③ 执行引擎（本章）   ← 你在这里
   ▼
结果集 (result_set_t)
   │  ④ 网络/CLI 输出（第10章）
   ▼
用户看到的表格
```

**执行引擎的输入**：一棵计划树（由 `plan_node_t` 组成的树形结构）。
**执行引擎的输出**：一个结果集（`result_set_t`，包含若干行数据）。

### 9.1.2 为什么需要单独的"执行层"？

你可能会问：优化器既然已经选好了"先扫描、再过滤、再投影"的顺序，为什么不直接在优化器里跑完算了？

原因有三：

| 原因 | 说明 |
|---|---|
| **职责分离** | 优化器只决定"做什么、什么顺序"，不关心"怎么取一行、怎么比较两个数"。执行器只关心"怎么取数据"，不关心"先过滤还是先投影更省"。各管一摊，便于独立修改。 |
| **可替换性** | 同一棵计划树，可以用 Volcano 模型跑，也可以用向量化模型跑，也可以编译成字节码跑。执行层独立，就能换不同实现做性能对比。 |
| **流水线机会** | 执行层可以一边算一边输出（流式），不用等全部算完才返回。这对 `LIMIT 10` 这种查询尤其重要——找到 10 行就能停。 |

### 9.1.3 执行引擎要回答的三个核心问题

1. **数据从哪里来？** → Scan 算子（从表里读行）
2. **数据要不要保留？** → Filter 算子（按条件过滤）
3. **数据要变成什么样？** → Project 算子（选列、计算）

本章实现的三个算子，正好对应这三个问题。后续章节会加入 Join（数据怎么拼接）、Agg（数据怎么汇总）、Sort（数据怎么排序）。

---

## 9.2 Volcano 模型详解

### 9.2.1 一句话理解

Volcano 模型由 Goetz Graefe 在 1994 年提出。它的核心思想是：

> **每个算子都是一个迭代器，提供三个方法：`open` / `next` / `close`。父算子通过反复调用子算子的 `next()` 来拉取数据。**

### 9.2.2 三个接口的职责

```
Project(id)              ← root.next()
  └─ Filter(age > 28)     ← child.next()
       └─ SeqScan(users)  ← 从表读一行
```

| 方法 | 签名 | 作用 | 调用时机 |
|---|---|---|---|
| `open` | `void open(op)` | 初始化算子状态（重置游标、打开文件等） | 开始执行前调用一次 |
| `next` | `bool next(op, out)` | 取下一行放入 `out`，返回 true；无数据返回 false | 反复调用直到返回 false |
| `close` | `void close(op)` | 释放资源（关闭文件、释放缓冲区等） | 全部取完后调用一次 |

`next` 返回 `false` 表示"我没有更多数据了"，这就像读文件读到 EOF。

### 9.2.3 拉模式（Pull）vs 推模式（Push）

数据流动有两种风格：

**拉模式（Pull，Volcano 采用）**：父算子主动调用子算子的 `next()`，把数据"拉"上来。

```
Project.next() ──调用──► Filter.next() ──调用──► SeqScan.next()
       ◄──返回 row──            ◄──返回 row──            ◄──读磁盘──
```

**推模式（Push）**：子算子主动把数据"推"给父算子的 `consume(row)` 方法。

```
SeqScan 产生 row ──consume──► Filter.consume(row) ──consume──► Project.consume(row)
```

| 维度 | 拉模式 (Pull) | 推模式 (Push) |
|---|---|---|
| 控制流方向 | 自顶向下（父调子） | 自底向上（子调父） |
| 数据流方向 | 自底向上（子返父） | 自底向上（子推父） |
| 实现难度 | 简单，递归调用 | 需要回调/协程 |
| 提前终止 | 容易（父不调 next 即可） | 较难（需中断传播） |
| 典型代表 | PostgreSQL、SQLite、miniDB | HyPer、Datafusion |

### 9.2.4 为什么 miniDB 选择拉模式？

1. **教学经典**：拉模式是数据库教科书的标准讲法，概念清晰。
2. **代码简单**：每个算子就是一个 `switch case`，递归调用，不需要回调机制。
3. **提前终止容易**：`LIMIT 10` 只要不调第 11 次 `next` 就行，无需特殊处理。
4. **与主流一致**：PostgreSQL、SQLite 都用拉模式，学完 miniDB 能直接看懂它们。

### 9.2.5 open/next/close 的状态机视角

每个算子实例有一个隐含的状态机：

```
            open()
  未初始化 ─────────► 已打开
                          │
                          │ next() 返回 true
                          ▼
                       有数据 ──┐
                          ▲     │ next() 再调
                          └─────┘
                          │
                          │ next() 返回 false
                          ▼
                       已耗尽
                          │
                          │ close()
                          ▼
                       已关闭
```

在 miniDB 中，`opened` 字段记录"是否已打开"，`pos` 字段记录"扫描到第几行"。

---

## 9.3 迭代器模式

### 9.3.1 从设计模式角度理解

Volcano 模型本质上是经典的**迭代器模式（Iterator Pattern）**。在 GoF 23 种设计模式中，迭代器模式定义为：

> 提供一种方法顺序访问一个聚合对象中的各个元素，而又不暴露该对象的内部表示。

数据库里的"聚合对象"就是**查询的中间结果**，"各个元素"就是**一行行数据**。

### 9.3.2 为什么数据库特别适合迭代器？

考虑一个普通程序遍历数组：

```c
for (int i = 0; i < n; i++)
    process(arr[i]);
```

数据库查询和普通循环的对应关系：

| 普通循环 | 数据库查询 |
|---|---|
| `arr` | 表（Table） |
| `i < n` | `SeqScan.next()` 返回 true |
| `arr[i]` | `next` 输出的 `out` 行 |
| `process()` | 父算子对行的处理 |
| `if (cond) ...` | `Filter` 算子 |
| `transform(arr[i])` | `Project` 算子 |

数据库用迭代器的好处：

1. **统一接口**：不管数据来自内存数组、磁盘文件、网络流，都是 `next()` 取一行。
2. **惰性求值**：不取就不算，省 CPU。`SELECT * FROM big_table LIMIT 1` 只取一行就停。
3. **可组合**：任意算子组合都是一棵树，接口一致，无需为每种组合写专门代码。

### 9.3.3 迭代器模式 vs 访问者模式（Visitor）

新手常把这两个模式搞混。对比一下：

| 维度 | 迭代器模式 (Iterator) | 访问者模式 (Visitor) |
|---|---|---|
| 谁主动 | 客户端主动调 `next()` | 元素主动调 `accept(visitor)` |
| 数据流向 | 拉数据 | 推数据给 visitor |
| 典型场景 | 遍历集合 | 对 AST 做多种操作 |
| 在数据库中 | **执行引擎用这个** | **优化器遍历计划树用这个** |

有趣的是，miniDB 内部两种模式都用了：
- **执行引擎**用迭代器模式（`executor_next` 反复调用）
- **优化器**遍历计划树时类似访问者（递归处理每个节点）

### 9.3.4 一个最小迭代器示例

为了让你直观感受，这里是一个最简单的迭代器（遍历整数数组）：

```c
typedef struct {
    int *data;
    int len;
    int pos;       // 当前位置
} IntIterator;

void iter_open(IntIterator *it) { it->pos = 0; }

bool iter_next(IntIterator *it, int *out) {
    if (it->pos >= it->len) return false;
    *out = it->data[it->pos++];
    return true;
}

void iter_close(IntIterator *it) { /* 无资源要释放 */ }
```

使用：

```c
int arr[] = {10, 20, 30};
IntIterator it = {arr, 3, 0};
iter_open(&it);
int x;
while (iter_next(&it, &x))
    printf("%d\n", x);
iter_close(&it);
```

miniDB 的 `SeqScan` 算子，本质上就是这个整数迭代器，只是把 `int` 换成了 `exec_row_t`。

---

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

---

## 9.4 数据格式

### 9.4.1 行（exec_row_t）

### 行

```c
typedef struct {
    int32_t values[EXEC_MAX_COLS];  // 列值（简化：全部 int32）
    int num_cols;
} exec_row_t;
```

逐字段解释：

| 字段 | 类型 | 含义 |
|---|---|---|
| `values` | `int32_t[16]` | 最多 16 列的值，全部用 32 位整数存储 |
| `num_cols` | `int` | 这一行实际有几列 |

**为什么全部用 int32？** 这是教学简化。真实数据库一行里可能有 int、float、字符串、日期、NULL 等多种类型，需要更复杂的编码（如 PostgreSQL 的 `Datum` + `typlen`）。miniDB 统一用 int32，让初学者把注意力放在执行流程而非类型系统上。

**为什么最多 16 列？** `EXEC_MAX_COLS = 16` 是一个编译期常量，定长数组比变长数组（`malloc`）更简单、更快。16 对教学足够。

### 9.4.2 列元数据（exec_col_meta_t）

```c
typedef struct {
    char name[EXEC_MAX_NAME];   // 列名，最多 32 字符
} exec_col_meta_t;
```

教学版只存列名。真实数据库的列元数据还包括：类型、是否可空、是否主键、默认值、约束等。

### 9.4.3 表（exec_table_t）

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

| 字段 | 含义 |
|---|---|
| `name` | 表名，如 `"users"` |
| `cols` | 列描述数组 |
| `num_cols` | 列数 |
| `rows` | 指向行数组的指针 |
| `num_rows` | 行数 |

> **简化说明**：教学版用内存数组代替磁盘读取。生产系统会从 Buffer Pool → Heap → Tuple 逐层读取。

### 9.4.4 内存布局图

假设 `users` 表有 3 列（id, age, score）、5 行，内存布局如下：

```
exec_table_t users
┌──────────────────────────────────────────┐
│ name = "users"                            │
│ num_cols = 3                              │
│ cols = [ {"id"}, {"age"}, {"score"} ]     │
│ rows ─────────────────────────┐           │
│ num_rows = 5                  │           │
└───────────────────────────────┼───────────┘
                                 ▼
   ┌────────────────────────────────────────┐
   │ rows[0]: values=[1, 25, 85]  num_cols=3│
   │ rows[1]: values=[2, 30, 90]  num_cols=3│
   │ rows[2]: values=[3, 35, 75]  num_cols=3│
   │ rows[3]: values=[4, 28, 95]  num_cols=3│
   │ rows[4]: values=[5, 40, 60]  num_cols=3│
   └────────────────────────────────────────┘
```

`SeqScan` 算子就是拿着 `pos` 从 `rows[0]` 一路扫到 `rows[4]`。

---

## 9.5 各算子详解

miniDB 目前实现了三个算子，下面逐一讲解。

### 9.5.1 SeqScan（顺序扫描）

**职责**：从头到尾扫描一张表的所有行，一次返回一行。

**状态**：只需一个 `pos` 游标，记录扫到第几行。

**工作流程**：

```
open:  pos = 0
next:  if pos >= num_rows: return false
       out = rows[pos]
       pos++
       return true
close: 无操作
```

### 算子实现

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

**逐步演示**（表有 5 行）：

| 调用 | pos 变化 | 返回 | 说明 |
|---|---|---|---|
| `open()` | 0→0 | — | 初始化 |
| `next()` | 0→1 | true, rows[0] | 返回第 1 行 |
| `next()` | 1→2 | true, rows[1] | 返回第 2 行 |
| `next()` | 2→3 | true, rows[2] | 返回第 3 行 |
| `next()` | 3→4 | true, rows[3] | 返回第 4 行 |
| `next()` | 4→5 | true, rows[4] | 返回第 5 行 |
| `next()` | 5→5 | false | 扫完，EOF |

**IndexScan 的区别**：教学版里 IndexScan 和 SeqScan 代码完全一样（都是全表扫）。真实 IndexScan 会利用 B+ 树索引，只扫描满足条件的行，跳过大量无关数据。miniDB 在优化器阶段选择了 IndexScan 计划，但执行器还没实现真正的索引访问逻辑——这是留给读者的扩展点。

### 9.5.2 Filter（过滤）

**职责**：从子算子拉取每一行，只输出满足条件的行。

**状态**：无需额外状态，它只是子算子的"消费者"。

**工作流程**：

```
open:  递归 open 子算子
next:  while (子算子.next(row)):
           if 满足条件(row):
               out = row
               return true
       return false
close: 递归 close 子算子
```

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

**逐步演示**（条件 `age > 28`，子算子是 SeqScan(users)）：

| Filter.next() 内部步骤 | 子算子返回 | age | age>28? | Filter 行为 |
|---|---|---|---|---|
| 调子算子 next | (1,25,85) | 25 | 否 | 继续循环 |
| 调子算子 next | (2,30,90) | 30 | 是 | 输出 (2,30,90)，返回 true |
| （下次 next）调子算子 next | (3,35,75) | 35 | 是 | 输出 (3,35,75)，返回 true |
| （下次 next）调子算子 next | (4,28,95) | 28 | 否 | 继续循环 |
| 调子算子 next | (5,40,60) | 40 | 是 | 输出 (5,40,60)，返回 true |
| （下次 next）调子算子 next | false (EOF) | — | — | 返回 false |

注意：Filter 一次 `next()` 可能调用子算子**多次** `next()`（跳过不满足的行）。这是"消费者"的典型行为。

### 9.5.3 Project（投影）

**职责**：从子算子取一行，只保留查询需要的列。

**状态**：`proj_indices[]` 记录"输出第 i 列对应输入第几列"。

**工作流程**：

```
open:  递归 open 子算子
next:  if not 子算子.next(row): return false
       for i in 0..proj_num_cols:
           out.values[i] = row.values[proj_indices[i]]
       out.num_cols = proj_num_cols
       return true
close: 递归 close 子算子
```

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

**逐步演示**（`SELECT id FROM users`，即只输出第 0 列）：

假设子算子返回 `(2, 30, 90)`：

| 步骤 | 操作 | 结果 |
|---|---|---|
| 1 | 调子算子 next | row = (2,30,90) |
| 2 | out.num_cols = 1 | 输出列数=1 |
| 3 | out.values[0] = row.values[proj_indices[0]] | proj_indices[0]=0, 所以 out.values[0]=2 |
| 4 | 返回 true | out = (2) |

**SELECT \* 的特殊处理**：当 `proj_num_cols == -1` 时，表示"输出所有列"，直接把子算子的行原样传出：

```c
if (op->proj_num_cols == -1) {
    *out = row;   // 原样输出
}
```

### 9.5.4 三个算子对比总结

| 算子 | 输入 | 输出 | 调子算子次数 | 核心逻辑 |
|---|---|---|---|---|
| SeqScan | 表 | 全部行 | 0（它是叶子） | pos++ |
| Filter | 子算子的行 | 满足条件的行 | ≥1（跳过不满足的） | eval_predicate |
| Project | 子算子的行 | 选列后的行 | 1（一对一） | 列重排 |

---

## 9.6 表达式求值

### 9.6.1 谓词的结构

WHERE 子句的条件在 AST 里表示为 `ast_expr_t`，包含三部分：

| 部分 | 字段 | 示例（`age > 28`） |
|---|---|---|
| 列名 | `pred->column` | `"age"` |
| 运算符 | `pred->op` | `">"` |
| 比较值 | `pred->value` | `28`（int_val=28） |

### 9.6.2 求值函数

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

实际代码（executor.c:20-42）比上面多了类型转换和边界检查：

```c
static bool eval_predicate(const ast_expr_t *pred, const exec_row_t *row,
                           const exec_table_t *table) {
    int idx = find_col_idx(table, pred->column);
    if (idx < 0 || idx >= row->num_cols) return false;   // 列不存在

    int32_t v = row->values[idx];
    int32_t target;

    if (pred->value.type == VAL_INT)                     // 整数比较值
        target = pred->value.int_val;
    else if (pred->value.type == VAL_FLOAT)              // 浮点比较值：转 int
        target = (int32_t)pred->value.float_val;
    else
        return false;                                    // 字符串等暂不支持

    if (strcmp(pred->op, "=") == 0)  return v == target;
    if (strcmp(pred->op, "!=") == 0) return v != target;
    if (strcmp(pred->op, "<") == 0)  return v <  target;
    if (strcmp(pred->op, ">") == 0)  return v >  target;
    if (strcmp(pred->op, "<=") == 0) return v <= target;
    if (strcmp(pred->op, ">=") == 0) return v >= target;
    return false;
}
```

### 9.6.3 六种比较运算符

| 运算符 | C 表达式 | 示例 | 示例结果 |
|---|---|---|---|
| `=` | `v == target` | `age = 30`（age=30） | true |
| `!=` | `v != target` | `age != 30`（age=25） | true |
| `<` | `v < target` | `age < 30`（age=25） | true |
| `>` | `v > target` | `age > 30`（age=25） | false |
| `<=` | `v <= target` | `age <= 30`（age=30） | true |
| `>=` | `v >= target` | `age >= 30`（age=25） | false |

### 9.6.4 类型转换

miniDB 的值统一存为 `int32_t`，但 SQL 里可以写 `age > 28.5`（浮点）。处理方式：

```c
if (pred->value.type == VAL_FLOAT)
    target = (int32_t)pred->value.float_val;   // 截断为 int
```

这是**教学简化**（直接截断小数）。真实数据库会：
1. 把两边提升到同一类型（int + float → float + float）
2. 用对应类型的比较函数
3. 处理精度损失

### 9.6.5 NULL 处理（当前未实现）

SQL 标准里 NULL 有特殊的三值逻辑：

| 表达式 | SQL 结果 |
|---|---|
| `NULL > 28` | UNKNOWN（不是 true 也不是 false） |
| `NULL = NULL` | UNKNOWN（不是 true！） |
| `WHERE NULL > 28` | 行被过滤掉（UNKNOWN 当 false 处理） |

miniDB 目前**没有 NULL 概念**，所有值都是有效 int32。这是教学简化。真实数据库每个列值还要带一个 `is_null` 标志位，比较时先判 NULL。

### 9.6.6 多条件 AND

miniDB 支持多个 WHERE 条件用 AND 连接，如 `WHERE age > 25 AND score < 90`。实现方式是**串联多个 Filter 算子**：

```
Project
  └─ Filter(score < 90)
       └─ Filter(age > 25)
            └─ SeqScan(users)
```

每个 Filter 只判断一个条件，数据要同时通过所有 Filter 才能到达 Project。这叫"谓词下推 + 串联"，是优化器的标准做法。

---

## 9.7 数据流

本节用一组逐步细化的图，展示一行数据如何从底层 SeqScan 流到顶层 Project。

### 9.7.1 查询与数据

```
SQL: SELECT id FROM users WHERE age > 28

users 表（5 行 3 列）:
  id | age | score
   1 |  25 |   85
   2 |  30 |   90
   3 |  35 |   75
   4 |  28 |   95
   5 |  40 |   60
```

### 9.7.2 计划树与算子树

```
计划树:                        算子树:
Project(id)                    Project(proj_indices=[0])
  └─ Filter(age > 28)    →       └─ Filter(pred={col="age", op=">", val=28})
       └─ SeqScan(users)              └─ SeqScan(table=&users, pos=0)
```

`executor_build` 把计划树转成算子树，区别是：算子树里的指针指向**真实的数据表**和**预解析的列索引**。

### 9.7.3 第一次 next() 调用

```
Project.next()
  │
  │ 调用 Filter.next()
  ▼
Filter.next()
  │
  │ 循环调用 SeqScan.next():
  │   第1次: SeqScan.next() → (1,25,85), pos=1
  │          eval(age>28): 25>28? NO → 继续循环
  │   第2次: SeqScan.next() → (2,30,90), pos=2
  │          eval(age>28): 30>28? YES → 返回 (2,30,90)
  ▼
返回 row=(2,30,90) 给 Project
  │
  │ Project 选列: out.values[0] = row.values[0] = 2
  ▼
输出: (2)
```

### 9.7.4 第二次 next() 调用

```
Project.next()
  │
  ▼
Filter.next()
  │
  │   SeqScan.next() → (3,35,75), pos=3
  │   eval(age>28): 35>28? YES → 返回
  ▼
返回 row=(3,35,75)
  │
  │ Project 选列: out = (3)
  ▼
输出: (3)
```

### 9.7.5 第三次 next() 调用

```
Project.next()
  │
  ▼
Filter.next()
  │
  │   SeqScan.next() → (4,28,95), pos=4
  │   eval(age>28): 28>28? NO → 继续
  │   SeqScan.next() → (5,40,60), pos=5
  │   eval(age>28): 40>28? YES → 返回
  ▼
返回 row=(5,40,60)
  │
  │ Project 选列: out = (5)
  ▼
输出: (5)
```

### 9.7.6 第四次 next() 调用（EOF）

```
Project.next()
  │
  ▼
Filter.next()
  │
  │   SeqScan.next() → pos=5 >= num_rows=5 → false (EOF)
  │   循环结束
  ▼
返回 false
  │
  ▼
Project 返回 false → 执行结束
```

### 9.7.7 完整数据流汇总

```
SeqScan 游标:  0 → 1 → 2 → 3 → 4 → 5(EOF)
                 │    │    │    │    │
                 ▼    ▼    ▼    ▼    ▼
原始行:        r0   r1   r2   r3   r4
              (1,25,85) (2,30,90) (3,35,75) (4,28,95) (5,40,60)
                 │    │    │    │    │
                 ▼    ▼    ▼    ▼    ▼
Filter(age>28): 跳过  通过  通过  跳过  通过
                      │    │          │
                      ▼    ▼          ▼
过滤后:           (2,30,90) (3,35,75) (5,40,60)
                      │    │          │
                      ▼    ▼          ▼
Project(id):         (2)   (3)        (5)
                      │    │          │
                      ▼    ▼          ▼
最终结果:            [(2), (3), (5)]
```

### 9.7.8 调用次数统计

| 算子 | next() 被调用次数 | 内部调子算子 next() 次数 |
|---|---|---|
| Project | 4（3 次成功 + 1 次 EOF） | 4 |
| Filter | 4 | 5（扫完整个表） |
| SeqScan | 5 | 0（叶子） |

注意 Filter 被调用 4 次，但它内部调了 SeqScan 5 次——因为有些调用里它连续拉了多行才找到一个满足条件的。

---

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

---

## 9.8 向量化执行

### 9.8.1 Volcano 模型为什么慢？

Volcano 每次只处理一行，每行都要经过一长串函数调用。看 9.7.8 节的统计：处理 5 行数据，next() 被调用了 13 次。如果表有 100 万行、算子树有 5 层，那就是 500 万次函数调用。

**性能瓶颈**：

| 问题 | 说明 |
|---|---|
| 函数调用开销 | 每次 next() 都有调用、返回、参数传递的开销 |
| 虚函数/分支预测 | switch-case 分支难以预测，CPU 流水线效率低 |
| 无法用 SIMD | 一行一个值，无法用 CPU 向量指令一次处理多个 |
| 缓存不友好 | 跨算子跳来跳去，局部性差 |

### 9.8.2 向量化模型的核心思想

**向量化**：每次 next() 不返回一行，而返回**一批行（batch，通常 1024 或 4096 行）**。

```
Volcano（一次一行）:          向量化（一次一批）:
next() → row1                 next() → [row1, row2, ..., row1024]
next() → row2                 next() → [row1025, ..., row2048]
next() → row3                 ...
...                           next() → false
next() → false
```

### 9.8.3 向量化为什么快？

| 优化点 | Volcano | 向量化 |
|---|---|---|
| 函数调用次数 | N 行 × M 层算子 | N/1024 批 × M 层算子（少 1024 倍） |
| SIMD | 无法用 | 可用 AVX2 一次比 8 个 int |
| 分支预测 | 每行都分支 | 一批只分支一次 |
| 缓存局部性 | 跨算子跳 | 一个算子处理完一批再传下去 |

### 9.8.4 向量化 Filter 示意

Volcano 版 Filter（一次一行）：

```c
bool next(row) {
    while (child.next(row))
        if (row.age > 28) return true;
    return false;
}
```

向量化版 Filter（一次一批）：

```c
bool next(batch) {              // batch 有 1024 行
    child.next(batch);          // 一次拉一批
    int out_cnt = 0;
    for (int i = 0; i < batch.size; i++)
        if (batch.rows[i].age > 28)            // 连续比较，SIMD 友好
            batch.out[out_cnt++] = batch.rows[i];
    batch.size = out_cnt;
    return out_cnt > 0;
}
```

内层 for 循环对连续内存做相同比较，编译器可以自动用 SIMD 指令优化。

### 9.8.5 ClickHouse 的实践

ClickHouse 是向量化执行的标杆：

| 特性 | ClickHouse 做法 |
|---|---|
| 批大小 | 通常 65536 行（一个 Block） |
| 列存 | 每批按列组织（ColumnBlock），不是行存 |
| SIMD | 大量手写 AVX2/AVX-512 汇编 |
| 性能 | 分析查询比 PostgreSQL 快 10-100 倍 |

### 9.8.6 PostgreSQL 的混合策略

PostgreSQL 传统上是 Volcano，但 11+ 版本开始引入向量化（通过扩展如 pg_vectorize、Citus）。它的执行器仍然是逐行的，但：

- **批量读**：SeqScan 一次读一个 page（8KB，约几百行）到缓冲池
- **JIT 编译**：11+ 版本用 LLVM 把算子树编译成机器码，消除函数调用开销

### 9.8.7 miniDB 为什么不用向量化？

1. **教学目标**：理解执行流程比追求性能重要
2. **代码复杂度**：向量化要处理批的填充、排空、部分批，代码量大 3 倍
3. **数据量小**：教学表就几行，向量化优势体现不出来

理解了 Volcano，再学向量化只是"把 next() 的返回值从一行变成一批"，概念上很自然。

---

## 9.9 代码逐行解读

本节逐段解读 `executor.h` 和 `executor.c` 的关键部分。

### 9.9.1 executor.h — 数据结构

```c
#define EXEC_MAX_COLS 16       // 每行最多 16 列
#define EXEC_MAX_NAME 32       // 名字最多 32 字符
#define EXEC_MAX_ROWS 1024     // 结果集最多 1024 行
```

三个常量定义了系统的容量上限。教学版用定长数组避免动态内存管理。

```c
typedef struct {
    int32_t values[EXEC_MAX_COLS];   // 列值数组
    int num_cols;                    // 实际列数
} exec_row_t;
```

一行数据。定长数组 `values` 存所有列的值，`num_cols` 记录实际用了几列。

```c
struct operator {
    plan_type_t type;               // 算子类型（SCAN/FILTER/PROJECT）
    operator_t *left;               // 左子算子
    operator_t *right;              // 右子算子（当前未用，预留给 Join）

    bool opened;                    // 是否已 open
    int pos;                        // 扫描游标（SeqScan 用）

    const exec_table_t *table;      // 指向表（Scan 用）
    ast_expr_t predicate;           // 谓词（Filter 用）
    bool has_predicate;             // 是否有条件

    int proj_indices[EXEC_MAX_COLS]; // 输出列→输入列的映射（Project 用）
    int proj_num_cols;               // 输出列数

    exec_row_t current;             // 当前行（当前版本未使用）
};
```

注意：一个 `operator_t` 结构体包含了**所有算子**需要的字段。SeqScan 只用 `table` 和 `pos`，Filter 只用 `predicate` 和 `left`，Project 只用 `proj_indices` 和 `left`。这是 C 语言里常见的"联合体式"写法（没用 union 但效果类似），简单但浪费一点内存。

### 9.9.2 executor.c — 辅助函数

**find_table**（第 6-11 行）：按名字在表数组里找表。

```c
static const exec_table_t *find_table(exec_table_t *tables, int n, const char *name) {
    for (int i = 0; i < n; i++)
        if (strcmp(tables[i].name, name) == 0)
            return &tables[i];
    return NULL;
}
```

线性查找，O(n)。表数量少，够用。

**find_col_idx**（第 13-18 行）：按名字在表里找列索引。

```c
static int find_col_idx(const exec_table_t *t, const char *name) {
    for (int i = 0; i < t->num_cols; i++)
        if (strcmp(t->cols[i].name, name) == 0)
            return i;
    return -1;
}
```

返回列在 `values[]` 数组里的下标，找不到返回 -1。

### 9.9.3 executor_build — 构建算子树

```c
operator_t *executor_build(const plan_node_t *plan,
                           exec_table_t *tables, int num_tables) {
    if (!plan) return NULL;

    operator_t *op = calloc(1, sizeof(operator_t));   // 1. 分配并清零
    op->type = plan->type;                            // 2. 复制类型
```

`calloc` 会把所有字段清零，所以 `opened=false`、`pos=0` 等初始状态自动正确。

```c
    switch (plan->type) {
        case PLAN_SEQ_SCAN:
        case PLAN_INDEX_SCAN:
            op->table = find_table(tables, num_tables, plan->table_name);
            break;
```

Scan 算子：根据计划里的表名，找到真实的 `exec_table_t` 指针。

```c
        case PLAN_FILTER:
            op->predicate = plan->predicate;
            op->has_predicate = plan->has_predicate;
            break;
```

Filter 算子：复制谓词。

```c
        case PLAN_PROJECT:
            if (plan->select_all) {
                op->proj_num_cols = -1;               // SELECT * 标记
            } else {
                op->proj_num_cols = plan->num_cols;
                // 向下找到 Scan 节点，拿到表，解析列名→列索引
                const plan_node_t *scan = plan->left;
                while (scan && scan->type != PLAN_SEQ_SCAN &&
                       scan->type != PLAN_INDEX_SCAN)
                    scan = scan->left;
                if (scan && tables && num_tables > 0) {
                    const exec_table_t *t = find_table(tables, num_tables,
                                                        scan->table_name);
                    if (t) {
                        for (int i = 0; i < plan->num_cols; i++)
                            op->proj_indices[i] = find_col_idx(t, plan->columns[i]);
                    }
                }
            }
            break;
```

Project 算子最复杂：要把"列名"转成"列索引"。因为执行时只有行数据（`exec_row_t`），没有列名信息，必须预先把 `columns[i]`（如 `"id"`）转成 `proj_indices[i]`（如 `0`）。

```c
    op->left = executor_build(plan->left, tables, num_tables);    // 递归构建左子树
    op->right = executor_build(plan->right, tables, num_tables);  // 递归构建右子树
    return op;
}
```

最后递归构建子算子。整个函数就是把计划树"实例化"成算子树。

### 9.9.4 executor_open — 初始化

```c
void executor_open(operator_t *op) {
    if (!op || op->opened) return;     // 防重复 open
    op->opened = true;
    op->pos = 0;                       // 重置游标
    if (op->left)  executor_open(op->left);    // 递归 open 子算子
    if (op->right) executor_open(op->right);
}
```

先序遍历，自顶向下 open。`pos = 0` 对 SeqScan 有意义，对其他算子无害。

### 9.9.5 executor_next — 核心逻辑

```c
bool executor_next(operator_t *op, exec_row_t *out) {
    if (!op || !op->opened) return false;     // 安全检查

    switch (op->type) {
        case PLAN_SEQ_SCAN:
        case PLAN_INDEX_SCAN: {
            if (!op->table) return false;
            if (op->pos >= op->table->num_rows) return false;   // 扫完
            *out = op->table->rows[op->pos++];                  // 取行并前进
            return true;
        }
```

SeqScan：检查游标是否越界，不越界就取一行、游标前进。

```c
        case PLAN_FILTER: {
            exec_row_t row;
            while (executor_next(op->left, &row)) {            // 从子算子拉
                const exec_table_t *t = NULL;
                operator_t *scan = op->left;
                while (scan && scan->type == PLAN_FILTER)      // 找到 Scan 节点
                    scan = scan->left;
                if (scan) t = scan->table;                     // 拿到表（用于查列索引）
                if (!op->has_predicate ||
                    eval_predicate(&op->predicate, &row, t)) {
                    *out = row;
                    return true;                               // 满足条件，返回
                }
            }
            return false;                                      // 子算子耗尽
        }
```

Filter：循环拉子算子的行，找到满足条件的就返回。注意它要向下找到 Scan 节点来拿表元数据（因为 `eval_predicate` 需要表来查列索引）。

```c
        case PLAN_PROJECT: {
            exec_row_t row;
            if (!executor_next(op->left, &row)) return false;  // 拉一行
            if (op->proj_num_cols == -1) {
                *out = row;                                    // SELECT *
            } else {
                out->num_cols = op->proj_num_cols;
                for (int i = 0; i < op->proj_num_cols; i++) {
                    int idx = op->proj_indices[i];
                    out->values[i] = (idx >= 0 && idx < row.num_cols)
                                   ? row.values[idx] : 0;      // 按索引选列
                }
            }
            return true;
        }
```

Project：拉一行，按 `proj_indices` 重排列。`idx` 越界时填 0（防御性编程）。

### 9.9.6 executor_close / executor_destroy

```c
void executor_close(operator_t *op) {
    if (!op) return;
    if (op->left)  executor_close(op->left);    // 后序关闭
    if (op->right) executor_close(op->right);
    op->opened = false;
}

void executor_destroy(operator_t *op) {
    if (!op) return;
    executor_destroy(op->left);                 // 后序释放
    executor_destroy(op->right);
    free(op);
}
```

`close` 是逻辑关闭（可重新 open），`destroy` 是物理释放（内存回收）。两者都是后序遍历：先处理子节点再处理自己。

### 9.9.7 executor_run — 顶层入口

```c
result_set_t *executor_run(const plan_node_t *plan,
                           exec_table_t *tables, int num_tables) {
    operator_t *op = executor_build(plan, tables, num_tables);   // 1. 建算子树
    if (!op) return NULL;

    result_set_t *rs = calloc(1, sizeof(result_set_t));

    executor_open(op);                                           // 2. 初始化

    exec_row_t row;
    while (executor_next(op, &row) && rs->num_rows < EXEC_MAX_ROWS) {
        rs->rows[rs->num_rows++] = row;                          // 3. 收集结果
    }
```

注意循环条件有两个：`executor_next` 返回 true **且** `num_rows < EXEC_MAX_ROWS`。后者是结果集大小上限保护。

```c
    // 4. 填充结果集的列元数据
    if (op->type == PLAN_PROJECT && op->proj_num_cols > 0) {
        rs->num_cols = op->proj_num_cols;
        // ... 找到 Scan 节点，按 proj_indices 取列名
    } else {
        // SELECT * 的情况：直接用 Scan 节点的所有列
    }

    executor_close(op);                                          // 5. 关闭
    executor_destroy(op);                                        // 6. 释放

    return rs;
}
```

整个流程：build → open → 循环 next → 填列名 → close → destroy → 返回结果集。

---

## 9.10 内存管理

### 9.10.1 结果集收集

```c
while (executor_next(op, &row) && rs->num_rows < EXEC_MAX_ROWS) {
    rs->rows[rs->num_rows++] = row;          // 逐行拷贝到结果集
}
```

每拉到一行，就拷贝到 `rs->rows` 数组里。`result_set_t` 定义：

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

### 9.10.2 内存分配策略

miniDB 的内存分配分三类：

| 对象 | 分配方式 | 释放时机 | 谁负责 |
|---|---|---|---|
| 算子树 (`operator_t`) | `calloc` | `executor_destroy` | 执行器 |
| 结果集 (`result_set_t`) | `calloc` | `result_set_destroy` | 调用方 |
| 表数据 (`exec_table_t`) | 调用方准备 | 调用方管理 | 调用方 |

**关键原则**：谁分配谁释放。执行器分配算子树，执行器释放；调用方分配表数据，调用方释放。

### 9.10.3 为什么限制结果集大小？

```c
#define EXEC_MAX_ROWS 1024

while (executor_next(op, &row) && rs->num_rows < EXEC_MAX_ROWS) {
```

三个原因：

| 原因 | 说明 |
|---|---|
| **定长数组** | `rows[1024]` 是定长的，超过会数组越界崩溃 |
| **教学简化** | 避免引入 `realloc` 动态扩容逻辑 |
| **真实数据库也有限** | PostgreSQL 有 `LIMIT`，MySQL 有 `max_result_size`，防止 `SELECT * FROM huge_table` 拖垮内存 |

真实数据库的做法：
- **流式返回**：不收集全部结果，边算边通过网络发给客户端
- **游标（Cursor）**：客户端按需 fetch，每次取一批
- **溢出到磁盘**：结果太大时写临时文件

miniDB 的 1024 行上限，相当于隐含了一个 `LIMIT 1024`。

### 9.10.4 内存布局总览

```
栈上                          堆上
┌──────────────┐              ┌─────────────────────┐
│ executor_run │              │ result_set_t        │
│  rs ───────────────────────►│  rows[1024]         │
│  op ───────────────────────►│  num_rows, cols...  │
└──────────────┘              └─────────────────────┘
                              ┌─────────────────────┐
                              │ operator_t (Project)│
                              │  left ──────────────┼──┐
                              └─────────────────────┘  │
                              ┌─────────────────────┐  │
                              │ operator_t (Filter) │◄─┘
                              │  left ──────────────┼──┐
                              └─────────────────────┘  │
                              ┌─────────────────────┐  │
                              │ operator_t (SeqScan)│◄─┘
                              │  table ─────────────┼──┐
                              └─────────────────────┘  │
                                                        │
                              表数据（调用方管理）◄──────┘
                              ┌─────────────────────┐
                              │ exec_table_t users  │
                              │  rows ──────────────┼──► 行数组
                              └─────────────────────┘
```

---

## 9.11 与真实数据库对比

### 9.11.1 三种执行模型对比

| 维度 | miniDB | SQLite | PostgreSQL | ClickHouse |
|---|---|---|---|---|
| 执行模型 | Volcano | 字节码 VM | Volcano + JIT | 向量化 |
| 一次处理 | 1 行 | 1 行（VM 指令） | 1 行 | 1 批（65536 行） |
| 数据类型 | int32 | 多类型 | 多类型 | 多类型+列存 |
| 函数调用 | 递归 C 调用 | VM 指令分发 | 递归 C 调用 | 批内循环 |
| 并行 | 单线程 | 单线程 | 多线程 | 多线程+向量化 |

### 9.11.2 SQLite 的字节码 VM

SQLite 不用 Volcano，而是把 SQL 编译成**字节码**，在一个虚拟机上执行。类似 Java JVM 的思路。

```
SQL: SELECT id FROM users WHERE age > 28

SQLite 字节码（示意）:
  OpenRead   users       # 打开表
  Rewind                 # 游标回到开头
Loop:
  IfNoMore   Done        # 没数据就跳到 Done
  Column     age  r1     # 读 age 列到寄存器 r1
  IfLE       r1  28 Loop # if r1 <= 28 跳回 Loop
  Column     id   r2     # 读 id 列到 r2
  ResultRow  r2          # 输出 r2
  Goto       Loop        # 跳回 Loop
Done:
  Close
```

| 优点 | 缺点 |
|---|---|
| 指令级优化（常量折叠、死代码消除） | 实现复杂（要写编译器+VM） |
| 字节码紧凑，适合嵌入式 | 难以并行 |
| 调试方便（可反汇编字节码） | 不适合大表分析查询 |

### 9.11.3 PostgreSQL 的 Volcano + 向量化

PostgreSQL 传统执行器是 Volcano，和 miniDB 概念一致，但工程上复杂得多：

| 特性 | miniDB | PostgreSQL |
|---|---|---|
| 算子种类 | 3 个（Scan/Filter/Project） | 30+ 个（含 Join/Agg/Sort/Window/...） |
| 数据类型 | int32 | 几十种（int/float/text/json/uuid/...） |
| 表存储 | 内存数组 | 磁盘堆 + Buffer Pool + WAL |
| 并行 | 无 | 并行 Scan/Join/Agg |
| JIT | 无 | LLVM 编译表达式 |
| 向量化 | 无 | 逐行（社区在推进向量化） |

PostgreSQL 一个 SeqScan 的代码就有上千行，要处理：可见性检查（MVCC）、WAL 日志、缓冲池替换、并行工作线程等。miniDB 的 SeqScan 只有 3 行。

### 9.11.4 功能差距对照表

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

### 9.11.5 miniDB 的学习价值

虽然功能简单，但 miniDB 的执行引擎包含了**所有数据库执行器的核心思想**：

1. ✅ 算子树（Operator Tree）
2. ✅ 迭代器接口（open/next/close）
3. ✅ 拉模式数据流
4. ✅ 谓词求值
5. ✅ 列投影
6. ✅ 结果集收集

理解了这些，再看 PostgreSQL 的 `execProcnode.c` 或 SQLite 的 `vdbe.c`，你会发现**核心骨架完全一样**，只是它们在骨架上加了更多算子、更多类型、更多优化。

---

## 9.12 习题

### 习题 1（执行流程追踪）

给定表 `t(a, b)`，数据为 `[(1,10), (2,20), (3,30), (4,40)]`，执行 `SELECT a FROM t WHERE b > 15`：

1. 画出算子树
2. 列出每次 `next()` 调用链
3. 给出最终结果

### 习题 2（算子调用次数）

对 9.7 节的查询（5 行表，3 行满足条件），如果改成 `SELECT * FROM users WHERE age > 28`（注意是 `SELECT *`）：

1. Project 算子的 `next()` 被调用几次？
2. SeqScan 的 `next()` 被调用几次？
3. 结果集有几行？

### 习题 3（代码理解）

阅读 `executor.c` 的 Filter 实现，回答：

1. 为什么 Filter 内部要用 `while` 循环调子算子的 `next()`，而不是 `if`？
2. 如果改成 `if`，会发生什么？用一个具体例子说明。

### 习题 4（扩展实现）

miniDB 当前没有 `LIMIT n` 算子。请设计一个 `Limit` 算子：

1. 它需要哪些字段？
2. 写出它的 `next()` 伪代码
3. 它应该放在算子树的什么位置？

### 习题 5（向量化改造）

假设要把 miniDB 的 Filter 改成向量化（一次处理一批 1024 行）：

1. `exec_row_t` 需要怎么改？
2. `executor_next` 的签名需要怎么改？
3. 写出向量化 Filter 的伪代码

### 习题 6（性能分析）

对一张 100 万行的表执行 `SELECT id FROM big_table WHERE age > 28`，假设算子树有 3 层（Project→Filter→SeqScan），约 50% 的行满足条件：

1. Volcano 模型下，`next()` 总共被调用大约多少次？
2. 如果改成批大小 1024 的向量化，`next()` 大约被调用多少次？
3. 函数调用次数减少多少倍？

### 习题 7（NULL 语义）

SQL 标准中 `NULL > 28` 的结果是 UNKNOWN（不是 true 也不是 false）。如果要在 miniDB 里支持 NULL：

1. `exec_row_t` 需要加什么字段？
2. `eval_predicate` 需要怎么改？
3. 三值逻辑下，`WHERE` 子句如何处理 UNKNOWN？

### 习题 8（设计思考）

miniDB 把所有算子的字段塞在一个 `operator_t` 结构体里（SeqScan 用 `table`/`pos`，Filter 用 `predicate`，Project 用 `proj_indices`）。这种设计的优缺点是什么？如果用 C 的 `union` 会怎样？如果用 C++ 的继承会怎样？

---

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

### 测试数据初始化

所有测试共用一个 `users` 表（5 行 3 列）：

```c
static void init_users_table(void) {
    strcpy(users_table.name, "users");
    users_table.num_cols = 3;
    strcpy(users_table.cols[0].name, "id");
    strcpy(users_table.cols[1].name, "age");
    strcpy(users_table.cols[2].name, "score");

    users_rows[0].values[0] = 1; users_rows[0].values[1] = 25; users_rows[0].values[2] = 85;
    users_rows[1].values[0] = 2; users_rows[1].values[1] = 30; users_rows[1].values[2] = 90;
    users_rows[2].values[0] = 3; users_rows[2].values[1] = 35; users_rows[2].values[2] = 75;
    users_rows[3].values[0] = 4; users_rows[3].values[1] = 28; users_rows[3].values[2] = 95;
    users_rows[4].values[0] = 5; users_rows[4].values[1] = 40; users_rows[4].values[2] = 60;
}
```

### 典型测试结构

每个测试都遵循"准备数据 → 解析 SQL → 优化 → 执行 → 断言 → 清理"的流程：

```c
void test_filter_equal(void) {
    init_users_table();                              // 1. 准备数据
    catalog_t *c = make_catalog();
    exec_table_t tables[] = {users_table};

    parser_t *p = parser_create("SELECT * FROM users WHERE id = 3");
    ast_stmt_t *s = parser_parse(p);                 // 2. 解析
    plan_node_t *plan = optimizer_optimize(s, c);    // 3. 优化
    result_set_t *rs = executor_run(plan, tables, 1);// 4. 执行

    TEST_ASSERT_NOT_NULL(rs);                        // 5. 断言
    TEST_ASSERT_EQUAL_INT(1, rs->num_rows);
    TEST_ASSERT_EQUAL_INT32(3, rs->rows[0].values[0]);

    plan_destroy(plan);                              // 6. 清理
    free(s);
    parser_destroy(p);
    result_set_destroy(rs);
    catalog_destroy(c);
}
```

---

## 9.13 关键概念速查表

| 概念 | 一句话解释 | 代码位置 |
|---|---|---|
| Volcano 模型 | 每个算子是迭代器，父调子 next() 拉数据 | executor.c:97 |
| 拉模式 | 父算子主动调子算子，数据被"拉"上来 | executor.c:110 |
| open/next/close | 迭代器三件套：初始化/取行/释放 | executor.c:89/97/144 |
| SeqScan | 顺序扫描，pos 游标逐行返回 | executor.c:101-107 |
| Filter | 消费者，跳过不满足条件的行 | executor.c:108-123 |
| Project | 选列，按 proj_indices 重排 | executor.c:124-138 |
| 谓词求值 | 取列值、取比较值、switch 六种运算符 | executor.c:20-42 |
| 结果集 | 定长数组收集最多 1024 行 | executor.c:168 |
| 算子树构建 | 递归把 plan_node_t 转成 operator_t | executor.c:44-87 |

---

## 下一步

执行引擎就绪后，下一章实现**网络协议与 CLI**：让用户通过命令行输入 SQL，实时查看执行结果。
