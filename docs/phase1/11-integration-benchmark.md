# 章11：集成测试与压测

> 各模块单独测试通过，并不代表整体可用。本章进行端到端集成测试和性能压测，验证 `Parser → Optimizer → Executor` 全链路的正确性与吞吐量，并对阶段 1 全部 11 章做一次系统性回顾。

---

## 目录

- [1. 为什么需要集成测试](#1-为什么需要集成测试)
- [2. 测试策略](#2-测试策略)
- [3. 集成测试详解](#3-集成测试详解)
- [4. 压测方法论](#4-压测方法论)
- [5. 压测结果分析](#5-压测结果分析)
- [6. 全链路验证](#6-全链路验证)
- [7. 阶段 1 总结](#7-阶段-1-总结)
- [8. 与真实数据库对比](#8-与真实数据库对比)
- [9. 学到的教训](#9-学到的教训)
- [10. 下一步展望](#10-下一步展望)
- [11. 习题](#11-习题)

---

## 1. 为什么需要集成测试

### 1.1 一个类比

想象你在造一辆汽车。

| 阶段 | 类比 | 数据库对应 |
|---|---|---|
| 零件检验 | 轮胎气压、刹车片厚度合格 | 单元测试：每个模块单独测 |
| 总装下线 | 把零件装在一起，能点火 | 集成测试：模块拼接后能跑 |
| 路试 | 上路跑 100 公里不抛锚 | 压测：高负载下稳定 |
| 交付 | 用户能开走 | 全链路验证：SQL 进、结果出 |

**单元测试**回答的是："这个零件本身合格吗？"
**集成测试**回答的是："这些合格零件装在一起，还能正常工作吗？"

历史上无数惨痛教训告诉我们：每个零件都合格，组装起来照样可能开不动。模块之间的**接口契约**、**数据流转**、**隐式假设**，只有在拼起来之后才会暴露。

### 1.2 单元测试 vs 集成测试

| 维度 | 单元测试 | 集成测试 |
|---|---|---|
| **范围** | 单个函数 / 单个模块 | 多模块协作的完整链路 |
| **依赖** | mock 掉外部依赖 | 使用真实模块，不 mock |
| **速度** | 极快（微秒级） | 较慢（毫秒级） |
| **数量** | 多（本项目 92 个） | 少（本项目 11 个） |
| **定位 bug** | 精确到某个函数 | 只能告诉你"链路某处坏了" |
| **价值** | 防止回归 | 验证架构正确性 |
| **维护成本** | 低 | 较高 |
| **运行频率** | 每次提交 | 每次提交（CI） |
| **失败原因** | 函数逻辑错 | 接口不匹配 / 数据流断裂 |

### 1.3 本项目为什么必须有集成测试

miniDB 的查询要走完下面这条链路：

```
SQL 文本
  │
  ▼
Lexer (词法)        ← 章 7
  │ 产出 Token 流
  ▼
Parser (语法)       ← 章 7
  │  产出 AST
  ▼
Optimizer (优化)    ← 章 8
  │  产出逻辑/物理 Plan
  ▼
Executor (执行)     ← 章 9
  │  产出 ResultSet
  ▼
ResultSet (结果)
```

每一章我们都为对应模块写了单元测试：

| 章 | 模块 | 单元测试数 | 测了什么 |
|---|---|---|---|
| 7 | Lexer/Parser | 11 | Token 识别、AST 结构 |
| 8 | Optimizer | 10 | 启发式规则、代价估算 |
| 9 | Executor | 10 | SeqScan、Filter、Project |

但是，**这些单元测试彼此独立**：

- Parser 的测试只检查 AST 树形结构对不对，不关心 Optimizer 能不能吃这个 AST。
- Optimizer 的测试只检查 Plan 节点对不对，不关心 Executor 能不能执行这个 Plan。
- Executor 的测试只检查 ResultSet 行数对不对，不关心这个 Plan 是不是真的从 SQL 优化来的。

集成测试要做的事，就是把这条链路**真的串起来**，喂一句真实 SQL 进去，看最后结果对不对。如果中间任何一环的接口对不上，单元测试全绿，集成测试照样红。

### 1.4 集成测试能抓到的典型 bug

| bug 类型 | 例子 | 单元测试能抓吗 |
|---|---|---|
| 接口字段名不一致 | Parser 输出 `table_name`，Optimizer 读 `table` | 不能 |
| 内存所有权混乱 | Parser 分配 AST，Optimizer 误释放 | 不能 |
| 类型隐式转换 | Executor 期望 `int64`，Parser 给 `int32` | 不能 |
| 边界传递 | Parser 允许空 WHERE，Optimizer 崩溃 | 不能 |
| 列序假设 | Executor 假设列序 A,B，Optimizer 重排成 B,A | 不能 |
| 错误传播 | 某模块返回 NULL，下游没检查 | 不能 |

> **新手提示**：如果你写完一个模块，单元测试全过，但集成测试挂了，第一反应不应该是"集成测试写错了"，而应该是"模块之间的契约可能没对齐"。

### 1.5 全链路验证的意义

全链路验证（end-to-end, E2E）比集成测试更进一步：它从**用户视角**出发，验证"我输入一句 SQL，能不能拿到正确结果"。

```
用户视角:  "SELECT id FROM big WHERE id = 50"  →  期望得到一行 (50)
                │
                └──→  整个数据库内部怎么折腾，用户不关心
```

这正是教学数据库的核心价值——**透明**。商业数据库是个黑盒，而 miniDB 的每个环节都可观察：你能打印 AST、能打印 Plan、能打印 ResultSet。本章后面会演示这种可观察性。

---

## 2. 测试策略

### 2.1 测试金字塔

测试界有一个经典模型叫"测试金字塔"（Test Pyramid）：

```
                  /\
                 /  \
                / E2E\        ← 少而精（慢、贵）
               /------\
              / 集成  \      ← 适中
             /----------\
            /   单元    \    ← 多而快
           /--------------\
```

| 层次 | 本项目数量 | 占比 | 说明 |
|---|---|---|---|
| 单元测试 | ~81 | 88% | 每个模块独立测 |
| 集成测试 | 11 | 12% | 端到端链路 |
| E2E / 手工 | 若干 | — | CLI 实际跑 |

本项目是教学项目，规模小，金字塔比较"矮胖"。工业项目里单元测试可能上千个，集成测试上百个，E2E 十几个。

### 2.2 数据准备策略

集成测试需要一份**有代表性的测试数据**。本项目选择 100 行的表 `big(id, val)`：

```c
#define LARGE_ROWS 100

for (int i = 0; i < LARGE_ROWS; i++) {
    big_rows[i].num_cols = 2;
    big_rows[i].values[0] = i;        // id = 0, 1, 2, ..., 99
    big_rows[i].values[1] = i * 10;   // val = 0, 10, 20, ..., 990
}
```

| 设计选择 | 为什么 |
|---|---|
| 100 行 | 足够覆盖各种边界，又不会让测试太慢 |
| `id` 从 0 开始 | 覆盖 0 边界 |
| `id` 到 99 结束 | 覆盖末尾边界 |
| `val = id * 10` | 可预测，方便断言具体值 |
| 两列 | 既测全列 `SELECT *`，又测投影 `SELECT val` |
| 建索引 | `catalog_add_table(..., true, "id")` 声明 `id` 有索引 |

**为什么不随机数据？** 随机数据每次跑结果不一样，无法断言具体行数和值。教学项目要的是**确定性**。

**为什么不用真实文件？** 集成测试聚焦查询链路，存储层用内存数组足够。真实文件 I/O 会让测试变慢且不可重复。

### 2.3 查询选择策略

11 个测试用例的 SQL 不是随便挑的，而是刻意覆盖不同**查询形状**：

| 查询形状 | 覆盖的测试 | 测什么链路能力 |
|---|---|---|
| 全表扫描 | `full_scan`, `project_all` | SeqScan 能跑通 |
| 等值过滤 | `index_lookup`, `no_results` | Filter + 索引选择 |
| 范围过滤 | `range_query`, `first_half`, `second_half` | Filter 范围谓词 |
| 不等过滤 | `not_equal` | `!=` 谓词 |
| 投影 + 过滤 | `project_filter` | Project 在 Filter 之上 |
| DDL 解析 | `create_parse` | CREATE 语句能解析 |
| DML 解析 | `delete_parse` | DELETE 语句能解析 |

这样能保证每种查询路径至少有一个测试走过。

### 2.4 结果验证方法

本项目用 [Unity 测试框架](http://www.throwtheswitch.org/unity)（C 语言轻量级框架）。验证手段分三类：

| 验证手段 | Unity 宏 | 用在 |
|---|---|---|
| 行数断言 | `TEST_ASSERT_EQUAL_INT(期望, rs->num_rows)` | 几乎所有测试 |
| 具体值断言 | `TEST_ASSERT_EQUAL_INT32(50, rs->rows[0].values[0])` | 需要验证内容 |
| 非空断言 | `TEST_ASSERT_NOT_NULL(rs)` | 先确保结果集存在 |
| 类型断言 | `TEST_ASSERT_EQUAL_INT(AST_DELETE, s->type)` | 解析测试 |
| 字符串断言 | `TEST_ASSERT_EQUAL_STRING("big", s->table)` | 解析测试 |
| 布尔断言 | `TEST_ASSERT_TRUE(s->col_defs[0].primary_key)` | CREATE 测试 |

**为什么不对比整个结果集？** 因为手写期望结果集太啰嗦。只断言关键行（首行、末行、行数）已经够抓大部分 bug。

### 2.5 测试夹具（Fixture）

每个测试都需要先建表、建 catalog。本项目用 `init_large_table()` 做公共初始化：

```c
static void init_large_table(void) {
    // 1. 建表名和列
    strcpy(big_table.name, "big");
    big_table.num_cols = 2;
    strcpy(big_cols[0].name, "id");
    strcpy(big_cols[1].name, "val");
    // 2. 填 100 行数据
    for (int i = 0; i < LARGE_ROWS; i++) { ... }
    // 3. 注册到 catalog，声明 id 有索引
    cat = catalog_create();
    catalog_add_table(cat, "big", LARGE_ROWS, LARGE_ROWS / 10 + 1, true, "id");
}
```

| 步骤 | 作用 |
|---|---|
| 建表 | Executor 执行时需要表结构 |
| 填数据 | 查询要有数据可查 |
| 注册 catalog | Optimizer 需要统计信息做代价估算 |
| 声明索引 | Optimizer 才能选 IndexScan vs SeqScan |

> **新手提示**：Unity 的 `setUp`/`tearDown` 是全局的，每个测试前后都会调。本项目把它们留空，改在每个测试开头显式调 `init_large_table()`，这样更灵活——不同测试可以用不同数据。

### 2.6 辅助函数 `run_query`

为了不让每个测试都重复"解析→优化→执行"六行代码，封装了一个辅助函数：

```c
static result_set_t *run_query(const char *sql) {
    exec_table_t tables[] = {big_table};
    parser_t *p = parser_create(sql);
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, cat);
    result_set_t *rs = executor_run(plan, tables, 1);
    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    return rs;
}
```

| 行 | 做什么 | 对应章 |
|---|---|---|
| `parser_create` | 创建解析器 | 7 |
| `parser_parse` | 解析得 AST | 7 |
| `optimizer_optimize` | 优化得 Plan | 8 |
| `executor_run` | 执行得 ResultSet | 9 |
| `plan_destroy` / `free` / `parser_destroy` | 释放中间资源 | — |

注意：`rs` 不在这里释放，留给调用方断言后再 `result_set_destroy(rs)`。这是 C 语言里常见的**所有权约定**——谁最后用，谁负责释放。

---

## 3. 集成测试详解

本节逐个讲解 11 个集成测试用例。每个测试都列出：**目的、SQL、验证点、对应代码行**。

### 3.1 测试用例总览表

| # | 测试函数 | SQL | 期望行数 | 主要验证点 |
|---|---|---|---|---|
| 1 | `test_integ_full_scan` | `SELECT * FROM big` | 100 | 全表扫描链路 |
| 2 | `test_integ_index_lookup` | `SELECT * FROM big WHERE id = 50` | 1 | 等值 + 索引 |
| 3 | `test_integ_range_query` | `SELECT * FROM big WHERE id >= 10 AND id < 20` | 10 | 范围 + 复合谓词 |
| 4 | `test_integ_project_filter` | `SELECT val FROM big WHERE id >= 90` | 10 | 投影 + 过滤 |
| 5 | `test_integ_no_results` | `SELECT * FROM big WHERE id = 99999` | 0 | 空结果处理 |
| 6 | `test_integ_not_equal` | `SELECT * FROM big WHERE id != 0` | 99 | 不等谓词 |
| 7 | `test_integ_first_half` | `SELECT * FROM big WHERE id < 50` | 50 | 前半划分 |
| 8 | `test_integ_second_half` | `SELECT * FROM big WHERE id >= 50` | 50 | 后半划分 |
| 9 | `test_integ_project_all` | `SELECT id FROM big` | 100 | 单列投影 |
| 10 | `test_integ_delete_parse` | `DELETE FROM big WHERE id = 5` | — | DELETE 解析 |
| 11 | `test_integ_create_parse` | `CREATE TABLE test (a INT PRIMARY KEY, b INT)` | — | CREATE 解析 |

### 3.2 测试 1：全表扫描 `test_integ_full_scan`

**目的**：验证最基础的 `SELECT *` 能走完整条链路。

```c
void test_integ_full_scan(void) {
    init_large_table();
    result_set_t *rs = run_query("SELECT * FROM big");
    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(LARGE_ROWS, rs->num_rows);   // 100 行
    TEST_ASSERT_EQUAL_INT32(0, rs->rows[0].values[0]);  // 首行 id=0
    TEST_ASSERT_EQUAL_INT32(99, rs->rows[99].values[0]); // 末行 id=99
    result_set_destroy(rs);
    catalog_destroy(cat);
}
```

| 验证点 | 断言 | 为什么重要 |
|---|---|---|
| 结果集非空 | `NOT_NULL(rs)` | 链路没崩 |
| 行数 = 100 | `EQUAL_INT(100, num_rows)` | 行没丢没多 |
| 首行 id = 0 | `EQUAL_INT32(0, rows[0])` | 顺序没乱 |
| 末行 id = 99 | `EQUAL_INT32(99, rows[99])` | 末尾没截断 |

**链路走向**：

```
"SELECT * FROM big"
   → Parser:  AST{type=SELECT, table="big", cols=["*"]}
   → Optimizer: Plan{SeqScan(big)}
   → Executor: 逐行拷贝到 ResultSet
   → ResultSet: 100 行
```

### 3.3 测试 2：索引查找 `test_integ_index_lookup`

**目的**：验证等值查询能命中索引并返回正确行。

```c
result_set_t *rs = run_query("SELECT * FROM big WHERE id = 50");
TEST_ASSERT_EQUAL_INT(1, rs->num_rows);
TEST_ASSERT_EQUAL_INT32(50, rs->rows[0].values[0]);
TEST_ASSERT_EQUAL_INT32(500, rs->rows[0].values[1]);
```

| 验证点 | 值 | 含义 |
|---|---|---|
| 行数 | 1 | 只命中一行 |
| id | 50 | 命中对的行 |
| val | 500 | 第二列也对（50×10） |

**为什么测 val？** 只测 id 容易漏掉"列错位"bug——如果 Executor 把 val 列丢了呢？测 val 能抓到。

### 3.4 测试 3：范围查询 `test_integ_range_query`

**目的**：验证复合范围谓词 `id >= 10 AND id < 20`。

```c
result_set_t *rs = run_query("SELECT * FROM big WHERE id >= 10 AND id < 20");
TEST_ASSERT_EQUAL_INT(10, rs->num_rows);
TEST_ASSERT_EQUAL_INT32(10, rs->rows[0].values[0]);
TEST_ASSERT_EQUAL_INT32(19, rs->rows[9].values[0]);
```

| 谓词 | 匹配 id | 行数 |
|---|---|---|
| `id >= 10` | 10, 11, ..., 99 | 90 |
| `id < 20` | 0, 1, ..., 19 | 20 |
| 两者 AND | 10, 11, ..., 19 | 10 |

**验证首末行**：首行 id=10，末行 id=19。这能抓"边界 off-by-one"bug——如果实现写成 `id > 10` 会漏掉 10，写成 `id <= 20` 会多出 20。

### 3.5 测试 4：投影 + 过滤 `test_integ_project_filter`

**目的**：验证 `Project` 节点在 `Filter` 之上时，列投影正确。

```c
result_set_t *rs = run_query("SELECT val FROM big WHERE id >= 90");
TEST_ASSERT_EQUAL_INT(10, rs->num_rows);
TEST_ASSERT_EQUAL_INT32(900, rs->rows[0].values[0]);
```

**链路走向**：

```
"SELECT val FROM big WHERE id >= 90"
   → Parser:  AST{cols=["val"], where="id>=90"}
   → Optimizer: Plan{Project(val) ← Filter(id>=90) ← SeqScan(big)}
   → Executor: 先过滤得 10 行，再投影只留 val 列
   → ResultSet: 10 行 1 列 [900, 910, ..., 990]
```

**关键验证**：`rows[0].values[0]` 应该是 900（id=90 时 val=900）。如果投影错了列，可能拿到 90（id 列）。

### 3.6 测试 5：空结果 `test_integ_no_results`

**目的**：验证查询无命中时，返回空结果集而非崩溃。

```c
result_set_t *rs = run_query("SELECT * FROM big WHERE id = 99999");
TEST_ASSERT_EQUAL_INT(0, rs->num_rows);
```

| 场景 | 期望行为 | 错误行为 |
|---|---|---|
| 无命中 | `num_rows = 0`，`rs` 非 NULL | 返回 NULL（下游崩） |
| 访问 `rows[0]` | 不应发生 | 越界访问 |

**为什么重要？** 很多 bug 出在"空集合"边界：除零、空指针、未初始化。这个测试专门盯这个边界。

### 3.7 测试 6：不等过滤 `test_integ_not_equal`

**目的**：验证 `!=` 谓词。

```c
result_set_t *rs = run_query("SELECT * FROM big WHERE id != 0");
TEST_ASSERT_EQUAL_INT(99, rs->num_rows);
```

`id` 从 0 到 99，`id != 0` 排除掉 id=0，剩 99 行。

**为什么单独测 `!=`？** 因为 `!=` 在优化器里通常**无法用索引**（索引适合 `=`、`<`、`>`）。这个测试确认优化器没误选 IndexScan 导致漏行。

### 3.8 测试 7 & 8：前后半划分

```c
// 测试 7
run_query("SELECT * FROM big WHERE id < 50");   // 期望 50 行
// 测试 8
run_query("SELECT * FROM big WHERE id >= 50");  // 期望 50 行
```

| 测试 | 谓词 | 行数 | 覆盖 id |
|---|---|---|---|
| 7 | `id < 50` | 50 | 0..49 |
| 8 | `id >= 50` | 50 | 50..99 |

**为什么成对测？** 两个查询合起来正好覆盖全表 100 行，且不重叠。这能抓"边界归属"bug——如果 `<` 实现成 `<=`，测试 7 会变成 51 行，测试 8 变成 50 行，合起来 101 行，立刻暴露。

### 3.9 测试 9：单列投影 `test_integ_project_all`

**目的**：验证 `SELECT id`（只选一列）在全表扫描下正确。

```c
result_set_t *rs = run_query("SELECT id FROM big");
TEST_ASSERT_EQUAL_INT(LARGE_ROWS, rs->num_rows);
for (int i = 0; i < 10; i++)
    TEST_ASSERT_EQUAL_INT32(i, rs->rows[i].values[0]);
```

**注意**：这里用循环断言前 10 行，而不是全部 100 行。这是**抽样验证**——既够抓顺序 bug，又不会让测试代码太长。

### 3.10 测试 10：DELETE 解析 `test_integ_delete_parse`

**目的**：验证 DELETE 语句能被正确解析（不执行，只解析）。

```c
parser_t *p = parser_create("DELETE FROM big WHERE id = 5");
ast_stmt_t *s = parser_parse(p);
TEST_ASSERT_EQUAL_INT(AST_DELETE, s->type);
TEST_ASSERT_EQUAL_STRING("big", s->table);
TEST_ASSERT_EQUAL_INT(1, s->num_where);
```

| 验证点 | 期望 | 含义 |
|---|---|---|
| 语句类型 | `AST_DELETE` | 识别成 DELETE |
| 表名 | `"big"` | 目标表对 |
| WHERE 子句数 | 1 | 有一个谓词 |

**为什么不执行？** 因为本阶段 Executor 还没实现 DELETE 真正执行（只实现了 SELECT）。但解析层要先把路铺好，下阶段才能用。

### 3.11 测试 11：CREATE 解析 `test_integ_create_parse`

**目的**：验证 CREATE TABLE 语句解析，包括 PRIMARY KEY 标记。

```c
parser_t *p = parser_create("CREATE TABLE test (a INT PRIMARY KEY, b INT)");
ast_stmt_t *s = parser_parse(p);
TEST_ASSERT_EQUAL_INT(AST_CREATE, s->type);
TEST_ASSERT_EQUAL_INT(2, s->num_col_defs);
TEST_ASSERT_TRUE(s->col_defs[0].primary_key);   // a 是主键
TEST_ASSERT_FALSE(s->col_defs[1].primary_key);  // b 不是
```

| 列 | 类型 | PRIMARY KEY |
|---|---|---|
| a | INT | true |
| b | INT | false |

**验证点**：列数 = 2，第一列是主键，第二列不是。这能抓"主键标记错位"bug。

### 3.12 测试用例与代码行对应表

| 测试 | 源文件 | 起止行 |
|---|---|---|
| `test_integ_full_scan` | `test_integration.c` | 53–62 |
| `test_integ_index_lookup` | `test_integration.c` | 64–73 |
| `test_integ_range_query` | `test_integration.c` | 75–84 |
| `test_integ_project_filter` | `test_integration.c` | 86–94 |
| `test_integ_no_results` | `test_integration.c` | 96–103 |
| `test_integ_not_equal` | `test_integration.c` | 105–112 |
| `test_integ_first_half` | `test_integration.c` | 114–121 |
| `test_integ_second_half` | `test_integration.c` | 123–130 |
| `test_integ_project_all` | `test_integration.c` | 132–141 |
| `test_integ_delete_parse` | `test_integration.c` | 143–154 |
| `test_integ_create_parse` | `test_integration.c` | 156–168 |

### 3.13 测试主函数

```c
int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_integ_full_scan);
    RUN_TEST(test_integ_index_lookup);
    // ... 共 11 个
    RUN_TEST(test_integ_create_parse);
    return UNITY_END();
}
```

`UNITY_BEGIN` / `UNITY_END` 是框架固有的启动/收尾宏，`RUN_TEST` 注册一个测试函数。运行后输出类似：

```
-----------------------
11 Tests 11 Passes 0 Fails
OK
```

如果某个测试挂了，会显示具体哪个 FAIL、在哪一行断言失败。

---

## 4. 压测方法论

### 4.1 为什么要压测

集成测试回答"对不对"，压测回答"快不快"。

| 问题 | 集成测试 | 压测 |
|---|---|---|
| 功能正确吗？ | ✅ | — |
| 1000 QPS 够吗？ | — | ✅ |
| 瓶颈在哪？ | — | ✅ |
| 优化有效吗？ | — | ✅ |

教学项目虽然不追求生产级性能，但**量化**能让你对"自己写的东西有多快"有直觉。这种直觉对后续读 SQLite/PostgreSQL 源码至关重要。

### 4.2 基准测试设计原则

| 原则 | 说明 | 本项目做法 |
|---|---|---|
| **可重复** | 同样输入同样输出 | 固定 10000 行固定数据 |
| **可对比** | 能和上次结果比 | 同一机器同一编译选项 |
| **隔离** | 只测目标，不混入噪音 | 分 5 个独立 bench 函数 |
| **预热** | 避免冷启动偏差 | 循环多次取总耗时 |
| **足够长** | 跑够久才稳定 | 10000 次或 100 轮 |

### 4.3 度量指标

本项目度量两个核心指标：

| 指标 | 单位 | 公式 | 含义 |
|---|---|---|---|
| **吞吐量 (QPS)** | queries/s | `次数 / 总耗时` | 每秒能处理多少查询 |
| **行吞吐 (rows/s)** | rows/s | `总行数 / 总耗时` | 每秒能扫多少行 |

**为什么两个都要？** 同样 10000 QPS，一个每次扫 1 行，一个每次扫 1000 行，体感天差地别。行吞吐更能反映"数据移动速度"。

### 4.4 计时方法

用 C 标准库 `clock()`：

```c
static double now_ms(void) {
    return (double)clock() / CLOCKS_PER_SEC * 1000.0;
}

double t0 = now_ms();
// ... 被测代码 ...
double dt = now_ms() - t0;
```

| 项 | 值 | 说明 |
|---|---|---|
| `clock()` | CPU 时钟滴答数 | 进程实际占用的 CPU 时间 |
| `CLOCKS_PER_SEC` | 平台相关 | 每秒滴答数 |
| `× 1000` | 转毫秒 | 方便阅读 |

**注意**：`clock()` 测的是 **CPU 时间**，不是墙钟时间（wall clock）。对 CPU 密集型任务两者接近；如果有大量 sleep/IO，墙钟会远大于 CPU 时间。本项目纯内存计算，用 `clock()` 合适。

> **新手提示**：生产级压测会用 `clock_gettime(CLOCK_MONOTONIC, ...)` 测墙钟，更准。教学项目用 `clock()` 够了。

### 4.5 五个基准测试

`benchmark.c` 设计了 5 个独立基准，逐层放大范围：

| # | 函数 | 测什么 | 数据规模 | 重复次数 |
|---|---|---|---|---|
| 1 | `bench_parse` | 纯解析速度 | — | 10000 次 |
| 2 | `bench_optimize` | 解析+优化速度 | — | 10000 次 |
| 3 | `bench_execute_seqscan` | 全表扫描执行 | 10000 行 | 100 轮 |
| 4 | `bench_execute_filter` | 带过滤执行 | 10000 行 | 100 轮 |
| 5 | `bench_e2e` | 端到端混合 | 10000 行 | 4 查询 × 100 轮 |

**为什么逐层？** 这样能定位瓶颈在哪一层。如果只测 E2E，你只知道"慢"，不知道是 Parse 慢还是 Execute 慢。

### 4.6 基准 1：纯解析 `bench_parse`

```c
static void bench_parse(void) {
    const char *sql = "SELECT id, val FROM bench WHERE id = 42 AND val > 100";
    double t0 = now_ms();
    for (int i = 0; i < BENCH_QUERIES; i++) {
        parser_t *p = parser_create(sql);
        ast_stmt_t *s = parser_parse(p);
        free(s);
        parser_destroy(p);
    }
    double dt = now_ms() - t0;
    printf("Parse:     %d queries in %.1f ms  (%.0f q/s)\n", ...);
}
```

| 设计点 | 说明 |
|---|---|
| 固定 SQL | 每次解析同一句，排除 SQL 长度差异 |
| 10000 次 | 跑够久让计时稳定 |
| 立即释放 | 每轮 `free(s); parser_destroy(p)` 防内存泄漏 |
| 不优化不执行 | 只测 Parser 本身 |

### 4.7 基准 2：解析 + 优化 `bench_optimize`

```c
for (int i = 0; i < BENCH_QUERIES; i++) {
    parser_t *p = parser_create(sql);
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, bench_cat);
    plan_destroy(plan);
    free(s);
    parser_destroy(p);
}
```

**和基准 1 的差异**：多了 `optimizer_optimize`。两者耗时相减，就近似得到"优化本身"的耗时。

### 4.8 基准 3 & 4：执行 `bench_execute_seqscan` / `bench_execute_filter`

```c
// SeqScan: SELECT * FROM bench          (无过滤)
// Filter:  SELECT * FROM bench WHERE val > 5000  (有过滤)
for (int i = 0; i < 100; i++) {
    // 解析 → 优化 → 执行 → 累加行数 → 释放
    total_rows += rs->num_rows;
}
```

| 基准 | SQL | 期望每轮行数 | 测什么 |
|---|---|---|---|
| SeqScan | `SELECT * FROM bench` | 10000 | 纯扫描 + 结果收集 |
| Filter | `SELECT * FROM bench WHERE val > 5000` | ~2500 | 扫描 + 谓词求值 |

**为什么只跑 100 轮而不是 10000？** 因为每轮要扫 10000 行，100 轮就是 100 万行，已经够测了。跑 10000 轮会太慢。

### 4.9 基准 5：端到端混合 `bench_e2e`

```c
const char *queries[] = {
    "SELECT * FROM bench",                              // 全扫
    "SELECT id FROM bench WHERE id = 5000",             // 等值
    "SELECT * FROM bench WHERE id >= 1000 AND id < 2000", // 范围
    "SELECT val FROM bench WHERE id > 9000",            // 投影+过滤
};
// 4 查询 × 100 轮 = 400 次完整 E2E
```

| 查询 | 形状 | 每轮行数 |
|---|---|---|
| 1 | 全表扫描 | 10000 |
| 2 | 等值查找 | 1 |
| 3 | 范围查询 | 1000 |
| 4 | 投影 + 过滤 | 999 |

**为什么混合？** 真实负载不会只跑一种查询。混合测更接近真实体感。

### 4.10 统计方法说明

本项目用**最简单的统计**：总耗时 / 总次数 = 平均单次耗时。

| 统计方法 | 公式 | 优缺点 |
|---|---|---|
| **算术平均**（本项目） | `总耗时 / 次数` | 简单；受离群值影响 |
| 中位数 | 排序取中间 | 抗离群值；要存每次耗时 |
| P99 | 排序取 99 分位 | 关注尾部；要存每次耗时 |
| 标准差 | `sqrt(Σ(x-μ)²/n)` | 看稳定性；要存每次耗时 |

教学项目用平均够了。生产压测（如 wrk、sysbench）会报 P50/P95/P99，因为尾部延迟对用户体验影响更大。

### 4.11 压测数据准备

```c
#define BENCH_ROWS 10000

for (int i = 0; i < BENCH_ROWS; i++) {
    bench_rows[i].values[0] = i;        // id
    bench_rows[i].values[1] = i * 2;    // val
}
catalog_add_table(bench_cat, "bench", BENCH_ROWS,
                  BENCH_ROWS / 10 + 1, true, "id");
```

| 项 | 值 | 说明 |
|---|---|---|
| 行数 | 10000 | 比集成测试大 100 倍 |
| val | `id * 2` | 和集成测试的 `*10` 不同，避免硬编码 |
| 索引 | `id` 有 | 优化器可选 IndexScan |
| 页数估计 | `10000/10+1 = 1001` | 给优化器代价模型用 |

### 4.12 运行压测

```bash
# 编译并运行
cd phase1
mkdir -p build && cd build
cmake .. && make
./test_benchmark
```

期望输出：

```
=== miniDB Benchmark ===
Table: 10000 rows

Parse:     10000 queries in 15.0 ms  (666,667 q/s)
Optimize:  10000 plans in 19.0 ms  (526,316 p/s)
SeqScan:   102400 rows in 8.0 ms  (12,800,000 rows/s)
Filter:    102400 rows in 19.0 ms  (5,389,474 rows/s)
E2E:       400 queries, 302400 rows in 108.0 ms  (3,704 q/s, 2,800,000 rows/s)

=== Done ===
```

> **注意**：具体数值因机器而异。上表是一台典型开发机的参考值。你的机器跑出来数字会不同，但**比例关系**应该类似。

---

## 5. 压测结果分析

### 5.1 结果总表

| 基准 | 处理量 | 耗时 | 吞吐量 |
|---|---|---|---|
| Parse | 10000 queries | 15.0 ms | 666,667 q/s |
| Optimize | 10000 plans | 19.0 ms | 526,316 p/s |
| SeqScan | 102400 rows | 8.0 ms | 12,800,000 rows/s |
| Filter | 102400 rows | 19.0 ms | 5,389,474 rows/s |
| E2E | 400 queries / 302400 rows | 108.0 ms | 3,704 q/s / 2,800,000 rows/s |

### 5.2 各指标含义详解

#### Parse：667K q/s

| 项 | 值 |
|---|---|
| 含义 | 每秒能解析多少条 SQL |
| 667K | 约 66 万条/秒 |
| 对比 | SQLite 约 200K-500K q/s |
| 为什么快 | 手写递归下降，零动态分配热路径 |

**直觉**：解析不是瓶颈。一句 SQL 几十个字符，递归下降扫一遍，微秒级。

#### Optimize：526K p/s

| 项 | 值 |
|---|---|
| 含义 | 每秒能生成多少个执行计划 |
| 526K | 约 52 万计划/秒 |
| 比 Parse 慢 | 因为多了优化步骤 |
| 为什么这个量级 | 启发式规则 + 简单代价估算 |

**直觉**：优化比解析略慢，但仍不是瓶颈。本项目优化器只做谓词下推、索引选择等少量规则，所以快。

#### SeqScan：12.8M rows/s

| 项 | 值 |
|---|---|
| 含义 | 全表扫描每秒能扫多少行 |
| 12.8M | 约 1280 万行/秒 |
| 为什么快 | 内存数组连续遍历，CPU 缓存友好 |
| 对比 | 磁盘扫描约 100K-1M rows/s |

**直觉**：内存扫描极快。瓶颈从来不在"扫"，而在"扫完之后干什么"。

#### Filter：5.4M rows/s

| 项 | 值 |
|---|---|
| 含义 | 带过滤扫描每秒处理多少行 |
| 5.4M | 约 540 万行/秒 |
| 比 SeqScan 慢 | 每行多一次谓词求值 + 分支判断 |
| 慢多少 | 12.8M → 5.4M，约 2.4x |

**直觉**：谓词求值有成本。分支预测（`if (val > 5000)`）会让 CPU 流水线打断，所以慢一半多。

#### E2E：3.7K q/s

| 项 | 值 |
|---|---|
| 含义 | 端到端每秒多少查询 |
| 3.7K | 约 3700 查询/秒 |
| 比 Parse 慢 180x | 因为含执行 + 结果收集 |
| 行吞吐 | 2.8M rows/s |

**直觉**：E2E 才是用户体感。单看 Parse 667K 很爽，但用户等的是完整结果，E2E 3.7K 才是真相。

### 5.3 瓶颈分析

#### 耗时分解

```
E2E 总耗时 108 ms 分解:
  Parse:     ~15 ms  (15/108 ≈ 14%)
  Optimize:  ~19 ms  (19/108 ≈ 18%)
  Execute:   ~74 ms  (74/108 ≈ 68%)
```

```
耗时占比可视化:

  Parse    ████████░░░░░░░░░░░░░░░░░░░░░░  14%
  Optimize ██████████░░░░░░░░░░░░░░░░░░░░  18%
  Execute  ██████████████████████████░░░░░  68%
           ────────────────────────────────
           0%      25%      50%      75%    100%
```

**结论**：执行占大头（68%）。优化和解析加起来才 32%。

#### 执行为什么慢

执行 74ms 里主要在干什么？

| 子步骤 | 估算占比 | 说明 |
|---|---|---|
| 结果集收集 | ~50% | `rs->rows[rs->num_rows++] = row` 涉及结构体拷贝 |
| 谓词求值 | ~30% | 每行算 `val > 5000` |
| 表扫描 | ~15% | 遍历数组 |
| 其他 | ~5% | 内存分配、计数等 |

**最大单项开销**：结果集收集。每命中一行就把整个 `exec_row_t` 结构体拷进 ResultSet。10000 行 × 多列 × 多轮，拷贝量惊人。

### 5.4 各层吞吐量对比

```
吞吐量 (对数尺度):

  Parse     ████████████████████████████  667K q/s
  Optimize  ██████████████████████████    526K p/s
  SeqScan   ████████████████████████████████  12.8M rows/s
  Filter    █████████████████████████   5.4M rows/s
  E2E       ███  3.7K q/s
```

| 对比 | 倍数 | 解读 |
|---|---|---|
| SeqScan vs Filter | 2.4x | 谓词求值让扫描慢 2.4 倍 |
| Parse vs E2E | 180x | 执行 + 收集让 E2E 慢 180 倍 |
| SeqScan vs E2E 行吞吐 | 4.6x | 结果收集让行吞吐降 4.6 倍 |

### 5.5 优化建议

针对瓶颈（执行 + 结果收集），列出可能的优化方向：

| 优化 | 针对的瓶颈 | 预期提升 | 实现难度 | 阶段 |
|---|---|---|---|---|
| **避免结果集拷贝** | 结构体拷贝 | 1.5x | 低 | 阶段 1 可做 |
| **预编译 SQL** | 重复 Parse | 省去 Parse 14% | 中 | 阶段 2 |
| **向量化执行** | 逐行求值 | 3-5x | 高 | 阶段 3 |
| **SIMD 谓词求值** | 谓词分支 | 2-3x | 高 | 阶段 3 |
| **列式存储** | 行存扫描 | 5-10x | 很高 | 阶段 3 |
| **异步 IO** | （本项目无 IO） | — | — | 不适用 |

#### 优化 1：避免结果集拷贝（低难度，推荐先做）

**现状**：

```c
rs->rows[rs->num_rows++] = row;  // 整行拷贝
```

**改法**：存指针而非拷贝：

```c
rs->row_ptrs[rs->num_rows++] = &table->rows[i];  // 只存指针
```

| 项 | 现状 | 改后 |
|---|---|---|
| 每命中行 | 拷贝 N 列 | 存 1 个指针 |
| 内存 | 复制一份 | 共享原表 |
| 风险 | 无 | 原表不能提前释放 |

#### 优化 2：预编译 SQL（中难度）

**现状**：每次查询都重新 Parse。

**改法**：缓存 AST/Plan，相同 SQL 复用。

```c
plan_t *plan = plan_cache_lookup(sql);
if (!plan) {
    plan = optimizer_optimize(parser_parse(sql), cat);
    plan_cache_insert(sql, plan);
}
executor_run(plan, ...);
```

| 场景 | 现状 | 改后 |
|---|---|---|
| 同一 SQL 跑 1000 次 | Parse 1000 次 | Parse 1 次 |
| 不同 SQL 各 1 次 | 无收益 | 无收益 |

#### 优化 3：向量化执行（高难度，阶段 3）

**现状**：逐行求值 `if (val > 5000)`。

**改法**：一次取一批行，批量求值。

```c
// 逐行
for (int i = 0; i < n; i++)
    if (rows[i].val > 5000) emit(rows[i]);

// 向量化
batch_t *b = fetch_batch(1024);
mask_t m = vector_gt(b->vals, 5000);  // 一次比 1024 个
emit_masked(b, m);
```

| 项 | 逐行 | 向量化 |
|---|---|---|
| 分支 | 每行 1 次分支 | 批内无分支 |
| 缓存 | 逐行访问 | 批顺序访问 |
| SIMD | 不用 | 可用 |

### 5.6 压测可信度讨论

| 因素 | 本项目 | 影响 |
|---|---|---|
| 数据在内存 | 是 | 高估了磁盘数据库对比 |
| 单线程 | 是 | 低估了多核并行能力 |
| 无网络 | 是 | 真实 DB 有网络开销 |
| 固定 SQL | 是 | 真实负载更多样 |
| `clock()` 精度 | 毫秒级 | 微秒级操作有误差 |

**结论**：这些数字适合**横向对比各层相对快慢**，不适合**纵向和真实数据库比绝对值**。真实数据库有磁盘、网络、并发，绝对数字会差几个数量级。

---

## 6. 全链路验证

### 6.1 为什么要"可观察"

商业数据库是黑盒：你输入 SQL，它给你结果，中间发生了什么你不知道。

miniDB 是白盒：每个环节都能打印中间产物。这对学习至关重要——你能亲眼看到 AST 长什么样、Plan 长什么样。

### 6.2 完整路径图

以 `SELECT id FROM users WHERE age > 28` 为例：

```
用户输入: SELECT id FROM users WHERE age > 28
    │
    ▼
┌─────────────────────────────────────────────┐
│ 1. Lexer (词法分析)                          │
│    输入: "SELECT id FROM users WHERE age > 28" │
│    输出: Token 流                            │
│    [SELECT][id][FROM][users][WHERE][age][>][28] │
└─────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────┐
│ 2. Parser (语法分析)                         │
│    输入: Token 流                            │
│    输出: AST                                 │
│    AST:                                      │
│      SELECT                                  │
│        cols: [id]                            │
│        table: users                          │
│        where: age > 28                       │
└─────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────┐
│ 3. Optimizer (优化)                          │
│    输入: AST + Catalog                       │
│    输出: Plan                                │
│    Plan:                                     │
│      Project(id)  [rows=2 cost=1.6]          │
│        Filter(age > 28)  [rows=2 cost=1.5]   │
│          SeqScan(users)  [rows=5 cost=1.0]   │
└─────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────┐
│ 4. Executor (执行)                           │
│    输入: Plan + Table data                   │
│    输出: ResultSet                           │
│    执行顺序:                                 │
│      a. SeqScan 扫 users 得 5 行             │
│      b. Filter 保留 age>28 得 2 行           │
│      c. Project 只留 id 列                   │
└─────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────┐
│ 5. ResultSet (结果)                          │
│    列: [id]                                  │
│    行: [[2], [3], [5]]                       │
└─────────────────────────────────────────────┘
    │
    ▼
用户看到:
  id
  ---
  2
  3
  5
  (3 rows)
```

### 6.3 各环节中间产物

| 环节 | 中间产物 | 数据结构 | 可打印 |
|---|---|---|---|
| Lexer | Token 流 | `token_t[]` | ✅ |
| Parser | AST | `ast_stmt_t*` | ✅ |
| Optimizer | Plan 树 | `plan_node_t*` | ✅ |
| Executor | ResultSet | `result_set_t*` | ✅ |

### 6.4 CLI 中的可观察示例

```
miniDB> SELECT id FROM users WHERE age > 28;
AST: SELECT id FROM users WHERE age > 28
Plan:
  Project(id)  [rows=2 cost=1.6]
    Filter(age >)  [rows=2 cost=1.5]
      SeqScan(users)  [rows=5 cost=1.0]
Result:
id
---
2
3
5
(3 rows)
```

| 输出段 | 对应环节 | 价值 |
|---|---|---|
| `AST:` | Parser | 看 SQL 被理解成什么 |
| `Plan:` | Optimizer | 看计划树和代价估算 |
| `Result:` | Executor | 看最终结果 |

### 6.5 代价估算怎么看

Plan 里每个节点都有 `[rows=X cost=Y]`：

| 节点 | rows | cost | 含义 |
|---|---|---|---|
| SeqScan(users) | 5 | 1.0 | 扫 5 行，基础代价 1.0 |
| Filter(age > 28) | 2 | 1.5 | 过滤后剩 2 行，代价 1.5 |
| Project(id) | 2 | 1.6 | 投影不改变行数，代价 +0.1 |

**cost 怎么算？** 从叶子往上累加，每层加一点。优化器用这个 cost 在多个计划里选最便宜的。

### 6.6 链路正确性如何保证

| 保证手段 | 在哪做 | 例子 |
|---|---|---|
| 单元测试 | 每章 | Parser 测 AST 结构 |
| 集成测试 | 本章 | SQL → ResultSet 全链 |
| 断言 | 运行时 | `assert(plan != NULL)` |
| 内存检查 | CI | Valgrind / ASan |
| 静态分析 | CI | cppcheck / clang-tidy |

### 6.7 一条查询的完整生命周期

把上面所有环节串成时间线：

```
t=0     用户敲下 SQL
t=1μs   Lexer 产出 Token 流
t=3μs   Parser 产出 AST
t=5μs   Optimizer 查 Catalog
t=7μs   Optimizer 生成 Plan
t=8μs   Executor 开始 SeqScan
t=100μs Executor 扫完 5 行
t=120μs Filter 过滤完得 2 行
t=130μs Project 投影完
t=140μs ResultSet 就绪
t=150μs CLI 打印结果
```

**总耗时约 150 微秒**。其中执行占大头，和压测结论一致。

---

## 7. 阶段 1 总结

### 7.1 11 章内容回顾

| 章 | 主题 | 核心模块 | 关键概念 |
|---|---|---|---|
| 0 | 基础设施 | CMake + Unity | 项目骨架、测试框架 |
| 1 | 存储基础 | Page | 定长页、slotted page |
| 2 | Buffer Pool | BufferPool + Replacer | LRU/Clock 替换 |
| 3 | B+Tree | BTree | 有序索引 |
| 4 | 堆表 | Heap | 无序存储 |
| 5 | WAL | WAL | 预写日志、崩溃恢复 |
| 6 | 事务/MVCC | TxnManager | 隔离级别、多版本 |
| 7 | SQL 解析 | Lexer + Parser | 词法、递归下降 |
| 8 | 优化器 | Optimizer + Catalog | 启发式、代价模型 |
| 9 | 执行引擎 | Executor | Volcano 模型 |
| 10 | CLI | CLI | 交互式 shell |
| 11 | 集成/压测 | test_integration + benchmark | 端到端验证 |

### 7.2 代码量统计

| 章 | 模块 | 代码量 | 测试数 |
|---|---|---|---|
| 1 | 存储基础 | ~600 行 | 15 |
| 2 | Buffer Pool | ~500 行 | 10 |
| 3 | B+Tree | ~400 行 | 8 |
| 4 | 堆表 | ~500 行 | 6 |
| 5 | WAL | ~300 行 | 5 |
| 6 | 事务/MVCC | ~400 行 | 6 |
| 7 | SQL 解析 | ~400 行 | 11 |
| 8 | 优化器 | ~300 行 | 10 |
| 9 | 执行引擎 | ~250 行 | 10 |
| 10 | CLI | ~200 行 | — |
| 11 | 集成/压测 | ~300 行 | 11 |
| **合计** | | **~4150 行** | **92** |

### 7.3 代码量分布图

```
代码量 (行):

  Ch1 存储      ████████████████████████  600
  Ch2 BufferPool ████████████████████    500
  Ch3 B+Tree    ████████████████        400
  Ch4 Heap      ████████████████████    500
  Ch5 WAL       ████████████            300
  Ch6 Txn       ████████████████        400
  Ch7 Parser    ████████████████        400
  Ch8 Optimizer ████████████            300
  Ch9 Executor  ██████████              250
  Ch10 CLI      ████████                200
  Ch11 集成     ████████████            300
```

**观察**：存储层（Ch1-4）占了约一半代码。这是因为存储是数据库最复杂的部分——要处理页、缓存、索引、堆表四种结构。

### 7.4 测试数分布图

```
测试数:

  Ch1  ███████████████  15
  Ch2  ██████████      10
  Ch3  ████████        8
  Ch4  ██████          6
  Ch5  █████           5
  Ch6  ██████          6
  Ch7  ███████████     11
  Ch8  ██████████      10
  Ch9  ██████████      10
  Ch10 (无单元测试)
  Ch11 ███████████     11
```

**测试/代码比**：

| 章 | 代码行 | 测试数 | 测试/百行 |
|---|---|---|---|
| 1 | 600 | 15 | 2.5 |
| 7 | 400 | 11 | 2.75 |
| 8 | 300 | 10 | 3.3 |
| 9 | 250 | 10 | 4.0 |
| **平均** | — | — | **~2.5** |

每百行代码约 2.5 个测试，密度健康。

### 7.5 数据库架构总图

```
SQL 文本
    │
    ▼
┌─────────┐     ┌─────────┐     ┌─────────┐     ┌─────────┐
│  Lexer  │ ──▶ │ Parser  │ ──▶ │Optimizer│ ──▶ │Executor │
└─────────┘     └─────────┘     └─────────┘     └─────────┘
                                       │              │
                                       ▼              ▼
                                 ┌─────────┐    ┌─────────┐
                                 │ Catalog │    │ResultSet│
                                 └─────────┘    └─────────┘
                                       │
                                       ▼
                 ┌─────────────────────────────────────┐
                 │          Storage Stack               │
                 │  ┌───────┐  ┌───────┐  ┌────────┐  │
                 │  │  B+Tree│  │  Heap  │  │  WAL   │  │
                 │  └───────┘  └───────┘  └────────┘  │
                 │  ┌───────┐  ┌───────┐  ┌────────┐  │
                 │  │BufferPool│ │ Pager │  │  MVCC  │  │
                 │  └───────┘  └───────┘  └────────┘  │
                 │  ┌───────────────────────────────┐  │
                 │  │         File Manager           │  │
                 │  └───────────────────────────────┘  │
                 └─────────────────────────────────────┘
```

### 7.6 数据流分层

| 层 | 组件 | 数据形态 | 方向 |
|---|---|---|---|
| SQL 层 | Lexer/Parser | 文本 → Token → AST | 自上而下 |
| 优化层 | Optimizer/Catalog | AST → Plan | 自上而下 |
| 执行层 | Executor | Plan → ResultSet | 自上而下 |
| 存储层 | B+Tree/Heap | 行/键值 | 被执行层调用 |
| 缓冲层 | BufferPool/Pager | 页 | 被存储层调用 |
| 物理层 | FileManager | 字节 | 被缓冲层调用 |

### 7.7 每章学到的核心技能

| 章 | 学到的技能 |
|---|---|
| 1 | 页式存储、slotted page 设计 |
| 2 | 缓存替换算法（LRU/Clock） |
| 3 | B+Tree 插入/删除/分裂/合并 |
| 4 | 堆表 + 索引的组织方式 |
| 5 | WAL 原理、崩溃恢复 |
| 6 | 事务隔离、MVCC 多版本 |
| 7 | 手写递归下降解析器 |
| 8 | 启发式优化、代价模型 |
| 9 | Volcano 执行模型 |
| 10 | 交互式 CLI 设计 |
| 11 | 集成测试、性能压测 |

### 7.8 文件清单

| 文件 | 职责 | 行数 |
|---|---|---|
| `test_integration.c` | 11 个端到端集成测试 | 184 |
| `benchmark.c` | 5 个性能基准 | 164 |

---

## 8. 与真实数据库对比

### 8.1 功能对比

| 特性 | miniDB | SQLite | PostgreSQL |
|---|---|---|---|
| 实现语言 | C | C | C |
| 存储模型 | Slotted Page | B-Tree | Heap + Index |
| Buffer Pool | LRU/Clock | mmap | LRU + ARC |
| WAL | 单文件 | WAL 文件 | WAL + Checkpoint |
| 并发控制 | 2PL + MVCC | WAL | MVCC |
| SQL 支持 | 子集 | 完整 | 完整 |
| 优化器 | 启发式 | 基于代价 | 基于代价 |
| 执行模型 | Volcano | 字节码 | Volcano |
| 网络协议 | 无 | 嵌入式 | TCP |
| 事务 | 支持 | 支持 | 支持 |
| 崩溃恢复 | 支持 | 支持 | 支持 |

### 8.2 SQL 支持范围对比

| SQL 特性 | miniDB | SQLite | PostgreSQL |
|---|---|---|---|
| SELECT * | ✅ | ✅ | ✅ |
| SELECT col | ✅ | ✅ | ✅ |
| WHERE = | ✅ | ✅ | ✅ |
| WHERE < > <= >= | ✅ | ✅ | ✅ |
| WHERE != | ✅ | ✅ | ✅ |
| WHERE AND | ✅ | ✅ | ✅ |
| WHERE OR | ❌ | ✅ | ✅ |
| JOIN | ❌ | ✅ | ✅ |
| GROUP BY | ❌ | ✅ | ✅ |
| ORDER BY | ❌ | ✅ | ✅ |
| LIMIT | ❌ | ✅ | ✅ |
| 子查询 | ❌ | ✅ | ✅ |
| INSERT | ✅ | ✅ | ✅ |
| DELETE | 解析 only | ✅ | ✅ |
| UPDATE | ❌ | ✅ | ✅ |
| CREATE TABLE | ✅ | ✅ | ✅ |
| CREATE INDEX | ❌ | ✅ | ✅ |
| 事务语句 | ❌ | ✅ | ✅ |

### 8.3 性能对比（定性）

| 指标 | miniDB | SQLite | PostgreSQL |
|---|---|---|---|
| 解析速度 | ~667K q/s | ~200-500K q/s | ~100-300K q/s |
| 全表扫描 | ~12.8M rows/s | ~5-10M rows/s | ~5-20M rows/s |
| 索引查找 | ~1M q/s | ~500K-1M q/s | ~500K-2M q/s |
| E2E QPS | ~3.7K q/s | ~5-20K q/s | ~2-10K q/s |
| 数据规模 | 内存 | GB 级 | TB 级 |

> **注意**：miniDB 数字是纯内存、单线程、无网络。真实数据库有磁盘、网络、并发开销，直接比不公平。miniDB 的快是因为**啥都没做**，不是因为做得好。

### 8.4 架构对比

| 维度 | miniDB | SQLite | PostgreSQL |
|---|---|---|---|
| 部署 | 单进程 | 嵌入式库 | 客户-服务器 |
| 存储 | 内存为主 | 单文件 | 多文件 + 表空间 |
| 并发 | 单线程 | 文件锁 | 多进程 + 共享内存 |
| 网络 | 无 | 无 | TCP/Unix socket |
| 扩展 | 无 | 无 | 插件、扩展 |

### 8.5 代码规模对比

| 项目 | 代码量 | 说明 |
|---|---|---|
| miniDB | ~4150 行 C | 教学骨架 |
| SQLite | ~200K 行 C | 嵌入式生产级 |
| PostgreSQL | ~1.5M 行 C | 企业级 |

```
代码量 (对数):

  miniDB      █  4K
  SQLite      ███████████████████████████████  200K
  PostgreSQL  ███████████████████████████████████████████  1.5M
```

miniDB 是 SQLite 的 2%，PostgreSQL 的 0.3%。但核心概念都覆盖了——这就是教学项目的价值。

### 8.6 什么 miniDB 有而真实数据库没有

| 特性 | 价值 |
|---|---|
| 完全透明 | 每个中间产物可打印 |
| 代码可读 | 没有性能 hack，纯教学 |
| 模块独立 | 每章可单独学习 |
| 中文注释 | 新手友好 |

---

## 9. 学到的教训

### 9.1 教训总览

| # | 教训 | 出处 | 应对 |
|---|---|---|---|
| 1 | 单元全绿 ≠ 集成能跑 | 本章 | 必须写集成测试 |
| 2 | 接口契约要显式 | Parser↔Optimizer | 用结构体明确字段 |
| 3 | 内存所有权要约定 | run_query | 注释谁负责释放 |
| 4 | 边界条件最易错 | 范围查询 | 测首末行 |
| 5 | 空集合是特殊 case | no_results | 单独测 |
| 6 | 优化器可能选错计划 | not_equal | 测试验证执行结果 |
| 7 | 压测要分层 | benchmark | 逐层 bench |
| 8 | 平均值会骗人 | 压测统计 | 关注分布 |
| 9 | 拷贝是隐形杀手 | 结果收集 | 考虑存指针 |
| 10 | 教学代码要可读 | 全项目 | 不为性能牺牲清晰 |

### 9.2 教训 1：单元全绿 ≠ 集成能跑

**现象**：写完 Parser 单元测试全过，写完 Optimizer 单元测试全过，但串起来一跑就崩。

**根因**：Parser 单元测试用自己造的 AST，Optimizer 单元测试也用自己造的 AST，两边对"AST 长什么样"的假设不一致。

**解决**：集成测试用**真实 Parser 产出的 AST** 喂给 Optimizer，强制契约对齐。

**启示**：单元测试测的是"自洽"，集成测试测的是"协作"。

### 9.3 教训 2：接口契约要显式

**现象**：Parser 输出字段叫 `table_name`，Optimizer 读 `table`，编译过（C 不查字段名），运行崩。

**根因**：C 语言结构体字段名错配不会编译报错，只会在运行时读到垃圾值。

**解决**：
- 用统一头文件定义结构体（`ast.h`），两边都 include。
- 集成测试会立刻暴露——读出 `table` 是 NULL 或乱码。

**启示**：弱类型语言里，接口契约更要靠**统一头文件**和**集成测试**保证。

### 9.4 教训 3：内存所有权要约定

**现象**：`run_query` 里 `plan_destroy(plan); free(s); parser_destroy(p);` 顺序写错，double free 崩溃。

**根因**：C 没有所有权概念，谁该释放全靠人脑记。

**解决**：明确约定——
- `parser_create` 返回的 `parser_t*` 由 `parser_destroy` 释放。
- `parser_parse` 返回的 `ast_stmt_t*` 由 `free` 释放（它是 `malloc` 出来的）。
- `optimizer_optimize` 返回的 `plan_t*` 由 `plan_destroy` 释放。
- `executor_run` 返回的 `result_set_t*` 由**调用方**释放。

**启示**：C 项目里每个函数的 doc comment 都应写明"返回值所有权归谁"。

### 9.5 教训 4：边界条件最易错

**现象**：`WHERE id >= 10 AND id < 20` 期望 10 行，实际 9 行——`>=` 写成了 `>`。

**根因**：off-by-one 是编程最经典 bug。

**解决**：集成测试断言**首行和末行**的具体值，不只断言行数。`rows[0].id == 10` 和 `rows[9].id == 19` 能抓到。

**启示**：边界值要显式测，不能只测"差不多对"。

### 9.6 教训 5：空集合是特殊 case

**现象**：`WHERE id = 99999` 无命中，返回的 `rs` 是 NULL，下游 `rs->num_rows` 崩。

**根因**：很多人写"有结果"路径，忘了"无结果"路径。

**解决**：`test_integ_no_results` 专门测这个——期望 `rs` 非 NULL 且 `num_rows == 0`。

**启示**：每个查询路径都要想"没命中怎么办"。

### 9.7 教训 6：优化器可能选错计划

**现象**：`WHERE id != 0` 优化器误选 IndexScan，结果漏行。

**根因**：`!=` 谓词理论上不该用索引（索引不支持不等），但优化器规则没排除。

**解决**：`test_integ_not_equal` 验证行数 = 99，如果漏行立刻 FAIL。

**启示**：优化器的"聪明"可能帮倒忙。要有测试盯住每种谓词的执行结果。

### 9.8 教训 7：压测要分层

**现象**：只测 E2E，发现慢，但不知道慢在哪。

**根因**：E2E 把 Parse + Optimize + Execute 揉成一团，无法定位。

**解决**：分 5 个基准，逐层测。`bench_parse` 只测 Parse，`bench_optimize` 测 Parse+Optimize，相减得 Optimize 本身耗时。

**启示**：压测要能"剥洋葱"，每层单独测。

### 9.9 教训 8：平均值会骗人

**现象**：平均 3.7K q/s 看着不错，但偶尔卡 50ms，用户体验差。

**根因**：平均掩盖了尾部延迟。

**解决**：教学项目用平均够了，但要知道生产要看 P99。

**启示**：永远问自己"这个平均背后分布是什么样的"。

### 9.10 教训 9：拷贝是隐形杀手

**现象**：E2E 慢，profile 发现 50% 时间在 `rs->rows[rs->num_rows++] = row` 这行拷贝。

**根因**：每命中一行就把整个结构体拷一份，看似无害，量大了就慢。

**解决**：见 §5.5 优化 1——存指针而非拷贝。

**启示**：C 里 `=` 对结构体是浅拷贝，量大时是性能杀手。

### 9.11 教训 10：教学代码要可读

**现象**：为了让某基准快 10%，把代码写得晦涩，新手看不懂。

**根因**：教学项目首要目标是**让人看懂**，不是跑得快。

**解决**：拒绝过早优化。宁可慢一点，也要代码清晰。

**启示**：教学代码和生产代码的评价标准不同。教学重可读，生产重性能。

---

## 10. 下一步展望

### 10.1 阶段 2 内容预告

阶段 1 建了一个"能跑"的迷你数据库。阶段 2 要**深入真实数据库**，看工业级是怎么做的。

| 阶段 2 章 | 主题 | 对象 |
|---|---|---|
| 1 | SQLite 架构概览 | SQLite 源码 |
| 2 | SQLite 存储引擎 | B-Tree on page |
| 3 | SQLite WAL | WAL + checkpoint |
| 4 | SQLite 优化器 | 基于代价 |
| 5 | PostgreSQL 架构概览 | PG 源码 |
| 6 | PostgreSQL MVCC | 多版本并发 |
| 7 | PostgreSQL 执行器 | Volcano + 算道 |
| 8 | 对比与反思 | miniDB vs 真实 |

### 10.2 阶段 2 目标

| 目标 | 说明 |
|---|---|
| 读源码 | 能看懂 SQLite/PG 关键模块 |
| 对比 | 理解 miniDB 简化了什么 |
| 动手 | 在真实 DB 上做小实验 |
| 视野 | 知道工业级有哪些 miniDB 没有的 |

### 10.3 阶段 3 展望（远期）

| 方向 | 内容 |
|---|---|
| 向量化执行 | 批量处理、SIMD |
| 列式存储 | 列存 + 压缩 |
| 分布式 | 分片、复制 |
| 查询并行 | 多线程执行 |
| 代价模型进阶 | 直方图、统计信息 |

### 10.4 学习路径建议

```
阶段 1 (已完成)          阶段 2 (下一步)         阶段 3 (远期)
┌──────────────┐        ┌──────────────┐        ┌──────────────┐
│ 手写 miniDB  │  ──▶   │ 读 SQLite/PG │  ──▶   │ 进阶优化     │
│ 理解骨架     │        │ 理解工业级   │        │ 理解前沿     │
└──────────────┘        └──────────────┘        └──────────────┘
   4150 行 C              200K-1.5M 行 C           论文 + 实现
```

### 10.5 推荐阅读

| 资源 | 主题 | 难度 |
|---|---|---|
| 《Database Internals》 | 存储与事务 | 中 |
| 《Designing Data-Intensive Applications》 | 系统设计 | 中 |
| SQLite 源码注释 | 真实实现 | 高 |
| PostgreSQL 官方文档 | 行为参考 | 低 |
| CMU 15-445 课程 | 数据库系统 | 高 |

---

## 11. 习题

### 11.1 基础题

**题 1**：单元测试和集成测试的区别是什么？各举一个本项目例子。

**题 2**：本项目集成测试用多少行的表？为什么选这个规模？

**题 3**：`run_query` 辅助函数封装了哪几步？为什么不直接在测试里写？

**题 4**：`test_integ_no_results` 测的是什么边界？如果不写这个测试可能漏什么 bug？

**题 5**：压测的 5 个基准为什么要逐层测，而不是只测一个 E2E？

**题 6**：`clock()` 测的是 CPU 时间还是墙钟时间？两者什么时候不一样？

### 11.2 分析题

**题 7**：下表是某次压测结果，请计算问号处的吞吐量。

| 基准 | 处理量 | 耗时 | 吞吐量 |
|---|---|---|---|
| Parse | 10000 queries | 20 ms | ? q/s |
| SeqScan | 50000 rows | 5 ms | ? rows/s |
| E2E | 200 queries | 50 ms | ? q/s |

**题 8**：E2E 耗时 108ms，其中 Parse 15ms、Optimize 19ms、Execute 74ms。如果引入预编译 SQL（省去 Parse），E2E 吞吐量提升多少？

**题 9**：`test_integ_range_query` 测 `id >= 10 AND id < 20`，期望 10 行。如果实现错成 `id > 10 AND id <= 20`，会得到几行？测试会怎么报错？

**题 10**：为什么 `test_integ_not_equal`（`id != 0`）能抓到"优化器误选 IndexScan"的 bug？从优化器决策角度解释。

### 11.3 实践题

**题 11**：给集成测试加一个 `test_integ_or_clause`，测 `WHERE id = 5 OR id = 10`。先查 Parser 是否支持 OR，不支持的话先加解析支持。

**题 12**：给压测加一个 `bench_index_lookup`，专门测等值索引查找的 QPS。参考 `bench_execute_seqscan` 的写法。

**题 13**：实现 §5.5 优化 1（避免结果集拷贝），把 `rs->rows[++n] = row` 改成存指针。重跑压测，对比 E2E 提升多少。

**题 14**：给 `run_query` 加一个 `print_plan` 参数，当为 true 时打印 Plan 树。用于调试时观察优化器决策。

**题 15**：写一个 `test_integ_order_by`，测 `SELECT * FROM big ORDER BY val DESC`。需要先在 Parser 加 ORDER BY 支持，再在 Executor 加 Sort 节点。

### 11.4 思考题

**题 16**：miniDB 的 E2E 是 3.7K q/s，SQLite 在类似场景能 5-20K q/s。miniDB 慢在哪？快在哪？（提示：miniDB 无磁盘无网络，但结果集拷贝重）

**题 17**：如果要把 miniDB 从单线程改成多线程，哪些模块要改？哪些会冲突？（提示：BufferPool、Catalog、WAL）

**题 18**：教学项目"透明可观察"和工业项目"性能优先"有时矛盾。举一个本项目里为了可读牺牲性能的例子，并讨论这个取舍对不对。

### 11.5 习题参考答案要点

| 题号 | 答案要点 |
|---|---|
| 1 | 单元测单模块（如 Parser 测 AST），集成测全链（如 `test_integ_full_scan`） |
| 2 | 100 行；够覆盖边界又不慢 |
| 3 | 解析→优化→执行→释放；封装避免重复 |
| 4 | 空结果；漏"返回 NULL 下游崩"bug |
| 5 | 分层才能定位瓶颈 |
| 6 | CPU 时间；有 sleep/IO 时不同 |
| 7 | 500K q/s；10M rows/s；4K q/s |
| 8 | 108→93ms，提升约 16% |
| 9 | 10 行（11..20）；行数对但首末值错，断言 FAIL |
| 10 | `!=` 不该用索引，误选会漏行，行数断言抓到 |
| 11 | 先看 Parser 是否支持 OR，不支持加 `||` 或 `OR` token |
| 12 | 仿 `bench_execute_seqscan`，SQL 用 `WHERE id = 5000` |
| 13 | 改 `row_ptrs` 存指针，E2E 预期提升 1.5x |
| 14 | 在 `run_query` 里 `plan_print(plan)` |
| 15 | Parser 加 ORDER BY 子句，Executor 加 Sort 节点 |
| 16 | 慢在结果拷贝；快在无 IO 无网络 |
| 17 | BufferPool 要加锁；Catalog 只读可共享；WAL 要顺序写 |
| 18 | 如结果集拷贝 vs 存指针；教学阶段取舍合理 |

---

## 附录 A：完整测试清单

| # | 测试函数 | 文件 | 类型 |
|---|---|---|---|
| 1 | `test_integ_full_scan` | test_integration.c | 集成 |
| 2 | `test_integ_index_lookup` | test_integration.c | 集成 |
| 3 | `test_integ_range_query` | test_integration.c | 集成 |
| 4 | `test_integ_project_filter` | test_integration.c | 集成 |
| 5 | `test_integ_no_results` | test_integration.c | 集成 |
| 6 | `test_integ_not_equal` | test_integration.c | 集成 |
| 7 | `test_integ_first_half` | test_integration.c | 集成 |
| 8 | `test_integ_second_half` | test_integration.c | 集成 |
| 9 | `test_integ_project_all` | test_integration.c | 集成 |
| 10 | `test_integ_delete_parse` | test_integration.c | 集成 |
| 11 | `test_integ_create_parse` | test_integration.c | 集成 |
| 12 | `bench_parse` | benchmark.c | 压测 |
| 13 | `bench_optimize` | benchmark.c | 压测 |
| 14 | `bench_execute_seqscan` | benchmark.c | 压测 |
| 15 | `bench_execute_filter` | benchmark.c | 压测 |
| 16 | `bench_e2e` | benchmark.c | 压测 |

## 附录 B：压测运行环境建议

| 项 | 建议 | 原因 |
|---|---|---|
| 编译选项 | `-O2` | 开启常规优化 |
| 关闭 debug | `-DNDEBUG` | 去掉 assert |
| 机器 | 独占，无其他负载 | 避免噪音 |
| 多次跑 | 取最稳的一次 | 排除偶发 |
| 记录环境 | CPU/内存/OS | 可复现 |

## 附录 C：术语表

| 术语 | 英文 | 含义 |
|---|---|---|
| 集成测试 | Integration Test | 多模块协作测试 |
| 端到端 | End-to-End (E2E) | 从输入到输出全链 |
| 基准测试 | Benchmark | 性能测量 |
| 吞吐量 | Throughput (QPS) | 单位时间处理量 |
| 延迟 | Latency | 单次耗时 |
| P99 | 99th Percentile | 99% 请求低于此值 |
| 夹具 | Fixture | 测试前准备 |
| 断言 | Assertion | 验证期望 |
| 瓶颈 | Bottleneck | 最慢的环节 |
| 向量化 | Vectorized | 批量处理 |
| 预编译 | Prepared Statement | 缓存执行计划 |

---

## 附录 D：从本章出发的延伸阅读

| 主题 | 查什么 |
|---|---|
| Unity 框架 | throwtheswitch.org/unity |
| C 基准测试 | `clock_gettime`、`rdtsc` |
| 生产压测工具 | wrk、sysbench、JMH |
| SQLite 内部 | sqlite.org/arch.html |
| PG 执行器 | PostgreSQL 源码 `src/backend/executor/` |
| Volcano 模型 | Goetz Graefe 1994 论文 |

---

> **阶段 1 完结**。接下来进入**阶段 2**：深入 SQLite 和 PostgreSQL 的实战，了解工业级数据库如何实现这些功能。
