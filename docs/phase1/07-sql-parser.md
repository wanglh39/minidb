# 章7：SQL 解析

> 用户写 `SELECT * FROM users WHERE id = 42`，数据库如何理解这句话？本章实现手写词法分析器和递归下降语法分析器，将 SQL 文本转化为 AST。

## 三步过程

```
SQL 文本 ──lexer──▶ Token 流 ──parser──▶ AST
 "SELECT * FROM..."   [KW_SELECT][OP:*]...   ast_stmt_t{type=SELECT,...}
```

| 阶段 | 输入 | 输出 | 类比 |
|---|---|---|---|
| **Lexer（词法）** | 字符流 | Token 流 | 把字母组合成单词 |
| **Parser（语法）** | Token 流 | AST | 理解句子结构 |
| **AST** | — | 语义对象 | 句子的含义 |

## Token 类型

```c
typedef enum {
    TOK_KEYWORD,   // SELECT, FROM, WHERE, INSERT, ...
    TOK_IDENT,     // 表名、列名
    TOK_NUMBER,    // 42, 3.14
    TOK_STRING,    // 'Alice'
    TOK_OP,        // =, !=, <, >, <=, >=, *
    TOK_LPAREN,    // (
    TOK_RPAREN,    // )
    TOK_COMMA,     // ,
    TOK_SEMICOLON, // ;
    TOK_EOF,       // 输入结束
    TOK_ERROR,     // 无法识别的字符
} token_type_t;
```

## 词法分析器（Lexer）

### 工作原理

逐字符扫描输入，跳过空白，根据首字符决定 Token 类型：

```
首字符是数字  → lex_number()  → TOK_NUMBER
首字符是字母  → lex_ident()   → TOK_IDENT 或 TOK_KEYWORD
首字符是 '    → lex_string()  → TOK_STRING
首字符是 =!<>> → lex_op()      → TOK_OP
首字符是 (  )  ,  ;           → 对应标点 Token
```

### 关键字识别

```c
static const struct { const char *text; keyword_t kw; } kw_table[] = {
    {"SELECT", KW_SELECT}, {"FROM", KW_FROM}, {"WHERE", KW_WHERE},
    {"INSERT", KW_INSERT}, {"INTO", KW_INTO}, {"VALUES", KW_VALUES},
    ...
};

static int lookup_keyword(const char *text) {
    for (int i = 0; kw_table[i].text; i++)
        if (strcasecmp(text, kw_table[i].text) == 0)
            return (int)kw_table[i].kw;
    return -1;  // 不是关键字
}
```

> **陷阱**：`keyword_t` 是枚举，GCC 可能将其视为无符号类型，导致 `(keyword_t)-1 >= 0` 为 true。`lookup_keyword` 返回 `int` 而非 `keyword_t` 来避免此问题。

### Peek 功能

```c
token_t lexer_peek(lexer_t *lex);  // 预读但不消费
token_t lexer_next(lexer_t *lex);  // 读取并消费
```

Parser 需要预读一个 Token 来决定走哪个语法分支，lexer 内部缓存一个 peeked token。

## AST 结构

```c
typedef struct {
    stmt_type_t type;       // SELECT / INSERT / DELETE / CREATE
    char table[32];         // 表名

    // SELECT 用
    char columns[16][32];   // 列名列表
    int num_cols;
    bool select_all;        // SELECT *

    // WHERE 用
    ast_expr_t where[8];    // 条件列表（AND 连接）
    int num_where;

    // INSERT 用
    ast_value_t values[16]; // 值列表
    int num_values;

    // CREATE 用
    ast_col_def_t col_defs[16]; // 列定义
    int num_col_defs;
} ast_stmt_t;
```

### 值类型

```c
typedef enum { VAL_INT, VAL_FLOAT, VAL_STRING, VAL_NULL } val_type_t;

typedef struct {
    val_type_t type;
    int32_t int_val;
    float   float_val;
    char    str_val[64];
} ast_value_t;
```

### 表达式

当前只支持比较表达式（`column OP value`）：

```c
typedef struct {
    expr_type_t type;       // EXPR_COMPARE
    char column[32];        // 列名
    char op[4];             // =, !=, <, >, <=, >=
    ast_value_t value;      // 比较值
} ast_expr_t;
```

## 递归下降语法分析器

### 核心思想

每条语法规则对应一个 C 函数。函数从当前 Token 开始，尝试匹配规则，成功则返回 AST 节点，失败返回 NULL。

```
parse()        → 根据 SELECT/INSERT/DELETE/CREATE 分发
parse_select() → SELECT (* | col_list) FROM table [WHERE expr_list]
parse_insert() → INSERT INTO table (col_list) VALUES (val_list)
parse_delete() → DELETE FROM table [WHERE expr_list]
parse_create() → CREATE TABLE table (col_def_list)
parse_where()  → WHERE expr (AND expr)*
parse_expr()   → IDENT OP value
parse_value()  → NUMBER | STRING | NULL
```

### 辅助函数

```c
static void advance(parser_t *p);           // 消费当前 Token，读下一个
static bool is_kw(parser_t *p, keyword_t);  // 当前 Token 是某关键字？
static bool accept_kw(parser_t *p, keyword_t); // 是则消费并返回 true
static bool expect_kw(parser_t *p, keyword_t); // 必须是，否则返回 false
```

### SELECT 解析示例

