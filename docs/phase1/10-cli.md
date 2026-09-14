# 章10：CLI 与网络协议

> 用户需要一个界面来输入 SQL 并查看结果。本章实现交互式 CLI，串联 Parser → Optimizer → Executor 全流程。

## 架构

```
用户输入 ──CLI──▶ Parser ──AST──▶ Optimizer ──Plan──▶ Executor ──ResultSet──▶ 输出
 "SELECT..."     "SELECT..."    ast_stmt_t    plan_node_t    result_set_t    表格打印
```

## CLI 功能

```
miniDB v0.1  (type .help for help)
miniDB> SELECT * FROM users;
AST: SELECT * FROM users
Plan:
  SeqScan(users)  [rows=5 cost=1.0]
Result:
id | age | score
--- | --- | ---
1 | 25 | 85
2 | 30 | 90
3 | 35 | 75
4 | 28 | 95
5 | 40 | 60
(5 rows)
```

### 点命令

| 命令 | 作用 |
|---|---|
| `.help` | 显示帮助 |
| `.tables` | 列出所有表 |
| `.exit` / `.quit` | 退出 |

### SQL 输入

- 以 `;` 结尾才执行
- 支持多行输入
- 自动去除首尾空白

## DB Context

```c
typedef struct {
    catalog_t *catalog;           // 表统计信息（给优化器用）
    exec_table_t tables[16];      // 实际数据（给执行器用）
    int num_tables;
} db_context_t;
```

`db_context_t` 是全局上下文，连接优化器的 Catalog 和执行器的数据源。

## 执行流程

```c
static void process_sql(db_context_t *ctx, const char *sql) {
    // 1. 解析
    parser_t *p = parser_create(sql);
    ast_stmt_t *stmt = parser_parse(p);

    // 2. 优化
    plan_node_t *plan = optimizer_optimize(stmt, ctx->catalog);

    // 3. 执行
    if (stmt->type == AST_SELECT) {
        result_set_t *rs = executor_run(plan, ctx->tables, ctx->num_tables);
        result_set_print(rs);
    }

    // 4. 清理
    plan_destroy(plan);
    free(stmt);
    parser_destroy(p);
}
```

## 示例数据

CLI 启动时预加载两张表：

### users

| id | age | score |
|---|---|---|
| 1 | 25 | 85 |
| 2 | 30 | 90 |
| 3 | 35 | 75 |
| 4 | 28 | 95 |
| 5 | 40 | 60 |

- 索引：`id`（主键）

### orders

| order_id | user_id | amount |
|---|---|---|
| 101 | 1 | 500 |
| 102 | 2 | 300 |
| 103 | 1 | 700 |
| 104 | 3 | 200 |

- 索引：`order_id`（主键）

## 查询示例

### 索引扫描

```
miniDB> SELECT * FROM users WHERE id = 3;
Plan:
  Filter(id =)  [rows=0 cost=1.0]
    IndexScan(users) [idx:id]  [rows=0 cost=1.0]
Result:
id | age | score
--- | --- | ---
3 | 35 | 75
(1 rows)
```

优化器自动选择 IndexScan 而非 SeqScan。

### 投影 + 过滤

```
miniDB> SELECT id FROM users WHERE age > 28;
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

age 列无索引，使用 SeqScan + Filter。

## main.c

```c
int main(int argc, char *argv[]) {
    if (argc > 1) {
        // 处理 --version, --help
    }

    db_context_t *ctx = db_context_create();
    init_sample_data(ctx);  // 加载示例表
    cli_run(ctx);           // 进入交互循环
    db_context_destroy(ctx);
    return 0;
}
```

## 网络协议（设计）

教学版 CLI 通过 stdin/stdout 交互。生产数据库还需要网络协议：

```
客户端 ──TCP──▶ 服务器
                 │
                 ├── 读取查询
                 ├── 解析 + 优化 + 执行
                 └── 返回结果
```

### 简化协议

```
请求: "SELECT * FROM users;\n"
响应: "id|age|score\n1|25|85\n2|30|90\n...\n"
```

> 本章未实现 TCP 服务器，因为教学重点在 SQL 处理流水线。后续可扩展。

## 与工业级 CLI 的差距

| 特性 | miniDB | sqlite3 | psql |
|---|---|---|---|
| 交互模式 | 简单循环 | readline + 历史 | readline + 历史 + 自动补全 |
| 多行输入 | 按 `;` 分割 | 智能分割 | 智能分割 |
| 输出格式 | 固定表格 | 多种模式 | 多种模式 |
| 错误处理 | 简单打印 | 详细位置 | 详细位置 + 提示 |
| 事务支持 | 无 | BEGIN/COMMIT | BEGIN/COMMIT |
| 脚本执行 | 无 | `.read file` | `\i file` |

## 文件清单

| 文件 | 职责 |
|---|---|
| `cli.h/c` | CLI 交互循环 + DB 上下文 |
| `main.c` | 入口 + 示例数据初始化 |

## 下一步

CLI 就绪后，下一章进行**集成测试与性能压测**，验证各模块协作正确性并测量吞吐量。