# 章10：CLI 与网络协议

> **一句话总结**：CLI（Command Line Interface，命令行界面）是用户与数据库交互的"窗户"。本章实现一个交互式 CLI，把前面所有章节的模块（Parser → Optimizer → Executor）串成一条完整流水线，让用户能像使用 `sqlite3` 那样敲 SQL 语句、看结果。
>
> **本章你将学到**：
> 1. 什么是 REPL，为什么数据库 CLI 几乎都用 REPL 模式
> 2. 点命令（`.help`、`.tables`、`.exit`）与 SQL 语句的区分逻辑
> 3. 多行输入的缓冲区管理与分号检测
> 4. `db_context_t` 全局上下文如何连接 Catalog 与执行器数据源
> 5. 从键盘输入到屏幕输出的完整执行流程
> 6. 教学版为什么不做 TCP 网络协议，真实数据库的协议长什么样
> 7. miniDB CLI 与 sqlite3、psql 的差距分析
>
> **前置章节**：第1～9章（存储、Parser、AST、Optimizer、Executor 等已全部完成）。
>
> **面向读者**：第一次写数据库 CLI 的新手。本章假设你已经会 C 语言基本语法（`struct`、`fgets`、`strcmp`、指针），但不假设你用过 `sqlite3` 或 `pssql`。

---

## 目录