```c
static ast_stmt_t *parse_select(parser_t *p) {
    advance(p);  // 消费 SELECT
    ast_stmt_t *s = calloc(1, sizeof(ast_stmt_t));
    s->type = AST_SELECT;

    if (p->cur.type == TOK_OP && p->cur.text[0] == '*') {
        s->select_all = true;
        advance(p);
    } else {
        // 解析列名列表: col, col, col
        while (p->cur.type == TOK_IDENT) {
            strncpy(s->columns[s->num_cols++], p->cur.text, 31);
            advance(p);
            if (p->cur.type != TOK_COMMA) break;
            advance(p);
        }
    }

    expect_kw(p, KW_FROM);              // 必须有 FROM
    strncpy(s->table, p->cur.text, 31); // 表名
    advance(p);
    parse_where(p, s);                  // 可选 WHERE
    return s;
}
```

### 语法图

```
SELECT 语句:
  ┌─────────┐     ┌──────────────────┐     ┌──────┐     ┌──────────┐
  │ SELECT  │ ──▶ │  *  │  col_list  │ ──▶ │ FROM │ ──▶ │  table   │
  └─────────┘     └──────────────────┘     └──────┘     └──────────┘
                                                    │
                                                    ▼
                                              ┌──────────┐
                                              │ [WHERE]  │
                                              └──────────┘

WHERE 子句:
  ┌────────┐     ┌──────┐     ┌────────┐     ┌──────────┐
  │ WHERE  │ ──▶ │ expr │ ──▶ │ (AND   │ ──▶ │  expr)*  │
  └────────┘     └──────┘     └────────┘     └──────────┘

expr:
  ┌──────┐     ┌───┐     ┌───────┐
  │ col  │ ──▶ │OP │ ──▶ │ value │
  └──────┘     └───┘     └───────┘
```

## 支持的 SQL 子集

| 语句 | 示例 |
|---|---|
| SELECT | `SELECT * FROM users WHERE id >= 10 AND age < 30` |
| INSERT | `INSERT INTO users (id, name) VALUES (1, 'Alice')` |
| DELETE | `DELETE FROM users WHERE id = 5` |
| CREATE | `CREATE TABLE users (id INT PRIMARY KEY, age INT, score FLOAT)` |

### 不支持（后续可扩展）

- UPDATE 语句（AST 已预留字段）
- JOIN / 子查询
- GROUP BY / HAVING / ORDER BY
- 聚合函数 (COUNT, SUM, AVG)
- OR 条件（仅 AND）
- 嵌套表达式

## 使用方式

```c
// 解析 SQL 字符串
parser_t *p = parser_create("SELECT * FROM users WHERE id = 42");
ast_stmt_t *stmt = parser_parse(p);

if (stmt) {
    printf("语句类型: %d\n", stmt->type);      // 0 = AST_SELECT
    printf("表名: %s\n", stmt->table);          // "users"
    printf("条件数: %d\n", stmt->num_where);    // 1
    printf("条件: %s %s %d\n",
           stmt->where[0].column,               // "id"
           stmt->where[0].op,                   // "="
           stmt->where[0].value.int_val);       // 42
    free(stmt);
}
parser_destroy(p);
```

## 设计要点

### 为什么手写而非用 flex/bison？

| | 手写 | flex/bison |
|---|---|---|
| 依赖 | 零 | 需要工具链 |
| 错误信息 | 可精确控制 | 自动生成，难以定制 |
| 学习价值 | 高 | 低 |
| 代码量 | 中 | 少（但生成代码多） |
| 性能 | 好 | 好 |

教学项目优先零依赖和学习价值，选择手写。

### 为什么 AST 用扁平结构而非指针树？

```c
// 本项目：扁平数组
ast_stmt_t {
    char columns[16][32];  // 直接内嵌
    ast_expr_t where[8];   // 直接内嵌
}

// 生产系统：指针树
struct ast_select {
    ast_expr_t *where;  // 动态分配
    ast_select_t *next; // 链表
}
```

扁平结构：
- **优点**：一次 calloc 搞定，无需多次 malloc/free，缓存友好
- **缺点**：上限固定（16 列、8 个条件），不支持任意嵌套
- **适合**：教学项目、简单 SQL 子集

## 与工业级实现的差距

| 特性 | miniDB | SQLite | PostgreSQL |
|---|---|---|---|
| Lexer | 手写 | 手写 | flex |
| Parser | 递归下降 | LALR(1) | bison (LALR) |
| AST | 扁平结构 | 标签联合 | 指针树 |
| 错误恢复 | 无 | 有 | 有 |
| 子查询 | 不支持 | 支持 | 支持 |
| JOIN | 不支持 | 支持 | 支持 |

## 文件清单

| 文件 | 职责 |
|---|---|
| `ast.h/c` | AST 节点定义 + 打印 |
| `lexer.h/c` | 词法分析器 |
| `parser.h/c` | 递归下降语法分析器 |
| `test_parser.c` | 11 个测试 |

## 测试覆盖

```
test_parse_select_star        — SELECT * FROM users
test_parse_select_cols        — SELECT id, name FROM users
test_parse_select_where       — SELECT * FROM users WHERE id = 42
test_parse_select_where_multi — 多个 AND 条件
test_parse_insert             — INSERT INTO ... VALUES
test_parse_insert_string_value — 字符串值
test_parse_delete             — DELETE FROM ... WHERE
test_parse_delete_all         — DELETE FROM（无 WHERE）
test_parse_create             — CREATE TABLE
test_parse_select_float_where — 浮点数条件
test_parse_invalid            — 非法输入返回 NULL
```

## 下一步

AST 生成后，下一章实现**查询优化器**：将 AST 转化为逻辑计划，应用启发式规则（谓词下推、投影裁剪）和代价模型选择最优 Join 顺序。