- [1. CLI 的作用：用户与数据库的桥梁](#1-cli-的作用用户与数据库的桥梁)
- [2. 交互式 REPL：Read-Eval-Print Loop](#2-交互式-replread-eval-print-loop)
- [3. 命令解析：点命令 vs SQL 语句](#3-命令解析点命令-vs-sql-语句)
- [4. 多行输入处理：缓冲区与分号检测](#4-多行输入处理缓冲区与分号检测)
- [5. DB Context：全局上下文设计](#5-db-context全局上下文设计)
- [6. 执行流程：从输入到输出](#6-执行流程从输入到输出)
- [7. 代码逐行解读](#7-代码逐行解读)
- [8. 示例数据：users 与 orders 表](#8-示例数据users-与-orders-表)
- [9. 网络协议设计](#9-网络协议设计)
- [10. 与真实 CLI 对比](#10-与真实-cli-对比)
- [11. 习题](#11-习题)
- [附录 A：完整文件清单](#附录-a完整文件清单)
- [附录 B：常见报错与排查](#附录-b常见报错与排查)
- [附录 C：术语表](#附录-c术语表)

---

## 1. CLI 的作用：用户与数据库的桥梁

### 1.1 为什么需要 CLI

数据库本身是一个"黑盒"：它内部有存储引擎、查询优化器、执行器，但这些组件都不会自己跟用户说话。用户没法直接调用 `executor_run()`，因为：

| 问题 | 没有 CLI 时 | 有 CLI 后 |
|---|---|---|
| 用户怎么发 SQL？ | 只能在 C 代码里硬编码 `process_sql("SELECT ...")`，每次改查询都要重新编译 | 在终端敲 `SELECT * FROM users;` 即可 |
| 用户怎么看结果？ | 只能 `printf` 调试，没有统一格式 | CLI 自动打印表格、行数 |
| 用户怎么探索？ | 没有办法列出有哪些表、有哪些命令 | `.tables`、`.help` 一敲就知道 |
| 用户怎么快速试错？ | 改一行 SQL 要重编重跑，秒级延迟 | 敲回车立即执行，毫秒级反馈 |

**一句话**：CLI 把数据库从"一个 C 函数库"变成"一个能对话的程序"。

### 1.2 CLI 在整个系统中的位置

```
┌─────────────────────────────────────────────────────────────┐
│                       用户（人）                              │
│                         │                                    │
│                         ▼ 敲键盘                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │                    CLI 层                            │   │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │   │
│  │  │ REPL 循环 │─▶│ 命令分发 │─▶│  process_sql()   │  │   │
│  │  └──────────┘  └──────────┘  └──────────────────┘  │   │
│  └─────────────────────────┬───────────────────────────┘   │
│                            │                                │
│                            ▼ 调用                            │
│  ┌─────────────────────────────────────────────────────┐   │
│  │                  SQL 处理流水线                       │   │
│  │  Parser ─▶ Optimizer ─▶ Executor ─▶ ResultSet       │   │
│  └─────────────────────────────────────────────────────┘   │
│                            │                                │
│                            ▼ 打印                            │
│                       终端屏幕（人眼）                       │
└─────────────────────────────────────────────────────────────┘
```

CLI 是**最上层**：它不关心 SQL 怎么解析、怎么优化、怎么执行，它只负责"把用户的话传下去，把结果传回来"。

### 1.3 CLI 的三大职责

| 职责 | 说明 | 对应代码 |
|---|---|---|
| **输入** | 从 stdin 读取用户键入的字符，处理多行、分号 | `cli_run()` 里的 `fgets` 循环 |
| **分发** | 判断是点命令还是 SQL，分别走不同路径 | `if (buf[0] == '.')` 分支 |
| **输出** | 把 ResultSet、Plan、错误信息格式化打印 | `result_set_print()`、`plan_print()` |

### 1.4 教学版 CLI 的设计取舍

我们刻意做得**简单**，目的是让你看懂核心逻辑：

| 特性 | 我们做 | 我们不做 | 为什么 |
|---|---|---|---|
| 输入方式 | `fgets` 读 stdin | readline 库（历史、补全） | 多一个依赖，多一份复杂度 |
| 多行输入 | 按 `;` 拼接 | 智能判断括号、字符串 | 教学版 SQL 简单，不需要 |
| 输出格式 | 固定 `col | col` 表格 | JSON、CSV、竖排等模式 | 一种格式够用 |
| 错误处理 | `printf("Error: ...")` | 带行号、列号、上下文 | Parser 章节已讲过位置信息 |
| 脚本执行 | 无 | `.read file.sql` | 可作为习题 |

> **新手提示**：如果你用过 `sqlite3`，会发现它有 `.mode csv`、`.read`、`.schema` 等几十个点命令。我们只实现 3 个，但**机制完全相同**——读完本章你就能看懂 sqlite3 的 shell.c。

---

## 2. 交互式 REPL：Read-Eval-Print Loop

### 2.1 什么是 REPL

REPL 是 **R**ead-**E**val-**P**rint **L**oop 的缩写，中文叫"读取-求值-打印-循环"。它是一种交互式程序运行方式：

```
   ┌──────────────────────────────────────────┐
   │                 REPL 循环                 │
   │                                          │
   │   ┌─────────┐                            │
   │   │  Read   │  读取用户输入的一行（或多行）│
   │   └────┬────┘                            │
   │        ▼                                 │
   │   ┌─────────┐                            │
   │   │  Eval   │  求值：解析 + 优化 + 执行   │
   │   └────┬────┘                            │
   │        ▼                                 │
   │   ┌─────────┐                            │
   │   │  Print  │  打印结果或错误             │
   │   └────┬────┘                            │
   │        ▼                                 │
   │   ┌─────────┐                            │
   │   │  Loop   │  回到 Read，等待下一次输入  │
   │   └─────────┘                            │
   │                                          │
   └──────────────────────────────────────────┘
```

**生活中的类比**：计算器就是 REPL——你输入 `1+2`，它输出 `3`，然后等你下一个输入。数据库 CLI 也是一样，只是"求值"变成了"执行 SQL"。

### 2.2 常见的 REPL 程序

| 程序 | 语言/系统 | 提示符 | 你可能用过 |
|---|---|---|---|
| `python` | Python | `>>>` | 是 |
| `node` | JavaScript | `>` | 是 |
| `sqlite3` | SQLite | `sqlite>` | 也许 |
| `psql` | PostgreSQL | `postgres=#` | 也许 |
| `mysql` | MySQL | `mysql>` | 也许 |
| **miniDB** | 本章 | `miniDB>` | 即将 |

它们**全是 REPL**。掌握了 miniDB 的 REPL，你就理解了所有这些工具的核心交互模式。

### 2.3 为什么用分号分隔

**问题**：用户什么时候算"输入完了"？回车？不行，因为 SQL 可以跨>多行：

```sql
SELECT id,
       age,
       score
FROM users
WHERE age > 25
  AND score > 70;
```

如果按回车就执行，第一行 `SELECT id,` 会被当成完整语句，立刻报错。

**解法**：用 `;` 作为语句结束符。

| 方案 | 优点 | 缺点 | 谁在用 |
|---|---|---|---|
| 回车即执行 | 简单 | 不支持多行 | Python REPL |
| 分号结束 | 支持多行，符合 SQL 习惯 | 用户必须记得打 `;` | sqlite3、psql、mysql |
| 空行结束 | 不用打 `;` | 容易误触发 | 一些教学型解释器 |

SQL 历史上就用 `;` 分隔语句（SQL 标准如此），所以数据库 CLI 普遍采用分号方案。miniDB 遵循这一惯例。

### 2.4 REPL 的伪代码

```
function REPL:
    打印欢迎信息
    while True:                          # Loop
        line = read(stdin)               # Read
        if line 是 ".exit":
            break
        if line 以 ";" 结尾:
            result = evaluate(line)      # Eval
            print(result)                # Print
    打印 "Bye!"
```

miniDB 的 `cli_run()` 就是这个伪代码的 C 实现，后面会逐行讲解。

### 2.5 提示符的设计

提示符（prompt）是 REPL 给用户的"我在等你输入"的信号：

```
miniDB> SELECT * FROM users;
        └────┘
        提示符
```

miniDB 用固定的 `miniDB> `。真实工具更讲究：

| 工具 | 提示符 | 含义 |
|---|---|---|
| sqlite3 | `sqlite>` | 固定 |
| psql | `mydb=#` | 带数据库名，超级用户用 `#`，普通用户用 `>` |
| psql（多行） | `mydb-#` | 横线表示语句还没结束 |
| mysql | `mysql>` | 固定 |

> **新手提示**：psql 的 `=` vs `-` 是很贴心的设计——用户一眼看出"我现在的 SQL 完没完"。miniDB 没做这个，作为习题留给你。

---

## 3. 命令解析：点命令 vs SQL 语句

### 3.1 两种命令

miniDB 的 CLI 要处理两类输入：

| 类型 | 例子 | 特点 | 处理方式 |
|---|---|---|---|
| **点命令** | `.help`、`.tables`、`.exit` | 以 `.` 开头，**立即执行**，不需要分号 | CLI 内部直接处理 |
| **SQL 语句** | `SELECT * FROM users;` | 以 SQL 关键字开头，**需要分号**才执行 | 交给 `process_sql()` |

**为什么分开**：点命令是"元命令"（meta-command），它操作的是 CLI 本身（列出表、退出、帮助），不是 SQL 查询。SQL 语句操作的是数据库（查数据、插数据）。两者职责完全不同。

### 3.2 分发逻辑

```
用户输入一行
      │
      ▼
┌─────────────────┐
│ 第一个字符是 '.' │
│      ?          │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
   是         否
    │         │
    ▼         ▼
┌────────┐  ┌──────────────────┐
│点命令  │  │ 追加到 SQL 缓冲区 │
│处理    │  │ 末尾是 ';' ?      │
└────────┘  └────────┬─────────┘
                     │
               ┌─────┴─────┐
               │           │
              是           否
               │           │
               ▼           ▼
        ┌──────────┐  ┌──────────────┐
        │执行 SQL  │  │继续读下一行  │
        │清空缓冲区│  │（多行输入）  │
        └──────────┘  └──────────────┘
```

### 3.3 点命令一览

miniDB 实现了 4 个点命令（含别名）：

| 命令 | 别名 | 作用 | 示例输出 |
|---|---|---|---|
| `.help` | 无 | 显示帮助 | `miniDB Commands: ...` |
| `.tables` | 无 | 列出所有表 | `users (5 rows, 3 cols, index on id)` |
| `.exit` | `.quit` | 退出 CLI | `Bye!` |

**对比 sqlite3 的点命令**（节选）：

| sqlite3 命令 | 作用 | miniDB 有无 |
|---|---|---|
| `.help` | 帮助 | ✅ |
| `.tables` | 列表 | ✅ |
| `.exit` | 退出 | ✅ |
| `.schema` | 显示建表语句 | ❌ |
| `.mode csv` | 切换输出格式 | ❌ |
| `.read file` | 执行脚本文件 | ❌ |
| `.indexes` | 列出索引 | ❌ |
| `.timer on` | 开启计时 | ❌ |

miniDB 只实现了最核心的 3 个，但**分发机制和 sqlite3 一模一样**：都是看第一个字符是不是 `.`。

### 3.4 点命令的实现

```c
if (buf[0] == '.') {
    if (strcmp(buf, ".exit") == 0 || strcmp(buf, ".quit") == 0)
        break;                              // 退出循环
    else if (strcmp(buf, ".help") == 0)
        print_help();                       // 打印帮助
    else if (strcmp(buf, ".tables") == 0)
        list_tables(ctx);                   // 列出表
    else
        printf("Unknown command: %s (try .help)\n", buf);
    len = 0;                                // 清空缓冲区
    continue;                               // 回到循环开头
}
```

**关键点**：
1. `buf[0] == '.'` —— 只看第一个字符，快速分流
2. `strcmp` 精确匹配 —— 不支持缩写（sqlite3 支持 `.e` 代表 `.exit`）
3. `len = 0` —— 点命令执行完必须清空缓冲区，否则残留字符会污染下一条 SQL
4. `continue` —— 不走后面的 SQL 逻辑

### 3.5 未知命令的处理

如果用户输入 `.foo`，miniDB 会：

```
miniDB> .foo
Unknown command: .foo (try .help)
```

**设计原则**：未知命令不崩溃，给出提示。这比"直接忽略"或"段错误"都好。

> **新手提示**：很多新手写 CLI 时，遇到未知命令会 `exit(1)` 直接退出程序。这很糟糕——用户打错一个命令就被踢出，体验极差。正确做法是打印错误，继续循环。

### 3.6 SQL 语句的识别

不是点命令的输入，一律视为 SQL：

```c
// 走到这里说明 buf[0] != '.'
if (buf[len - 1] == ';') {
    // 末尾有分号，是一条完整 SQL
    buf[--len] = '\0';          // 去掉分号
    // 去掉尾部空白
    while (len > 0 && (buf[len-1] == ' ' || buf[len-1] == '\t'))
        buf[--len] = '\0';
    if (len > 0)
        process_sql(ctx, buf);  // 交给 SQL 处理器
    len = 0;                    // 清空缓冲区
}
// else: 没有分号，继续读下一行（多行输入）
```

**注意**：miniDB 不检查 SQL 关键字（`SELECT`、`INSERT` 等），直接把字符串传给 Parser。如果用户输入 `hello world;`，Parser 会返回错误，CLI 打印 `Error: invalid SQL`。这是合理的——CLI 不该懂 SQL 语法，那是 Parser 的活。

---

## 4. 多行输入处理：缓冲区与分号检测

### 4.1 为什么需要多行输入

真实 SQL 往往很长：

```sql
SELECT u.id, u.age, o.amount
FROM users u
JOIN orders o ON u.id = o.user_id
WHERE u.age > 25
ORDER BY o.amount DESC;
```

用户希望分行敲，提高可读性。如果 CLI 只支持单行，用户被迫写成一行，体验极差。

### 4.2 缓冲区设计

miniDB 用一个 4096 字节的栈数组作为输入缓冲区：

```c
char buf[4096];   // 输入缓冲区，最多 4KB
int len = 0;      // 当前缓冲区已用长度
```

**为什么 4096**：
- 太小（如 256）：稍长的 SQL 就溢出
- 太大（如 1MB）：浪费栈空间（栈默认 8MB，一个 1MB 数组就占 1/8）
- 4096 是常见的"够用又不浪费"的折中值

> **新手提示**：sqlite3 的缓冲区是动态增长的（`malloc` + `realloc`），没有上限。我们用固定数组是为了简单——教学版 SQL 不会超过 4KB。

### 4.3 多行拼接的原理

关键在于 `fgets` 的写入位置：

```c
fgets(buf + len, sizeof(buf) - len, stdin)
       └──┬──┘
       从 buf 的"已用位置"开始写
```

- 第一次读：`len=0`，写到 `buf[0]`
- 第二次读：`len=20`，写到 `buf[20]`，接在上次后面
- 第三次读：`len=50`，写到 `buf[50]`，继续拼接

**示意图**：

```
第一次输入 "SELECT *\n"
buf: [S][E][L][E][C][T][ ][*][\n][\0]
len: 9  (去掉 \n 后 len=8)

第二次输入 "FROM users;\n"
buf: [S][E][L][E][C][T][ ][*][ ][F][R][O][M][ ][u][s][e][r][s][;][\n][\0]
                                                    └── 从 buf[8] 开始写
len: 19 → 去掉 \n → 18 → 去掉 ; → 17

最终 process_sql 收到: "SELECT * FROM users"
```

### 4.4 换行符的处理

`fgets` 会把 `\n` 也读进缓冲区。我们需要去掉它：

```c
if (len > 0 && buf[len - 1] == '\n')
    buf[--len] = '\0';    // 把 \n 替换成 \0
```

**为什么不能只靠 `\0`**：C 字符串用 `\0` 结尾，但 `fgets` 会在 `\n` 后面加 `\0`。如果不去掉 `\n`，后续 `strcmp(buf, ".exit")` 会失败（因为 `buf` 是 `".exit\n"` 而不是 `".exit"`）。

### 4.5 空行的处理

```c
if (len == 0)
    continue;    // 空行，直接跳过
```

用户连敲两个回车，`len` 为 0，不应触发任何操作。

### 4.6 分号检测的细节

```c
if (buf[len - 1] == ';') {
    buf[--len] = '\0';    // 去掉分号
    // 去掉分号前的空白（如 "SELECT 1 ;" → "SELECT 1"）
    while (len > 0 && (buf[len-1] == ' ' || buf[len-1] == '\t'))
        buf[--len] = '\0';
    ...
}
```

**为什么要去尾部空白**：用户可能输入 `SELECT 1 ;`（分号前有空格）。去掉分号后变成 `SELECT 1 `（尾部有空格），Parser 可能不喜欢。所以再扫一遍去掉空格和 tab。

### 4.7 多行输入完整示例

**用户输入**：
```
miniDB> SELECT id,
       age
FROM users
WHERE age > 28;
```

**缓冲区变化**：

| 步骤 | 用户输入 | buf 内容（\n 已去） | len | 末尾是 `;`? | 动作 |
|---|---|---|---|---|---|
| 1 | `SELECT id,` | `SELECT id,` | 11 | 否 | 继续读 |
| 2 | `age` | `SELECT id,age` | 14 | 否 | 继续读 |
| 3 | `FROM users` | `SELECT id,ageFROM users` | 24 | 否 | 继续读 |

**问题**：步骤 2 和 3 之间少了空格！`age` 和 `FROM` 粘在一起了。

**原因**：`fgets` 读到的 `age` 没有 `\n`（因为用户敲的是 `age\n`，但 `fgets` 会包含 `\n`）。等等，让我们重新看——实际上 `fgets` 会读 `\n`，去掉 `\n` 后是 `age`，拼到 `SELECT id,` 后变成 `SELECT id,age`。

**修复**：这确实是 miniDB 的一个**简化缺陷**。真实 sqlite3 会在每行末尾补一个空格。miniDB 没做，所以多行输入时行尾需要手动加空格：

```
miniDB> SELECT id,
       age
       FROM users
       WHERE age > 28;
```

如果写成 `SELECT id, age FROM users WHERE age > 28;`（一行），就没问题。

> **新手提示**：这是教学版的取舍。真实 CLI 会更仔细地处理空白。你可以把这当作习题——在每行拼接时自动补一个空格。

### 4.8 缓冲区溢出保护

```c
fgets(buf + len, sizeof(buf) - len, stdin)
                   └──────┬──────┘
                   最多读 (4096 - len) 字节
```

`fgets` 的第二个参数是"最多读多少字节"。我们传 `sizeof(buf) - len`，保证不会写超 `buf` 的末尾。如果用户输入超过 4KB，`fgets` 会截断，多余的留在 stdin 里下次读。

**但有个隐患**：`len += strlen(buf + len)` 之后如果 `len == 4095`，下次 `fgets(buf + 4095, 1, stdin)` 只能读 0 字节（因为要留一个位置给 `\0`）。这时缓冲区满了，但还没看到分号，会卡住。教学版不处理这个边界，真实版会报 `buffer overflow` 错误。

---

## 5. DB Context：全局上下文设计

### 5.1 为什么需要全局上下文

CLI 运行期间，多个模块需要访问同一份数据：

| 模块 | 需要什么 |
|---|---|
| Optimizer（优化器） | 表的统计信息（行数、有没有索引）→ Catalog |
| Executor（执行器） | 表的实际数据（行、列）→ `exec_table_t` |
| CLI（点命令 `.tables`） | 两者都要 |

如果每个模块各自持有一份，数据会重复、不一致。所以用一个 `db_context_t` 统一管理。

### 5.2 db_context_t 的定义

```c
typedef struct {
    catalog_t *catalog;           // 表统计信息（给优化器用）
    exec_table_t tables[16];      // 实际数据（给执行器用）
    int num_tables;               // 当前有多少张表
} db_context_t;
```

**字段解读**：

| 字段 | 类型 | 作用 | 谁用 |
|---|---|---|---|
| `catalog` | `catalog_t *` | 表的"元数据"：行数、索引信息 | Optimizer |
| `tables` | `exec_table_t[16]` | 表的"真数据"：行数组、列元信息 | Executor |
| `num_tables` | `int` | 已加载的表数 | CLI（`.tables`）、`add_table` |

**为什么最多 16 张表**：固定数组，简单。教学版两张表（users、orders）足够。真实数据库用动态链表或哈希表。

### 5.3 Catalog 与 Tables 的关系

```
db_context_t
├── catalog (catalog_t *)
│   ├── entries["users"]  → {num_rows=5, has_index=true, index_col="id"}
│   └── entries["orders"] → {num_rows=4, has_index=true, index_col="order_id"}
│
└── tables[16] (exec_table_t[])
    ├── tables[0] = {name="users",  rows=[...], num_rows=5, cols=[id,age,score]}
    └── tables[1] = {name="orders", rows=[...], num_rows=4, cols=[order_id,user_id,amount]}
```

**Catalog 是"目录页"**：只存摘要信息（多少行、有没有索引），不存真数据。
**Tables 是"正文"**：存实际的行和列。

优化器只看目录页（快），执行器才翻正文（慢）。这是数据库的经典设计——**统计信息与数据分离**。

### 5.4 创建与销毁

```c
db_context_t *db_context_create(void) {
    db_context_t *ctx = calloc(1, sizeof(db_context_t));
    ctx->catalog = catalog_create();
    return ctx;
}
```

- `calloc` 把结构体清零（`num_tables=0`，`tables` 全空）
- `catalog_create()` 创建空的 Catalog
- 返回指针，调用者负责销毁

```c
void db_context_destroy(db_context_t *ctx) {
    if (!ctx) return;            // 防御性检查
    catalog_destroy(ctx->catalog);
    free(ctx);
}
```

- 先销毁 Catalog
- 再 free 整个结构体
- `tables` 数组里的行数据是 `main.c` 的静态数组，不需要在这里 free

### 5.5 添加表

```c
void db_context_add_table(db_context_t *ctx, const char *name,
                          exec_row_t *rows, int num_rows,
                          exec_col_meta_t *cols, int num_cols,
                          bool has_index, const char *index_col) {
    if (ctx->num_tables >= 16) return;     // 满了，忽略
    exec_table_t *t = &ctx->tables[ctx->num_tables++];
    strncpy(t->name, name, EXEC_MAX_NAME - 1);
    t->rows = rows;                        // 指针赋值，不拷贝
    t->num_rows = num_rows;
    t->num_cols = num_cols;
    for (int i = 0; i < num_cols && i < EXEC_MAX_COLS; i++)
        t->cols[i] = cols[i];              // 列元信息拷贝
    catalog_add_table(ctx->catalog, name, num_rows,
                      num_rows / 10 + 1, has_index, index_col);
}
```

**关键设计**：
1. `t->rows = rows` 是**指针赋值**，不拷贝数据。`rows` 指向 `main.c` 里的静态数组，生命周期是整个程序，所以安全。
2. 同时往 Catalog 里加一份摘要：`num_rows / 10 + 1` 是估算的"每页行数"（教学版固定公式，真实版会统计）。
3. `num_tables++` 后自增，下次加表写到下一个槽位。

### 5.6 db_context_t 的生命周期

```
main()
  │
  ├── ctx = db_context_create()     ← 创建
  │
  ├── init_sample_data(ctx)         ← 加载 users、orders
  │     │
  │     └── db_context_add_table(ctx, "users", ...)
  │         ├── tables[0] = users 数据
  │         └── catalog.entries["users"] = 统计信息
  │
  ├── cli_run(ctx)                  ← 交互循环，反复用 ctx
  │     │
  │     └── process_sql(ctx, sql)
  │           ├── optimizer_optimize(stmt, ctx->catalog)  ← 读 catalog
  │           └── executor_run(plan, ctx->tables, ...)    ← 读 tables
  │
  └── db_context_destroy(ctx)       ← 销毁
```

**一句话**：`ctx` 是整个程序的"数据库实例"，从创建到销毁贯穿始终。

---

## 6. 执行流程：从输入到输出

### 6.1 完整流程图

```
用户敲键盘: "SELECT * FROM users;"
           │
           ▼
┌──────────────────────────────────┐
│ cli_run() 主循环                 │
│  ├── fgets 读 stdin              │
│  ├── 去掉 \n                     │
│  ├── 判断 buf[0] == '.'?  否     │
│  └── 判断末尾 == ';'?    是      │
│      └── 去掉 ; 和空白           │
└──────────────┬───────────────────┘
               │
               ▼  sql = "SELECT * FROM users"
┌──────────────────────────────────┐
│ process_sql(ctx, sql)            │
│                                  │
│ ① Parser                        │
│   p = parser_create(sql)         │
│   stmt = parser_parse(p)         │
│   → ast_stmt_t {type=SELECT, ...}│
│   → 打印 "AST: SELECT * FROM ..."│
│                                  │
│ ② Optimizer                     │
│   plan = optimizer_optimize(     │
│       stmt, ctx->catalog)        │
│   → plan_node_t {SeqScan(users)} │
│   → 打印 "Plan: ..."             │
│                                  │
│ ③ Executor                      │
│   rs = executor_run(             │
│       plan, ctx->tables, ...)    │
│   → result_set_t {5 行数据}      │
│   → 打印 "Result: ..."           │
│                                  │
│ ④ Cleanup                       │
│   plan_destroy(plan)             │
│   free(stmt)                     │
│   parser_destroy(p)              │
└──────────────────────────────────┘
               │
               ▼
屏幕显示:
  AST: SELECT * FROM users
  Plan:
    SeqScan(users)  [rows=5 cost=1.0]
  Result:
  id | age | score
  --- | --- | ---
  1 | 25 | 85
  ...
  (5 rows)
```

### 6.2 四个阶段详解

#### 阶段 ①：Parser（解析）

```c
parser_t *p = parser_create(sql);        // 创建解析器，绑定 SQL 字符串
ast_stmt_t *stmt = parser_parse(p);      // 解析，返回 AST
```

- **输入**：SQL 字符串 `"SELECT * FROM users"`
- **输出**：AST（抽象语法树），一个 `ast_stmt_t` 结构体
- **失败处理**：`stmt == NULL` 表示语法错误，打印 `Error: invalid SQL` 并返回

```c
if (!stmt) {
    printf("Error: invalid SQL\n");
    parser_destroy(p);
    return;
}
```

**打印 AST**：`ast_print(stmt)` 把 AST 反向打印成可读形式，方便调试：
```
AST: SELECT * FROM users
```

#### 阶段 ②：Optimizer（优化）

```c
plan_node_t *plan = optimizer_optimize(stmt, ctx->catalog);
```

- **输入**：AST + Catalog（统计信息）
- **输出**：执行计划 `plan_node_t`（一棵树，如 `SeqScan` 或 `IndexScan`）
- **失败处理**：`plan == NULL` 表示优化失败

优化器会根据 Catalog 里的统计信息（行数、索引）选择最优计划。例如 `WHERE id = 3` 且 `id` 有索引，就选 `IndexScan` 而非 `SeqScan`。

**打印 Plan**：
```
Plan:
  SeqScan(users)  [rows=5 cost=1.0]
```

#### 阶段 ③：Executor（执行）

```c
if (stmt->type == AST_SELECT) {
    result_set_t *rs = executor_run(plan, ctx->tables, ctx->num_tables);
    printf("Result:\n");
    result_set_print(rs);
    result_set_destroy(rs);
}
```

- **输入**：Plan + Tables（实际数据）
- **输出**：`result_set_t`（结果集，包含行数据）
- **按语句类型分支**：
  - `AST_SELECT`：执行并打印结果表
  - `AST_INSERT`：打印 `INSERT OK`
  - `AST_DELETE`：打印 `DELETE OK`
  - `AST_CREATE`：打印 `CREATE OK`

> **新手提示**：教学版的 INSERT/DELETE/CREATE 其实**没有真正执行**（没有修改 `ctx->tables`），只打印成功消息。这是 Phase 1 的简化——Phase 2 会实现真正的写操作。

#### 阶段 ④：Cleanup（清理）

```c
plan_destroy(plan);    // 释放执行计划
free(stmt);            // 释放 AST
parser_destroy(p);     // 释放解析器
```

每条 SQL 执行完都要释放内存，否则内存泄漏。C 没有 GC，必须手动管理。

### 6.3 错误处理流程

```
process_sql(ctx, sql)
        │
        ▼
   parser_parse(p)
        │
   ┌────┴────┐
   │         │
 stmt==NULL  stmt 有效
   │         │
   ▼         ▼
 打印 Error  optimizer_optimize()
   │              │
   ▼         ┌────┴────┐
 return      │         │
          plan==NULL  plan 有效
              │         │
              ▼         ▼
          打印 Error   executor_run()
              │         │
              ▼         ▼
          return      打印 Result
                          │
                          ▼
                      cleanup
```

**原则**：任何一步失败都立即返回，不继续执行后续步骤。避免"解析失败还去优化"的荒谬情况。

### 6.4 一次完整交互的时间线

```
t=0ms    用户敲 "SELECT * FROM users;"
t=1ms    fgets 返回
t=1ms    去掉 \n 和 ;
t=1ms    调用 process_sql
t=2ms    Parser 完成，得到 AST
t=2ms    打印 "AST: SELECT * FROM users"
t=3ms    Optimizer 完成，得到 Plan (SeqScan)
t=3ms    打印 "Plan: ..."
t=4ms    Executor 扫描 5 行
t=4ms    打印 "Result: ..."
t=4ms    打印表格
t=5ms    cleanup
t=5ms    回到 "miniDB> " 提示符
```

整个查询 5ms，其中 Parser 1ms、Optimizer 1ms、Executor 1ms、打印 2ms。教学版数据量小，瓶颈在 I/O（打印）而非计算。

---

## 7. 代码逐行解读

### 7.1 cli.h —— 头文件

```c
#ifndef MINIDB_CLI_H
#define MINIDB_CLI_H

#include "executor.h"
#include "catalog.h"

typedef struct {
    catalog_t *catalog;
    exec_table_t tables[16];
    int num_tables;
} db_context_t;

db_context_t *db_context_create(void);
void          db_context_destroy(db_context_t *ctx);
void          db_context_add_table(db_context_t *ctx, const char *name,
                                    exec_row_t *rows, int num_rows,
                                    exec_col_meta_t *cols, int num_cols,
                                    bool has_index, const char *index_col);

void cli_run(db_context_t *ctx);

#endif
```

**逐行解读**：

| 行 | 代码 | 作用 |
|---|---|---|
| 1-2 | `#ifndef/#define` | 头文件保护，防止重复包含 |
| 4-5 | `#include` | 引入 `executor.h`（拿 `exec_table_t` 等定义）和 `catalog.h`（拿 `catalog_t`） |
| 7-11 | `typedef struct` | 定义 `db_context_t`，见第5章 |
| 13 | `db_context_create` | 声明：创建上下文 |
| 14 | `db_context_destroy` | 声明：销毁上下文 |
| 15-18 | `db_context_add_table` | 声明：往上下文里加表 |
| 20 | `cli_run` | 声明：进入 CLI 主循环 |
| 22 | `#endif` | 头文件保护结束 |

**设计要点**：
- 头文件只放**声明**（函数原型、类型定义），不放实现
- `db_context_t` 在头文件里定义，因为其他模块（main.c）要用
- `cli_run` 只暴露一个函数，CLI 内部的 `process_sql`、`print_help` 是 `static` 的，不对外

### 7.2 cli.c —— 实现

#### 7.2.1 头部 include

```c
#include "cli.h"        // 自己的头文件
#include "parser.h"     // parser_create, parser_parse
#include "ast.h"        // ast_stmt_t, ast_print
#include "optimizer.h"  // optimizer_optimize
#include "logical_plan.h" // plan_node_t, plan_print, plan_destroy
#include "executor.h"   // executor_run, result_set_*
#include <stdlib.h>     // calloc, free
#include <string.h>     // strncpy, strcmp, strlen
#include <stdio.h>      // printf, fgets, fflush
```

**依赖关系**：cli.c 依赖几乎所有模块的头文件，因为它是"串联者"。

#### 7.2.2 db_context_create

```c
db_context_t *db_context_create(void) {
    db_context_t *ctx = calloc(1, sizeof(db_context_t));
    ctx->catalog = catalog_create();
    return ctx;
}
```

| 行 | 代码 | 作用 |
|---|---|---|
| 1 | `calloc(1, sizeof(...))` | 分配并清零内存（比 `malloc` 多一步清零） |
| 2 | `catalog_create()` | 创建空的 Catalog |
| 3 | `return ctx` | 返回指针 |

**为什么用 `calloc` 不用 `malloc`**：`calloc` 保证 `num_tables=0`、`tables` 全零，不用手动初始化。

#### 7.2.3 db_context_destroy

```c
void db_context_destroy(db_context_t *ctx) {
    if (!ctx) return;                  // 防御：传 NULL 不崩溃
    catalog_destroy(ctx->catalog);     // 先销毁 Catalog
    free(ctx);                         // 再 free 结构体
}
```

**顺序很重要**：先销毁 Catalog（它可能持有自己的内存），再 free 外层结构体。反过来会导致 Catalog 内部内存泄漏。

#### 7.2.4 db_context_add_table

```c
void db_context_add_table(db_context_t *ctx, const char *name,
                          exec_row_t *rows, int num_rows,
                          exec_col_meta_t *cols, int num_cols,
                          bool has_index, const char *index_col) {
    if (ctx->num_tables >= 16) return;                    // ① 满了
    exec_table_t *t = &ctx->tables[ctx->num_tables++];    // ② 取槽位
    strncpy(t->name, name, EXEC_MAX_NAME - 1);            // ③ 拷表名
    t->rows = rows;                                       // ④ 指针赋值
    t->num_rows = num_rows;
    t->num_cols = num_cols;
    for (int i = 0; i < num_cols && i < EXEC_MAX_COLS; i++)
        t->cols[i] = cols[i];                             // ⑤ 拷列元信息
    catalog_add_table(ctx->catalog, name, num_rows,
                      num_rows / 10 + 1, has_index, index_col);  // ⑥ 加 Catalog
}
```

| 标号 | 代码 | 作用 |
|---|---|---|
| ① | `if (num_tables >= 16)` | 防止溢出，最多 16 张表 |
| ② | `&ctx->tables[ctx->num_tables++]` | 取当前空槽位，`num_tables` 后自增 |
| ③ | `strncpy` | 拷表名，限制长度防溢出 |
| ④ | `t->rows = rows` | **指针赋值**，不拷贝数据（零拷贝） |
| ⑤ | `for` 循环 | 逐列拷贝列元信息（名字、类型） |
| ⑥ | `catalog_add_table` | 往 Catalog 加一份摘要 |

**`num_rows / 10 + 1` 的含义**：估算每页行数（教学版公式）。真实数据库会统计实际页大小。

#### 7.2.5 process_sql —— 核心处理函数

```c
static void process_sql(db_context_t *ctx, const char *sql) {
    parser_t *p = parser_create(sql);              // ① 创建 Parser
    ast_stmt_t *stmt = parser_parse(p);            // ② 解析

    if (!stmt) {                                   // ③ 语法错误
        printf("Error: invalid SQL\n");
        parser_destroy(p);
        return;
    }

    printf("AST: ");                               // ④ 打印 AST
    ast_print(stmt);

    plan_node_t *plan = optimizer_optimize(stmt, ctx->catalog);  // ⑤ 优化
    if (!plan) {                                   // ⑥ 优化失败
        printf("Error: optimization failed\n");
        free(stmt);
        parser_destroy(p);
        return;
    }

    printf("Plan:\n");                             // ⑦ 打印 Plan
    plan_print(plan, 1);

    if (stmt->type == AST_SELECT) {                // ⑧ 执行
        result_set_t *rs = executor_run(plan, ctx->tables, ctx->num_tables);
        printf("Result:\n");
        result_set_print(rs);
        result_set_destroy(rs);
    } else if (stmt->type == AST_INSERT) {
        printf("INSERT OK (0 rows affected)\n");
    } else if (stmt->type == AST_DELETE) {
        printf("DELETE OK (0 rows affected)\n");
    } else if (stmt->type == AST_CREATE) {
        printf("CREATE OK\n");
    }

    plan_destroy(plan);                            // ⑨ 清理
    free(stmt);
    parser_destroy(p);
}
```

**9 个步骤**：

| 步骤 | 代码 | 阶段 | 失败处理 |
|---|---|---|---|
| ① | `parser_create` | Parser 初始化 | 不会失败 |
| ② | `parser_parse` | 解析 | 返回 NULL |
| ③ | `if (!stmt)` | 错误检查 | 打印 + return |
| ④ | `ast_print` | 调试输出 | — |
| ⑤ | `optimizer_optimize` | 优化 | 返回 NULL |
| ⑥ | `if (!plan)` | 错误检查 | 打印 + return |
| ⑦ | `plan_print` | 调试输出 | — |
| ⑧ | `executor_run` | 执行 | 按语句类型分支 |
| ⑨ | `plan_destroy` 等 | 清理 | — |

**`static` 关键字**：`process_sql` 是 `static` 的，只在 cli.c 内部可见。外部模块不能调用它——这是封装。

#### 7.2.6 print_help

```c
static void print_help(void) {
    printf("miniDB Commands:\n");
    printf("  SQL statements end with ';'\n");
    printf("  .help     Show this help\n");
    printf("  .tables   List tables\n");
    printf("  .exit     Quit\n");
    printf("\nSupported SQL:\n");
    printf("  SELECT * | col1, col2 FROM table [WHERE col OP value [AND ...]]\n");
    printf("  INSERT INTO table (cols) VALUES (vals)\n");
    printf("  DELETE FROM table [WHERE ...]\n");
    printf("  CREATE TABLE table (col TYPE [PRIMARY KEY], ...)\n");
}
```

纯输出，无逻辑。帮助文本里列出了所有支持的 SQL 形式，是新手最该先看的。

#### 7.2.7 list_tables

```c
static void list_tables(db_context_t *ctx) {
    for (int i = 0; i < ctx->num_tables; i++) {
        printf("%s (%d rows, %d cols", ctx->tables[i].name,
               ctx->tables[i].num_rows, ctx->tables[i].num_cols);
        const catalog_entry_t *e = catalog_lookup(ctx->catalog, ctx->tables[i].name);
        if (e && e->has_index)
            printf(", index on %s", e->index_col);
        printf(")\n");
    }
}
```

**逻辑**：
1. 遍历所有表
2. 打印表名、行数、列数
3. 查 Catalog 看有没有索引，有就额外打印 `index on xxx`
4. 换行

**输出示例**：
```
users (5 rows, 3 cols, index on id)
orders (4 rows, 3 cols, index on order_id)
```

#### 7.2.8 cli_run —— 主循环

```c
void cli_run(db_context_t *ctx) {
    char buf[4096];                          // 输入缓冲区
    int len = 0;                             // 已用长度

    printf("miniDB v0.1  (type .help for help)\n");  // 欢迎语

    while (1) {                              // REPL 循环
        printf("miniDB> ");                  // 提示符
        fflush(stdout);                      // 强制刷新（见下文）

        if (!fgets(buf + len, sizeof(buf) - len, stdin))  // 读输入
            break;                           // EOF (Ctrl+D)

        len += strlen(buf + len);            // 更新长度

        if (len > 0 && buf[len - 1] == '\n') // 去换行符
            buf[--len] = '\0';

        if (len == 0)                        // 空行
            continue;

        if (buf[0] == '.') {                 // 点命令
            if (strcmp(buf, ".exit") == 0 || strcmp(buf, ".quit") == 0)
                break;
            else if (strcmp(buf, ".help") == 0)
                print_help();
            else if (strcmp(buf, ".tables") == 0)
                list_tables(ctx);
            else
                printf("Unknown command: %s (try .help)\n", buf);
            len = 0;                         // 清缓冲区
            continue;
        }

        if (buf[len - 1] == ';') {           // SQL 完结
            buf[--len] = '\0';               // 去分号
            while (len > 0 && (buf[len-1] == ' ' || buf[len-1] == '\t'))
                buf[--len] = '\0';           // 去尾部空白
            if (len > 0)
                process_sql(ctx, buf);       // 执行
            len = 0;                         // 清缓冲区
        }
        // else: 没分号，继续读下一行
    }

    printf("Bye!\n");                        // 退出语
}
```

**`fflush(stdout)` 的作用**：某些环境下，`printf` 后不立即刷新缓冲区，提示符 `miniDB> ` 不会显示。`fflush` 强制刷新，确保用户看到提示符。

**`fgets` 返回 NULL**：用户按 Ctrl+D（EOF）时，`fgets` 返回 NULL，`break` 退出循环。

**`while (1)` 的退出**：只有两个出口——`.exit` 命令或 EOF。没有 `return` 在循环中间，逻辑清晰。

### 7.3 main.c —— 程序入口

#### 7.3.1 头部

```c
#include "cli.h"       // db_context_*, cli_run
#include "executor.h"  // exec_row_t, exec_col_meta_t
#include <stdio.h>     // printf
#include <string.h>    // strcpy

static const char *VERSION = "0.1.0";        // 版本号

static exec_row_t users_rows[5];             // users 表的 5 行
static exec_col_meta_t users_cols[3];        // users 表的 3 列元信息
static exec_row_t orders_rows[4];            // orders 表的 4 行
static exec_col_meta_t orders_cols[3];       // orders 表的 3 列元信息
```

**为什么用 `static` 全局数组**：
1. 生命周期是整个程序，`db_context_add_table` 存指针也安全
2. `static` 限制文件作用域，不污染其他 .c 文件
3. 放在 BSS 段（未初始化数据），不占可执行文件体积

#### 7.3.2 init_sample_data

```c
static void init_sample_data(db_context_t *ctx) {
    // ① users 表列名
    strcpy(users_cols[0].name, "id");
    strcpy(users_cols[1].name, "age");
    strcpy(users_cols[2].name, "score");

    // ② users 表数据
    int users_data[5][3] = {
        {1, 25, 85}, {2, 30, 90}, {3, 35, 75}, {4, 28, 95}, {5, 40, 60}
    };
    for (int i = 0; i < 5; i++) {
        users_rows[i].num_cols = 3;
        users_rows[i].values[0] = users_data[i][0];
        users_rows[i].values[1] = users_data[i][1];
        users_rows[i].values[2] = users_data[i][2];
    }

    // ③ 注册到 ctx
    db_context_add_table(ctx, "users", users_rows, 5,
                         users_cols, 3, true, "id");

    // ④ orders 表（同上，略）
    ...
}
```

**步骤**：
1. 设置列名（`id`、`age`、`score`）
2. 用二维数组初始化数据，再拷到 `users_rows`
3. 调用 `db_context_add_table` 注册（带索引 `id`）
4. orders 表同理

#### 7.3.3 print_help（main.c 的）

```c
static void print_help(void) {
    printf("miniDB v%s - Educational RDBMS\n", VERSION);
    printf("\n");
    printf("Phase 1: Building a database from scratch\n");
    printf("Chapters 1-10 complete\n");
    printf("\n");
    printf("Usage:\n");
    printf("  minidb              Start interactive CLI\n");
    printf("  minidb --version    Show version\n");
    printf("  minidb --help       Show this help\n");
}
```

这是**命令行参数**的帮助（`--help`），和 CLI 内部的 `.help` 不同。两个 `print_help` 互不干扰（都是 `static`）。

#### 7.3.4 main 函数

```c
int main(int argc, char *argv[]) {
    // ① 处理命令行参数
    if (argc > 1) {
        if (strcmp(argv[1], "--version") == 0) {
            printf("miniDB v%s\n", VERSION);
            return 0;
        }
        if (strcmp(argv[1], "--help") == 0) {
            print_help();
            return 0;
        }
    }

    // ② 创建上下文
    db_context_t *ctx = db_context_create();
    // ③ 加载示例数据
    init_sample_data(ctx);
    // ④ 进入 CLI
    cli_run(ctx);
    // ⑤ 清理
    db_context_destroy(ctx);
    return 0;
}
```

**5 个步骤**：

| 步骤 | 代码 | 作用 |
|---|---|---|
| ① | `if (argc > 1)` | 处理 `--version`、`--help`，处理后 `return 0` 退出 |
| ② | `db_context_create` | 创建数据库上下文 |
| ③ | `init_sample_data` | 加载 users、orders 两张表 |
| ④ | `cli_run` | 进入 REPL 循环（用户退出前一直在这里） |
| ⑤ | `db_context_destroy` | 清理资源 |

**`argc > 1` 的含义**：`argc` 是参数个数，程序名本身算一个。`minidb` 时 `argc=1`，`minidb --version` 时 `argc=2`。所以 `argc > 1` 表示有额外参数。

### 7.4 函数调用关系

```
main()
  ├── print_help()              (如果 --help)
  ├── db_context_create()
  │     └── catalog_create()
  ├── init_sample_data()
  │     └── db_context_add_table()  (×2)
  │           └── catalog_add_table()
  ├── cli_run()
  │     ├── [循环] fgets()
  │     ├── print_help()        (如果 .help)
  │     ├── list_tables()
  │     │     └── catalog_lookup()
  │     └── process_sql()
  │           ├── parser_create()
  │           ├── parser_parse()
  │           ├── ast_print()
  │           ├── optimizer_optimize()
  │           ├── plan_print()
  │           ├── executor_run()
  │           │     └── result_set_print()
  │           ├── result_set_destroy()
  │           ├── plan_destroy()
  │           └── parser_destroy()
  └── db_context_destroy()
        └── catalog_destroy()
```

---

## 8. 示例数据：users 与 orders 表

### 8.1 为什么预加载示例数据

如果 CLI 启动后是空的，用户敲 `SELECT * FROM users;` 会报"表不存在"，体验很差。预加载两张表，用户一启动就能玩。

### 8.2 users 表

**设计意图**：模拟"用户表"，最经典的数据库示例。

| 列名 | 含义 | 类型 | 索引 |
|---|---|---|---|
| `id` | 用户 ID | int | ✅ 主键索引 |
| `age` | 年龄 | int | ❌ |
| `score` | 分数 | int | ❌ |

**数据**：

| id | age | score |
|---|---|---|
| 1 | 25 | 85 |
| 2 | 30 | 90 |
| 3 | 35 | 75 |
| 4 | 28 | 95 |
| 5 | 40 | 60 |

**为什么 5 行**：
- 太少（1-2行）：看不出 SeqScan vs IndexScan 的区别
- 太多（1000行）：初始化代码冗长
- 5 行刚好能演示过滤、投影、索引选择

**为什么 `id` 有索引**：主键默认建索引，优化器对 `WHERE id = 3` 会选 IndexScan。

**为什么 `age`、`score` 没索引**：演示"无索引列只能 SeqScan"的情况。

### 8.3 orders 表

**设计意图**：模拟"订单表"，与 users 形成**外键关系**（`orders.user_id` → `users.id`）。

| 列名 | 含义 | 类型 | 索引 |
|---|---|---|---|
| `order_id` | 订单 ID | int | ✅ 主键索引 |
| `user_id` | 用户 ID（外键） | int | ❌ |
| `amount` | 金额 | int | ❌ |

**数据**：

| order_id | user_id | amount |
|---|---|---|
| 101 | 1 | 500 |
| 102 | 2 | 300 |
| 103 | 1 | 700 |
| 104 | 3 | 200 |

**外键关系示意**：

```
users.id=1 ◀── orders.user_id=1 (order 101, amount 500)
         ◀── orders.user_id=1 (order 103, amount 700)
users.id=2 ◀── orders.user_id=2 (order 102, amount 300)
users.id=3 ◀── orders.user_id=3 (order 104, amount 200)
users.id=4 ◀── (无订单)
users.id=5 ◀── (无订单)
```

**注意**：教学版**不实现 JOIN**，但表结构已预留外键，为 Phase 2 做铺垫。

### 8.4 数据加载的代码

```c
int users_data[5][3] = {
    {1, 25, 85}, {2, 30, 90}, {3, 35, 75}, {4, 28, 95}, {5, 40, 60}
};
for (int i = 0; i < 5; i++) {
    users_rows[i].num_cols = 3;
    users_rows[i].values[0] = users_data[i][0];   // id
    users_rows[i].values[1] = users_data[i][1];   // age
    users_rows[i].values[2] = users_data[i][2];   // score
}
db_context_add_table(ctx, "users", users_rows, 5,
                     users_cols, 3, true, "id");
```

**步骤**：
1. 用二维数组 `users_data` 存原始数据（紧凑）
2. `for` 循环把数据拷到 `users_rows`（`exec_row_t` 结构体数组）
3. 调用 `db_context_add_table` 注册，`true, "id"` 表示有索引、索引列是 `id`

### 8.5 用示例数据玩 CLI

启动后可以试这些查询：

```
miniDB> .tables
users (5 rows, 3 cols, index on id)
orders (4 rows, 3 cols, index on order_id)

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

miniDB> SELECT * FROM users WHERE id = 3;
Plan:
  Filter(id =)  [rows=0 cost=1.0]
    IndexScan(users) [idx:id]  [rows=0 cost=1.0]
Result:
id | age | score
--- | --- | ---
3 | 35 | 75
(1 rows)

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

---

## 9. 网络协议设计

### 9.1 教学版为什么不做 TCP

miniDB 通过 stdin/stdout 交互，不走网络。原因：

| 原因 | 说明 |
|---|---|
| **教学重点** | 本章重点是"串联 SQL 流水线"，不是网络编程 |
| **复杂度** | TCP 服务器要处理连接、并发、错误恢复，代码量翻倍 |
| **调试方便** | stdin/stdout 可以直接用管道测试：`echo "SELECT 1;" \| ./minidb` |
| **Phase 规划** | 网络协议留到后续 Phase，循序渐进 |

### 9.2 stdin/stdout 交互模型

```
┌──────────┐    stdin     ┌──────────┐
│  终端    │ ──────────▶  │  miniDB  │
│ (用户)   │              │  进程    │
│          │ ◀────────── │          │
└──────────┘    stdout    └──────────┘
```

- 用户在终端敲键盘 → 终端把字符发到 miniDB 的 stdin
- miniDB `printf` 到 stdout → 终端显示在屏幕

**优点**：零配置，直接能用。
**缺点**：只能本地用，不能远程连接。

### 9.3 真实数据库的网络协议

生产数据库都是**客户端-服务器**架构：

```
┌──────────┐   TCP/IP    ┌──────────────┐
│ 客户端   │ ──────────▶ │  数据库      │
│ (psql)   │             │  服务器      │
│          │ ◀────────── │ (postgres)  │
└──────────┘   TCP/IP    └──────────────┘
```

**协议职责**：

| 职责 | 说明 |
|---|---|
| 连接建立 | TCP 三次握手 + 认证（用户名密码） |
| 请求发送 | 客户端把 SQL 编码成字节流发过去 |
| 响应接收 | 服务器把结果集编码成字节流发回来 |
| 错误处理 | 网络断开、超时、服务器崩溃 |
| 连接管理 | 连接池、心跳、断线重连 |

### 9.4 各数据库的协议

| 数据库 | 协议 | 端口 | 特点 |
|---|---|---|---|
| PostgreSQL | 自定义二进制协议 | 5432 | 消息分类型（Query、RowData、CommandComplete 等） |
| MySQL | 自定义二进制协议 | 3306 | 类似 PG，有握手阶段 |
| SQLite | **无网络协议** | — | 嵌入式，进程内调用，和 miniDB 类似 |
| MongoDB | Wire Protocol | 27017 | BSON 格式 |
| Redis | RESP (REdis Serialization Protocol) | 6379 | 文本协议，易读 |

**注意**：SQLite 也没有网络协议！它和 miniDB 一样是嵌入式数据库，直接链接到应用进程里。所以 miniDB 的设计**不是偷懒，而是和 SQLite 同类**。

### 9.5 如果要给 miniDB 加 TCP

**简化协议设计**（教学版如果实现）：

```
请求格式:  "SELECT * FROM users;\n"     (文本，以 \n 结尾)
响应格式:  "id|age|score\n1|25|85\n...\n(5 rows)\n"  (TSV，| 分隔)
```

**服务器伪代码**：

```c
int server_fd = socket(AF_INET, SOCK_STREAM, 0);
bind(server_fd, ...);
listen(server_fd, 10);

while (1) {
    int client_fd = accept(server_fd, ...);
    char buf[4096];
    read(client_fd, buf, sizeof(buf));     // 读 SQL
    process_sql(ctx, buf);                  // 执行（输出重定向到 client_fd）
    close(client_fd);
}
```

**但没做**，因为：
1. `process_sql` 当前写死 `printf` 到 stdout，要改成"写到任意 fd"需要重构
2. 并发处理（多客户端）要引入线程或 epoll
3. 这些是"网络编程"的课题，和"数据库内核"正交

### 9.6 协议设计的常见陷阱

| 陷阱 | 说明 | 真实案例 |
|---|---|---|
| **粘包** | TCP 是字节流，没有消息边界，要自己定分隔符 | 几乎所有协议都要处理 |
| **大端小端** | 不同 CPU 字节序不同，二进制协议要统一 | 网络字节序（大端） |
| **字符编码** | UTF-8 vs Latin1 vs GBK | MySQL 早期默认 latin1，坑了一代人 |
| **认证安全** | 明文密码 vs 哈希 vs SSL | PG 早期支持明文，现已废弃 |
| **版本协商** | 客户端和服务器版本不一致 | PG 有 StartupMessage 协商版本 |

> **新手提示**：如果你将来想给 miniDB 加网络层，建议先读 PostgreSQL 的协议文档（Frontend/Backend Protocol），它是教科书级的设计。

---

## 10. 与真实 CLI 对比

### 10.1 miniDB vs sqlite3 vs psql

| 特性 | miniDB | sqlite3 | psql |
|---|---|---|---|
| **交互模式** | 简单 `fgets` 循环 | readline + 历史 | readline + 历史 + 自动补全 |
| **多行输入** | 按 `;` 拼接 | 智能分割（考虑括号、字符串） | 智能分割 |
| **提示符** | 固定 `miniDB> ` | `sqlite> ` | `mydb=#` / `mydb-#` |
| **输出格式** | 固定表格 | 多种（`.mode csv/json/line/...`） | 多种（`\x` 竖排、`\a` 对齐） |
| **错误处理** | `Error: invalid SQL` | 带行号、列号、上下文 | 带行号、列号、提示 |
| **事务支持** | 无 | `BEGIN/COMMIT/ROLLBACK` | 同 sqlite3 |
| **脚本执行** | 无 | `.read file.sql` | `\i file.sql` |
| **元命令数** | 3 | ~40 | ~70 |
| **历史记录** | 无 | 上下箭头翻历史 | 同 + 搜索 |
| **自动补全** | 无 | 无 | Tab 补全表名/列名 |
| **计时** | 无 | `.timer on` | `\timing on` |
| **导出** | 无 | `.output file` | `\copy` |
| **配置文件** | 无 | `~/.sqliterc` | `~/.psqlrc` |

### 10.2 差距分析

#### 10.2.1 readline（历史 + 补全）

**miniDB**：用 `fgets`，没有历史记录，每次都要重新敲。
**sqlite3**：用 readline 库，上下箭头翻历史，Ctrl+R 搜索。
**psql**：readline + 自动补全，敲 `SELECT * FROM us<Tab>` 自动补成 `users`。

**如果 miniDB 要加**：
```c
#include <readline/readline.h>
#include <readline/history.h>

char *line = readline("miniDB> ");    // 替代 fgets
if (line && *line) add_history(line); // 加入历史
```

**没加的原因**：readline 是外部库，要 `-lreadline` 链接，增加构建复杂度。

#### 10.2.2 智能多行输入

**miniDB**：只看末尾有没有 `;`。
**sqlite3**：会考虑括号、字符串字面量。例如：
```sql
sqlite> SELECT 'hello
   ...> world';     -- 字符串里的换行不会触发执行
```
miniDB 会把 `'hello` 当成一条语句执行（因为末尾没 `;`），然后 `world';` 当成第二条——出错。

**如果 miniDB 要改**：Parser 要反馈"我还缺什么"（如"括号没闭合"），CLI 据此决定是否继续读。这需要 Parser 和 CLI 协作，复杂度显著增加。

#### 10.2.3 输出格式

**miniDB**：固定 `col | col` 表格。
**sqlite3**：
```
sqlite> .mode csv
sqlite> SELECT * FROM users;
1,25,85
2,30,90
```
**psql**：
```
postgres=# \x
Expanded display is on.
postgres=# SELECT * FROM users;
-[ RECORD 1 ]--
id    | 1
age   | 25
score | 85
```

**如果 miniDB 要加**：引入 `.mode` 命令，`result_set_print` 根据当前模式切换输出函数。

#### 10.2.4 错误位置

**miniDB**：
```
miniDB> SELEC * FROM users;
Error: invalid SQL
```
**sqlite3**：
```
sqlite> SELEC * FROM users;
Error: near "SELEC": syntax error
```
**psql**：
```
postgres=# SELEC * FROM users;
ERROR:  syntax error at or near "SELEC"
LINE 1: SELEC * FROM users;
        ^
```

psql 用 `^` 指出出错位置，体验最好。miniDB 的 Parser 其实有位置信息（Parser 章节讲过），但 `process_sql` 没用——只打印通用的 `invalid SQL`。这是简化，可作为习题。

### 10.3 miniDB 的优势

虽然功能少，但 miniDB 有自己的优势：

| 优势 | 说明 |
|---|---|
| **代码量小** | cli.c 150 行，sqlite3 的 shell.c 9000+ 行 |
| **易读** | 没有宏魔法、没有条件编译，一眼看懂 |
| **教学友好** | 打印 AST 和 Plan，sqlite3 默认不打印 |
| **零依赖** | 不用 readline、不用 ncurses，纯 C 标准库 |
| **启动快** | 无加载历史、无连数据库，毫秒级启动 |

> **新手提示**：工业级代码和教学代码的目标不同。工业级追求功能完备、性能极致；教学代码追求清晰、易读。miniDB 的 150 行能让你看懂 CLI 的本质，sqlite3 的 9000 行反而会淹没你。

---

## 11. 习题

### 习题 1（简单）：添加 `.version` 命令

**要求**：在 CLI 里加一个 `.version` 点命令，打印 `miniDB v0.1.0`。

**提示**：
- 在 `cli_run` 的点命令分支加 `else if (strcmp(buf, ".version") == 0)`
- 调用 `printf("miniDB v%s\n", ...)`（版本号怎么传进来？）
- 更新 `print_help` 的输出

**参考答案框架**：
```c
// 在 cli.h 加一个字段或用全局变量
// 在 cli.c 的点命令分支加:
else if (strcmp(buf, ".version") == 0)
    printf("miniDB v0.1.0\n");
```

### 习题 2（简单）：添加 `.schema` 命令

**要求**：加一个 `.schema` 命令，打印所有表的建表语句。

**期望输出**：
```
miniDB> .schema
CREATE TABLE users (id INT PRIMARY KEY, age INT, score INT);
CREATE TABLE orders (order_id INT PRIMARY KEY, user_id INT, amount INT);
```

**提示**：
- 遍历 `ctx->tables`
- 对每张表拼一个 `CREATE TABLE ...` 字符串
- 列名在 `t->cols[i].name`

### 习题 3（中等）：多行输入补空格

**要求**：修复第 4.7 节提到的"多行拼接丢空格"问题。每读一行，在末尾补一个空格再拼。

**当前行为**：
```
miniDB> SELECT id,
       age
       FROM users;
→ "SELECT id,ageFROM users"  (错！)
```

**期望行为**：
```
→ "SELECT id, age FROM users"  (对)
```

**提示**：在 `len += strlen(buf + len)` 之后，如果末尾不是 `;`，手动加一个空格：
```c
if (buf[len - 1] != ';') {
    buf[len++] = ' ';
    buf[len] = '\0';
}
```

### 习题 4（中等）：带行号的错误信息

**要求**：`process_sql` 解析失败时，打印更详细的错误。

**当前**：
```
miniDB> SELEC * FROM users;
Error: invalid SQL
```

**期望**：
```
miniDB> SELEC * FROM users;
Error: near "SELEC": syntax error
```

**提示**：
- Parser 内部有错误位置信息（Parser 章节讲过 `parser_t.error_msg`）
- `process_sql` 里 `stmt == NULL` 时，从 `p` 取错误信息打印

### 习题 5（中等）：`.timer` 命令

**要求**：加一个 `.timer on/off` 命令，开启后每条 SQL 执行完打印耗时。

**期望**：
```
miniDB> .timer on
miniDB> SELECT * FROM users;
...
(5 rows)
Time: 0.003s
```

**提示**：
- 用 `clock()` 或 `gettimeofday()` 记时
- `process_sql` 开头记开始时间，结尾记结束时间
- 用一个全局变量 `timer_on` 控制是否打印

### 习题 6（较难）：`.read` 脚本执行

**要求**：加一个 `.read file.sql` 命令，从文件读 SQL 逐行执行。

**期望**：
```
miniDB> .read test.sql
-- test.sql 内容:
SELECT * FROM users;
SELECT * FROM orders;
-- 输出: 两条查询的结果
```

**提示**：
- `fopen` 打开文件
- `fgets` 逐行读
- 对每行调用 `process_sql`（注意分号处理可以简化——文件里每行一条）
- `fclose` 关闭

### 习题 7（较难）：多语句执行

**要求**：支持一条输入里多条 SQL，用 `;` 分隔。

**期望**：
```
miniDB> SELECT * FROM users; SELECT * FROM orders;
-- 输出: users 表结果 + orders 表结果
```

**提示**：
- 当前逻辑看到第一个 `;` 就把整条送 `process_sql`
- 改成：按 `;` 分割，对每段调 `process_sql`
- 注意跳过空的段（连续 `;;`）

### 习题 8（难）：用 readline 替换 fgets

**要求**：用 GNU readline 库替换 `fgets`，支持上下箭头翻历史。

**提示**：
- `#include <readline/readline.h>` 和 `#include <readline/history.h>`
- `char *line = readline("miniDB> ");` 替代 `fgets`
- `if (line && *line) add_history(line);`
- `free(line);` 释放
- 编译时加 `-lreadline`（Linux 通常自带，Windows 要装）

### 习题 9（难）：TCP 服务器模式

**要求**：加一个 `--serve` 参数，启动 TCP 服务器（端口 5432），接受客户端连接，执行 SQL 返回结果。

**提示**：
- `main` 里判断 `argv[1] == "--serve"`，调 `server_run(ctx)` 而非 `cli_run(ctx)`
- `server_run` 用 `socket/bind/listen/accept`
- 把 `process_sql` 的 `printf` 重定向到 client fd（用 `dup2` 或改写 `process_sql` 接受 fd 参数）
- 用 `telnet localhost 5432` 测试

### 习题 10（思考题）：CLI 与嵌入式 API

**问题**：SQLite 既可以当 CLI 用（`sqlite3` 命令），也可以当库用（`#include <sqlite3.h>`，调 `sqlite3_exec()`）。miniDB 目前只有 CLI。如果要加嵌入式 API，`db_context_t` 的设计要改什么？

**思考方向**：
- `cli_run` 是 CLI 专属，API 不该调它
- `process_sql` 目前是 `static`，API 要用得改成非 static 并导出
- 返回值：目前 `process_sql` 直接 `printf`，API 要返回 `result_set_t *` 让调用者自己处理
- 错误处理：目前 `printf("Error")`，API 要返回错误码

---

## 附录 A：完整文件清单

| 文件 | 行数 | 职责 |
|---|---|---|
| `phase1/src/server/cli.h` | 22 | CLI 接口声明 + `db_context_t` 定义 |
| `phase1/src/server/cli.c` | 150 | CLI 实现：REPL 循环、命令分发、`process_sql` |
| `phase1/src/main.c` | 78 | 程序入口：参数处理、示例数据加载、调 `cli_run` |

**总行数**：250 行 C 代码，实现一个能用的数据库 CLI。

### A.1 编译与运行

```bash
# 编译（假设有 Makefile）
make

# 运行
./minidb

# 查看版本
./minidb --version

# 查看帮助
./minidb --help

# 管道测试（不进交互模式）
echo "SELECT * FROM users;" | ./minidb
```

### A.2 典型会话

```
$ ./minidb
miniDB v0.1  (type .help for help)
miniDB> .help
miniDB Commands:
  SQL statements end with ';'
  .help     Show this help
  .tables   List tables
  .exit     Quit

Supported SQL:
  SELECT * | col1, col2 FROM table [WHERE col OP value [AND ...]]
  INSERT INTO table (cols) VALUES (vals)
  DELETE FROM table [WHERE ...]
  CREATE TABLE table (col TYPE [PRIMARY KEY], ...)
miniDB> .tables
users (5 rows, 3 cols, index on id)
orders (4 rows, 3 cols, index on order_id)
miniDB> SELECT * FROM users WHERE age > 28;
AST: SELECT * FROM users WHERE age > 28
Plan:
  Filter(age >)  [rows=2 cost=1.5]
    SeqScan(users)  [rows=5 cost=1.0]
Result:
id | age | score
--- | --- | ---
2 | 30 | 90
3 | 35 | 75
4 | 28 | 95
5 | 40 | 60
(4 rows)
miniDB> .exit
Bye!
```

---

## 附录 B：常见报错与排查

### B.1 编译错误

| 错误 | 原因 | 解决 |
|---|---|---|
| `cli.h: No such file` | include 路径不对 | 检查 `-I` 参数 |
| `undefined reference to catalog_create` | 没链接 catalog.c | 检查 Makefile 的源文件列表 |
| `EXEC_MAX_NAME undeclared` | 没包含 executor.h | cli.h 已包含，检查 include 顺序 |

### B.2 运行时错误

| 现象 | 原因 | 解决 |
|---|---|---|
| 提示符不显示 | `fflush` 没调 | 已在代码里调，检查是否被删 |
| 敲 `;` 不执行 | 末尾有不可见字符 | 用 `cat -A` 检查输入 |
| 段错误 | 缓冲区溢出 | 检查 SQL 是否超 4KB |
| `.tables` 空输出 | `init_sample_data` 没调 | 检查 main.c 调用顺序 |

### B.3 行为异常

| 现象 | 原因 | 这是 bug 吗？ |
|---|---|---|
| 多行输入丢空格 | 第 4.7 节描述的问题 | 是（简化缺陷） |
| `SELECT 1;` 报错 | 教学版不支持无 FROM 子句 | 否（设计如此） |
| INSERT 后数据没变 | 教学版 INSERT 是空操作 | 否（Phase 1 简化） |
| Ctrl+D 能退出 | `fgets` 返回 NULL | 否（正确行为） |

---

## 附录 C：术语表

| 术语 | 英文 | 含义 |
|---|---|---|
| CLI | Command Line Interface | 命令行界面 |
| REPL | Read-Eval-Print Loop | 读取-求值-打印-循环 |
| AST | Abstract Syntax Tree | 抽象语法树 |
| Catalog | — | 表的元数据（统计信息） |
| ResultSet | — | 查询结果集 |
| SeqScan | Sequential Scan | 顺序全表扫描 |
| IndexScan | Index Scan | 索引扫描 |
| 点命令 | Dot Command | 以 `.` 开元的元命令 |
| 提示符 | Prompt | REPL 显示的等待输入标记 |
| 嵌入式数据库 | Embedded Database | 进程内调用，无独立服务器 |
| 客户端-服务器 | Client-Server | 网络架构，客户端发请求，服务器处理 |
| readline | — | GNU 库，提供行编辑和历史 |
| 粘包 | — | TCP 字节流无消息边界的问题 |
| 零拷贝 | Zero Copy | 传指针而非拷贝数据 |

---

## 下一步

CLI 就绪后，Phase 1 的全部模块（存储、Parser、Optimizer、Executor、CLI）已串联完成。下一章将进行**集成测试与性能压测**，验证各模块协作正确性并测量吞吐量。

**回顾 Phase 1 全景**：

```
章1  存储      ─┐
章2  Buffer    ─┤
章3  Parser    ─┤
章4  AST       ─┤
章5  Catalog   ─┤
章6  Optimizer ─┤  Phase 1: 从零搭一个能跑 SELECT 的数据库
章7  Executor  ─┤
章8  Index     ─┤
章9  ResultSet ─┤
章10 CLI       ─┘  ← 你在这里
章11 集成测试  ──  下一步
```

读完本章，你应该能：
- ✅ 解释 REPL 的工作原理
- ✅ 看懂 `cli_run` 的每一行
- ✅ 理解 `db_context_t` 为什么这样设计
- ✅ 说出 miniDB CLI 和 sqlite3 的 5 个差距
- ✅ 独立完成习题 1-3

如果还有疑问，回头重读第 7 节的代码逐行解读，或者打开 `cli.c` 边读边对照本章。**最好的学习方式是改代码**——挑一道习题做，做完你会真正理解 CLI。
