# 章7：SQL 解析器——从字符串到抽象语法树

> 当你敲下 `SELECT * FROM users WHERE id = 42` 时，数据库并不能直接"看懂"这行文字。它必须先把这串字符拆成一个个有意义的单词（Token），再根据语法规则把这些单词组装成一棵树（AST），最后才能执行查询。本章带你从零实现一个手写的词法分析器（Lexer）和递归下降语法分析器（Parser），把 SQL 文本转化为 AST。
>
> 本章面向新手，假设你只懂 C 语言基础（指针、结构体、枚举）。读完本章，你将理解：
> - 编译器的四个经典阶段是怎么衔接的；
> - 如何用 150 行 C 代码写一个词法分析器；
> - 如何用 180 行 C 代码写一个递归下降语法分析器；
> - 为什么枚举类型在 GCC 下会埋下"无符号陷阱"；
> - 手写解析器和 flex/bison 工具生成的解析器各有什么优劣。

---

## 目录

1. [编译原理基础](#1-编译原理基础)
2. [词法分析详解](#2-词法分析详解)
3. [递归下降解析](#3-递归下降解析)
4. [AST 概念](#4-ast-概念)
5. [语法规则详解](#5-语法规则详解)
6. [错误处理](#6-错误处理)
7. [代码逐行解读](#7-代码逐行解读)
8. [枚举陷阱](#8-枚举陷阱)
9. [手写 vs 工具生成](#9-手写-vs-工具生成)
10. [与真实数据库对比](#10-与真实数据库对比)
11. [习题](#11-习题)

---

## 1. 编译原理基础

### 1.1 一句话理解编译

**编译**就是把一种语言（源语言）翻译成另一种语言（目标语言）的过程。GCC 把 C 翻译成 x86 汇编；Javac 把 Java 翻译成字节码；SQL 解析器把 SQL 文本翻译成 AST。它们的本质都是同一条流水线。

### 1.2 四个经典阶段

教科书上的编译器分为四个阶段，本项目也严格遵循这一划分：

```
源程序（SQL 文本）
        │
        ▼  ① 词法分析（Lexical Analysis / Scanning）
   Token 流（单词序列）
        │
        ▼  ② 语法分析（Syntax Analysis / Parsing）
   抽象语法树 AST
        │
        ▼  ③ 语义分析（Semantic Analysis）
   带类型的 AST / 符号表
        │
        ▼  ④ 代码生成（Code Generation）
   逻辑计划 / 物理计划 / 机器码
```

下表把每个阶段类比成"读一句中文"的过程，方便新手建立直觉：

| 阶段 | 输入 | 输出 | 中文类比 | 本项目对应 |
|---|---|---|---|---|
| ① 词法分析 | 字符流 `S,E,L,E,C,T,...` | Token 流 `[KW_SELECT][OP:*]...` | 把"我要吃饭"拆成 `我/要/吃饭` 三个词 | `lexer.c` |
| ② 语法分析 | Token 流 | AST | 理解"我(主) 要(谓) 吃饭(宾)"的句子结构 | `parser.c` |
| ③ 语义分析 | AST | 带语义的 AST | 检查"吃饭"是不是及物动词、主语能不能是人 | （后续章节） |
| ④ 代码生成 | 带语义的 AST | 执行计划 | 把句子翻译成英文 `I want to eat` | （后续章节） |

> **本阶段范围**：本章只实现 ① 和 ②。③ 和 ④ 在后续章节完成。也就是说，解析器输出 AST 后就交差，**不检查表是否存在、列是否存在**——那是语义分析的活。

### 1.3 为什么数据库要自己写编译器前端？

你可能会问：SQL 是文本，为什么不直接用 `strcmp` 一条条匹配？

考虑这几种写法，它们语义相同但文本不同：

```sql
SELECT * FROM users WHERE id = 42;
select   *   from   users   where   id=42;
SELECT *
FROM users
WHERE id = 42;
```

如果用 `strcmp`，你要处理大小写、空格、换行、缩进……本质上你正在重新发明词法分析器。而写一个真正的 Lexer + Parser 之后，上面三种写法都会得到**完全相同**的 AST，后续执行逻辑只认 AST，不再关心文本长什么样。这就是编译器前端的价值：**把无穷无尽的文本变化归一化成一个有限的结构**。

### 1.4 本项目的三步管线

```
SQL 文本 ──lexer──▶ Token 流 ──parser──▶ AST
"SELECT * FROM..."  [KW_SELECT][OP:*]...   ast_stmt_t{type=AST_SELECT,...}
```

| 阶段 | 输入 | 输出 | 类比 |
|---|---|---|---|
| **Lexer（词法）** | 字符流 | Token 流 | 把字母组合成单词 |
| **Parser（语法）** | Token 流 | AST | 理解句子结构 |
| **AST** | — | 语义对象 | 句子的含义 |

### 1.5 小结

- 编译 = 翻译，分四阶段：词法 → 语法 → 语义 → 代码生成。
- 本章实现前两阶段，产物是 AST。
- 用结构（AST）代替字符串匹配，是处理"同一语义、多种写法"的标准手段。

---

## 2. 词法分析详解

### 2.1 什么是 Token

**Token**（单词/记号）是源代码中具有独立意义的最小单位。就像中文里的"词"是最小表意单位一样。

`SELECT * FROM users WHERE id = 42` 这条 SQL 被词法分析器切分成如下 Token：

| 序号 | 文本 | Token 类型 | 关键字 | 数值 |
|---|---|---|---|---|
| 1 | `SELECT` | TOK_KEYWORD | KW_SELECT | — |
| 2 | `*` | TOK_OP | — | — |
| 3 | `FROM` | TOK_KEYWORD | KW_FROM | — |
| 4 | `users` | TOK_IDENT | — | — |
| 5 | `WHERE` | TOK_KEYWORD | KW_WHERE | — |
| 6 | `id` | TOK_IDENT | — | — |
| 7 | `=` | TOK_OP | — | — |
| 8 | `42` | TOK_NUMBER | — | int_val=42 |
| 9 | (结尾) | TOK_EOF | — | — |

注意：空格被**丢弃**了。词法分析器只关心有意义的字符。

### 2.2 Token 类型定义

本项目定义了 11 种 Token 类型，覆盖 SQL 子集所需的全部单词种类：

```c
typedef enum {
    TOK_KEYWORD = 0,   // 关键字：SELECT, FROM, WHERE, INSERT, ...
    TOK_IDENT   = 1,   // 标识符：表名、列名（如 users, id）
    TOK_NUMBER  = 2,   // 数字字面量：42, 3.14
    TOK_STRING  = 3,   // 字符串字面量：'Alice'
    TOK_OP      = 4,   // 运算符：=, !=, <, >, <=, >=, *
    TOK_LPAREN  = 5,   // 左括号 (
    TOK_RPAREN  = 6,   // 右括号 )
    TOK_COMMA   = 7,   // 逗号 ,
    TOK_SEMICOLON = 8, // 分号 ;
    TOK_EOF     = 9,   // 输入结束（End Of File）
    TOK_ERROR   = 10,  // 无法识别的字符
} token_type_t;
```

新手可能问：为什么括号、逗号要单独成类，不归入 `TOK_OP`？

**答**：因为它们的语法地位不同。`=` 出现在表达式里（`id = 42`），而 `,` 出现在列表分隔处（`id, name, age`）。把它们分门别类，语法分析器就能直接用 `p->cur.type == TOK_COMMA` 判断，不必再去比较字符串内容，代码更清晰、更高效。

### 2.3 关键字表

关键字是"被语言保留的标识符"。`SELECT` 是关键字，不能当表名。本项目支持 19 个关键字：

```c
typedef enum {
    KW_SELECT, KW_FROM, KW_WHERE, KW_INSERT, KW_INTO, KW_VALUES,
    KW_UPDATE, KW_SET, KW_DELETE, KW_CREATE, KW_TABLE,
    KW_AND, KW_OR, KW_NULL, KW_INT, KW_FLOAT, KW_PRIMARY, KW_KEY,
    KW_NOT,
} keyword_t;
```

为了让"输入 `select`（小写）也能识别成 `KW_SELECT`"，词法分析器维护一张**关键字表**，并用 `strcasecmp`（忽略大小写比较）查找：

```c
static const struct { const char *text; keyword_t kw; } kw_table[] = {
    {"SELECT", KW_SELECT}, {"FROM", KW_FROM}, {"WHERE", KW_WHERE},
    {"INSERT", KW_INSERT}, {"INTO", KW_INTO}, {"VALUES", KW_VALUES},
    {"UPDATE", KW_UPDATE}, {"SET", KW_SET},   {"DELETE", KW_DELETE},
    {"CREATE", KW_CREATE}, {"TABLE", KW_TABLE}, {"AND", KW_AND},
    {"OR", KW_OR}, {"NULL", KW_NULL}, {"INT", KW_INT}, {"FLOAT", KW_FLOAT},
    {"PRIMARY", KW_PRIMARY}, {"KEY", KW_KEY}, {"NOT", KW_NOT},
    {NULL, 0},   // 哨兵，标记表尾
};
```

查找逻辑：

```c
static int lookup_keyword(const char *text) {
    for (int i = 0; kw_table[i].text; i++) {
        if (strcasecmp(text, kw_table[i].text) == 0)
            return (int)kw_table[i].kw;
    }
    return -1;   // 不是关键字，是普通标识符
}
```

**为什么用线性查找而不是哈希表？**

19 个关键字，线性查找平均比较 10 次，远低于哈希函数计算 + 处理冲突的开销。**小规模下，简单就是最快**。SQLite 也用同样的策略。

### 2.4 Token 结构体

一个 Token 不仅要记录"是什么类型"，还要记录"具体内容"：

```c
typedef struct {
    token_type_t type;     // 类型
    keyword_t    keyword;  // 若 type==TOK_KEYWORD，记录哪个关键字
    char         text[64]; // 原始文本（最多 63 字符）
    int32_t      int_val;  // 若是整数字面量，预转换的值
    float        float_val;// 若是浮点字面量，预转换的值
} token_t;
```

设计要点：

| 字段 | 作用 | 何时填充 |
|---|---|---|
| `type` | 必填，所有 Token 都有 | 词法分析时 |
| `keyword` | 仅 `TOK_KEYWORD` 填充 | `lex_ident` 命中关键字表时 |
| `text` | 原始文本，调试用 | 所有 Token |
| `int_val` | 整数预转换，省去解析器再 `atoi` | `lex_number` 且无小数点时 |
| `float_val` | 浮点预转换 | `lex_number` 且有小数点时 |

**预转换的好处**：解析器拿到 `TOK_NUMBER` 后直接读 `int_val`，不必再调 `atoi`，既快又避免重复代码。

### 2.5 词法分析器主循环

词法分析器内部状态：

```c
struct lexer {
    const char *src;     // 源字符串指针
    int pos;             // 当前扫描位置
    int len;             // 源串长度
    token_t peeked;      // 预读缓存
    bool has_peek;       // 缓存是否有效
};
```

主循环 `lexer_next` 的工作流程：

```
            ┌──────────────────────────┐
            │  调用 lexer_next(lex)    │
            └────────────┬─────────────┘
                         ▼
              ┌──────────────────────┐
              │ has_peek 为真？      │
              └────┬────────────┬────┘
                   │是          │否
                   ▼            ▼
        ┌─────────────────┐  ┌─────────────────────┐
        │ 清空 has_peek   │  │ 跳过空白字符        │
        │ 返回 peeked     │  │ while isspace: pos++│
        └─────────────────┘  └──────────┬──────────┘
                                        ▼
                            ┌─────────────────────┐
                            │ pos >= len？        │
                            └────┬───────────┬────┘
                                 │是         │否
                                 ▼           ▼
                       ┌──────────────┐  ┌──────────────────┐
                       │ 返回 TOK_EOF │  │ 看首字符 c       │
                       └──────────────┘  └────────┬─────────┘
                                                  ▼
                          ┌────────────────────────────────────┐
                          │ isdigit(c)  → lex_number          │
                          │ isalpha(c)  → lex_ident           │
                          │ c=='\''     → lex_string          │
                          │ c=='('      → TOK_LPAREN, pos++   │
                          │ c==')'      → TOK_RPAREN, pos++   │
                          │ c==','      → TOK_COMMA,  pos++   │
                          │ c==';'      → TOK_SEMICOLON,pos++ │
                          │ c=='*'      → TOK_OP("*"), pos++  │
                          │ c in =!<>>  → lex_op（可能双字符）│
                          │ 其他        → TOK_ERROR           │
                          └────────────────────────────────────┘
```

**首字符决定法**是词法分析的经典技巧：只看第一个字符就能确定 Token 的大类，因为 SQL 的词法规则满足"最长前缀唯一"性质。例如看到数字开头，就一定是数字 Token，不可能中途变成关键字。

### 2.6 各类 Token 的识别

#### 2.6.1 数字 `lex_number`

```c
static token_t lex_number(lexer_t *lex) {
    token_t t = make_tok(TOK_NUMBER);
    int start = lex->pos;
    bool is_float = false;

    while (lex->pos < lex->len &&
           (isdigit((unsigned char)lex->src[lex->pos]) ||
            lex->src[lex->pos] == '.')) {
        if (lex->src[lex->pos] == '.') is_float = true;
        lex->pos++;
    }
    // ... 复制文本、转换数值
    if (is_float) t.float_val = (float)atof(t.text);
    else          t.int_val   = atoi(t.text);
    return t;
}
```

逻辑：一直吞数字和小数点，遇到非数字字符停止。**注意**：这段代码不校验"多个小数点"这种错误（`3.14.15` 会被当成一个 Token），因为教学项目优先简洁。生产级词法器会在这里报错。

#### 2.6.2 标识符与关键字 `lex_ident`

```c
static token_t lex_ident(lexer_t *lex) {
    token_t t = make_tok(TOK_IDENT);
    int start = lex->pos;
    while (lex->pos < lex->len &&
           (isalnum((unsigned char)lex->src[lex->pos]) ||
            lex->src[lex->pos] == '_')) {
        lex->pos++;
    }
    // 复制到 t.text ...
    int kw = lookup_keyword(t.text);
    if (kw >= 0) {           // 命中关键字表
        t.type = TOK_KEYWORD;
        t.keyword = (keyword_t)kw;
    }
    return t;
}
```

**两步走**：先按"字母/数字/下划线"吞字符得到一个词，再去关键字表里查。命中就是关键字，没命中就是普通标识符。这就是为什么 `SELECT` 和 `users` 走的是同一段代码——它们在词法层面都是"由字母开头的词"，区别只在关键字表里有没有登记。

#### 2.6.3 字符串字面量 `lex_string`

```c
static token_t lex_string(lexer_t *lex) {
    token_t t = make_tok(TOK_STRING);
    lex->pos++;              // 跳过开头的 '
    int start = lex->pos;
    while (lex->pos < lex->len && lex->src[lex->pos] != '\'') lex->pos++;
    // 复制内容 ...
    if (lex->pos < lex->len) lex->pos++;  // 跳过结尾的 '
    return t;
}
```

SQL 字符串用单引号包围：`'Alice'`。代码跳过开头 `'`，一直吞到下一个 `'` 为止。**不支持转义**（如 `'It\'s'`），教学项目从简。

#### 2.6.4 运算符 `lex_op`

```c
if (c == '=' || c == '!' || c == '<' || c == '>') {
    token_t t = make_tok(TOK_OP);
    t.text[0] = c;
    lex->pos++;
    if (lex->pos < lex->len && lex->src[lex->pos] == '=') {
        t.text[1] = '=';
        t.text[2] = '\0';
        lex->pos++;
    } else {
        t.text[1] = '\0';
    }
    return t;
}
```

支持的双字符运算符：`<=`, `>=`, `!=`。逻辑：先吞第一个字符，如果下一个是 `=` 就再吞一个。**注意**：单独的 `!` 或 `<`（无 `=`）也会被当成合法 Token，但语义分析阶段会拒绝不认识的运算符。

### 2.7 Peek 机制（预读不消费）

```c
token_t lexer_peek(lexer_t *lex) {
    if (!lex->has_peek) {
        lex->peeked = lexer_next(lex);   // 真正读一次
        lex->has_peek = true;
    }
    return lex->peeked;
}
```

**为什么需要预读？** 语法分析器有时需要"看一眼下一个 Token 但不消费它"，才能决定走哪条语法分支。例如解析 `SELECT` 后，要看下一个是 `*` 还是列名，但如果是列名，这个列名 Token 还得留给后续的"列名列表"循环使用，不能消费掉。

**实现方式**：lexer 内部缓存一个 `peeked` Token 和一个 `has_peek` 标志。

| 操作 | 行为 |
|---|---|
| `lexer_peek` | 若缓存空，调 `lexer_next` 填充缓存；返回缓存。**不移动位置**。 |
| `lexer_next` | 若缓存有值，清空缓存并返回它；否则真正扫描下一个 Token。 |

**限制**：只支持**一级预读**（peek 一次）。这对本项目的 SQL 子集够用，但更复杂的语法（如 C++ 模板）需要任意级预读。

### 2.8 一个完整的词法分析示例

输入：`INSERT INTO users (id, name) VALUES (1, 'Alice')`

| 步骤 | pos | 当前字符 | 动作 | 输出 Token |
|---|---|---|---|---|
| 1 | 0 | `I` | `lex_ident` → 命中关键字 | `TOK_KEYWORD(KW_INSERT)` |
| 2 | 6 | ` ` | 跳空白 | — |
| 3 | 7 | `I` | `lex_ident` → 命中关键字 | `TOK_KEYWORD(KW_INTO)` |
| 4 | 11 | ` ` | 跳空白 | — |
| 5 | 12 | `u` | `lex_ident` → 未命中 | `TOK_IDENT("users")` |
| 6 | 17 | ` ` | 跳空白 | — |
| 7 | 18 | `(` | 直接返回 | `TOK_LPAREN` |
| 8 | 19 | `i` | `lex_ident` | `TOK_IDENT("id")` |
| 9 | 21 | `,` | 直接返回 | `TOK_COMMA` |
| 10 | 22 | ` ` | 跳空白 | — |
| 11 | 23 | `n` | `lex_ident` | `TOK_IDENT("name")` |
| 12 | 27 | `)` | 直接返回 | `TOK_RPAREN` |
| 13 | 28 | ` ` | 跳空白 | — |
| 14 | 29 | `V` | `lex_ident` → 命中 | `TOK_KEYWORD(KW_VALUES)` |
| 15 | 35 | ` ` | 跳空白 | — |
| 16 | 36 | `(` | 直接返回 | `TOK_LPAREN` |
| 17 | 37 | `1` | `lex_number` | `TOK_NUMBER(1)` |
| 18 | 38 | `,` | 直接返回 | `TOK_COMMA` |
| 19 | 39 | ` ` | 跳空白 | — |
| 20 | 40 | `'` | `lex_string` | `TOK_STRING("Alice")` |
| 21 | 47 | `)` | 直接返回 | `TOK_RPAREN` |
| 22 | 48 | (结尾) | — | `TOK_EOF` |

### 2.9 小结

- Token 是源代码的最小表意单位，由类型 + 内容组成。
- 首字符决定法：看第一个字符就能确定 Token 大类。
- 关键字 = 命中关键字表的标识符；用线性查找足够快。
- Peek 机制让语法分析器能"看而不吃"，靠一个缓存实现。

---

## 3. 递归下降解析

### 3.1 什么是递归下降

**递归下降**（Recursive Descent）是一种手写语法分析器的技术。核心思想极其简单：

> **每条语法规则对应一个 C 函数。**

例如语法规则：

```
select_stmt → SELECT col_list FROM table WHERE expr_list
```

就对应一个函数：

```c
static ast_stmt_t *parse_select(parser_t *p);
```

函数从当前 Token 开始，按规则尝试匹配。匹配成功就返回 AST 节点；失败就返回 NULL。"递归"体现在规则可以引用自身（如表达式嵌套），"下降"体现在从最外层规则一步步深入到最内层。

### 3.2 为什么不用 Yacc/Bison

Yacc/Bison 是 LALR(1) 解析器生成器：你写语法规则，它生成 C 代码。看起来很省事，但本项目选择手写，原因如下：

| 维度 | 手写递归下降 | Yacc/Bison |
|---|---|---|
| **学习价值** | 高——每行代码都能看懂 | 低——生成代码难读 |
| **依赖** | 零 | 需要 bison 工具链 |
| **错误信息** | 可精确控制位置和提示 | 自动生成，难以定制 |
| **回溯** | 容易加（需要时） | LALR 不支持回溯 |
| **代码量** | 中（本项目 180 行） | 规则少，但生成代码多 |
| **调试** | 单步进入自己的函数 | 进入生成代码，痛苦 |
| **左递归** | 需手动改写为循环 | 自动处理 |
| **性能** | 好 | 好 |

**教学项目优先零依赖和学习价值**，所以选手写。SQLite、MySQL 也都是手写解析器，原因类似：错误信息可控、可读性强、易扩展。

### 3.3 解析器状态

```c
struct parser {
    lexer_t *lex;   // 持有词法分析器
    token_t  cur;   // 当前 Token（lookahead）
};
```

只有两个字段：一个 lexer，一个"当前 Token"。`cur` 始终持有"下一个待处理的 Token"，类似一个游标。

### 3.4 四个辅助函数

递归下降的所有魔法都建立在这四个小函数之上：

```c
// 消费当前 Token，从 lexer 读下一个进来
static void advance(parser_t *p) {
    p->cur = lexer_next(p->lex);
}

// 当前 Token 是某关键字吗？（不消费）
static bool is_kw(parser_t *p, keyword_t kw) {
    return p->cur.type == TOK_KEYWORD && p->cur.keyword == kw;
}

// 如果是某关键字就消费并返回 true，否则返回 false
static bool accept_kw(parser_t *p, keyword_t kw) {
    if (is_kw(p, kw)) { advance(p); return true; }
    return false;
}

// 必须是某关键字，否则返回 false（表示语法错误）
static bool expect_kw(parser_t *p, keyword_t kw) {
    if (!accept_kw(p, kw)) return false;
    return true;
}
```

| 函数 | 行为 | 类比 |
|---|---|---|
| `advance` | 吃掉当前 Token，读下一个 | 翻到下一页 |
| `is_kw` | 看一眼，不翻页 | 偷看下一页标题 |
| `accept_kw` | 是就吃掉，不是就放过 | "如果是这页就翻过去" |
| `expect_kw` | 必须是，否则报错 | "这页必须是 XX，否则书错了" |

### 3.5 顶层分发

```c
ast_stmt_t *parser_parse(parser_t *p) {
    if (p->cur.type != TOK_KEYWORD) return NULL;
    switch (p->cur.keyword) {
        case KW_SELECT: return parse_select(p);
        case KW_INSERT: return parse_insert(p);
        case KW_DELETE: return parse_delete(p);
        case KW_CREATE: return parse_create(p);
        default: return NULL;   // UPDATE 等未实现
    }
}
```

看第一个 Token 是哪个关键字，就分发给对应的解析函数。这是递归下降的入口：**一个 Token 决定一条语法分支**。

### 3.6 解析函数一览

```
parser_parse()   → 顶层分发
    ├── parse_select()  → SELECT (* | col_list) FROM table [WHERE ...]
    ├── parse_insert()  → INSERT INTO table (col_list) VALUES (val_list)
    ├── parse_delete()  → DELETE FROM table [WHERE ...]
    └── parse_create()  → CREATE TABLE table (col_def_list)

parse_where()    → WHERE expr (AND expr)*       （被 SELECT/DELETE 共用）
parse_expr()     → IDENT OP value                （单个比较表达式）
parse_value()    → NUMBER | STRING | NULL        （字面量）
```

每条规则对应一个函数，规则之间的依赖关系就是函数之间的调用关系。这就是"递归下降"的"下降"——从顶层规则一层层往下调。

### 3.7 一个简单例子：parse_value

```c
static bool parse_value(parser_t *p, ast_value_t *val) {
    if (p->cur.type == TOK_NUMBER) {
        if (strchr(p->cur.text, '.')) {
            val->type = VAL_FLOAT;
            val->float_val = p->cur.float_val;
        } else {
            val->type = VAL_INT;
            val->int_val = p->cur.int_val;
        }
        advance(p);
        return true;
    }
    if (p->cur.type == TOK_STRING) {
        val->type = VAL_STRING;
        strncpy(val->str_val, p->cur.text, 63);
        advance(p);
        return true;
    }
    if (accept_kw(p, KW_NULL)) {
        val->type = VAL_NULL;
        return true;
    }
    return false;   // 都不是，返回 false
}
```

读这段代码的方式：

1. 看当前 Token 是 `TOK_NUMBER`？是 → 填 `val`，advance，返回 true。
2. 看当前 Token 是 `TOK_STRING`？是 → 填 `val`，advance，返回 true。
3. 看当前 Token 是 `KW_NULL`？是 → 填 `val`，advance，返回 true。
4. 都不是 → 返回 false（让调用方决定怎么处理）。

**关键模式**：每个分支末尾都 `advance`，把当前 Token 吃掉，让游标前进到下一个。这是递归下降的"步进"。

### 3.8 小结

- 递归下降 = 一条规则一个函数，函数之间互相调用。
- 四个辅助函数 `advance/is_kw/accept_kw/expect_kw` 是全部基础设施。
- 看第一个 Token 决定走哪条分支，这就是"递归下降"的"下降"。

---

## 4. AST 概念

### 4.1 什么是 AST

**AST**（Abstract Syntax Tree，抽象语法树）是源代码的树形结构表示。它丢掉了所有对"理解含义"不重要的细节（空格、换行、括号），只保留语义骨架。

例如 `SELECT * FROM users WHERE id = 42` 的 AST：

```
ast_stmt_t {
    type       = AST_SELECT
    select_all = true
    table      = "users"
    num_where  = 1
    where[0]   = {
        type   = EXPR_COMPARE
        column = "id"
        op     = "="
        value  = { type=VAL_INT, int_val=42 }
    }
}
```

注意：原始 SQL 里的 `*`、`FROM`、`WHERE` 这些关键字都**不见了**——它们是语法标记，告诉解析器怎么组装 AST，组装完就丢掉。这就是"抽象"的含义：**只保留含义，丢掉形式**。

### 4.2 为什么需要 AST

| 用途 | 说明 |
|---|---|
| **解耦** | 解析器只管生成 AST，执行器只管读 AST，两者互不依赖 |
| **归一化** | `SELECT * FROM users` 和 `select * from users` 生成同一棵 AST |
| **可遍历** | 树形结构天然适合递归遍历，便于做优化、转换、打印 |
| **可序列化** | 想存盘、想跨进程传，结构体比字符串好处理 |
| **可扩展** | 加新语句类型只要加新字段/新枚举，不必动解析器框架 |

**没有 AST 会怎样？** 你得在执行时反复 `strcmp` 字符串，每次都重新解析。AST 把"理解"这件事做一次，做完之后所有人共享结果。

### 4.3 本项目的 AST 设计

为了教学简洁，本项目用**扁平结构体**而非指针树：

```c
#define AST_MAX_COLS 16     // 最多 16 列
#define AST_MAX_NAME 32     // 名字最长 31 字符
#define AST_MAX_WHERE 8     // 最多 8 个 WHERE 条件

typedef struct {
    stmt_type_t type;       // SELECT / INSERT / UPDATE / DELETE / CREATE
    char table[AST_MAX_NAME];          // 表名

    // SELECT 用
    char columns[AST_MAX_COLS][AST_MAX_NAME];  // 列名列表
    int  num_cols;
    bool select_all;                            // SELECT *

    // WHERE 用（SELECT/DELETE 共用）
    ast_expr_t where[AST_MAX_WHERE];   // 条件数组（AND 连接）
    int num_where;

    // INSERT 用
    ast_value_t values[AST_MAX_COLS];  // 值列表
    int num_values;

    // UPDATE 用（已预留，未实现）
    char set_cols[AST_MAX_COLS][AST_MAX_NAME];
    ast_value_t set_values[AST_MAX_COLS];
    int num_set;

    // CREATE 用
    ast_col_def_t col_defs[AST_MAX_COLS];  // 列定义
    int num_col_defs;
} ast_stmt_t;
```

**为什么用扁平数组而不是指针树？**

```c
// 本项目：扁平数组
ast_stmt_t {
    char columns[16][32];   // 直接内嵌
    ast_expr_t where[8];    // 直接内嵌
}

// 生产系统：指针树
struct ast_select {
    char **columns;         // 动态分配
    ast_expr_t *where;      // 动态分配
    ast_select_t *next;     // 链表
}
```

| | 扁平结构 | 指针树 |
|---|---|---|
| **分配** | 一次 calloc | 多次 malloc |
| **释放** | 一次 free | 递归 free，易漏 |
| **缓存友好** | 是（连续内存） | 否（指针跳转） |
| **上限** | 固定（16 列、8 条件） | 任意 |
| **嵌套** | 不支持 | 支持 |
| **适合** | 教学项目、简单 SQL 子集 | 生产数据库 |

教学项目优先**简单**：一次 `calloc` 搞定，`free` 一次完事，不必担心内存泄漏。

### 4.4 值类型

SQL 的字面量分四种：

```c
typedef enum { VAL_INT, VAL_FLOAT, VAL_STRING, VAL_NULL } val_type_t;

typedef struct {
    val_type_t type;
    int32_t int_val;
    float   float_val;
    char    str_val[64];
} ast_value_t;
```

| 类型 | 例 | 存储字段 |
|---|---|---|
| `VAL_INT` | `42` | `int_val` |
| `VAL_FLOAT` | `3.14` | `float_val` |
| `VAL_STRING` | `'Alice'` | `str_val` |
| `VAL_NULL` | `NULL` | （无） |

用联合体更省内存，但教学项目用结构体更直白，新手不必理解 union。

### 4.5 表达式

当前只支持**比较表达式**（`column OP value`）：

```c
typedef struct {
    expr_type_t type;                 // 目前只有 EXPR_COMPARE
    char column[AST_MAX_NAME];        // 列名
    char op[4];                       // 运算符: =, !=, <, >, <=, >=
    ast_value_t value;                // 比较值
} ast_expr_t;
```

例如 `age >= 18` 解析后：

```
ast_expr_t {
    type   = EXPR_COMPARE
    column = "age"
    op     = ">="
    value  = { type=VAL_INT, int_val=18 }
}
```

**不支持**：算术表达式（`a + b`）、嵌套表达式（`(a OR b) AND c`）、函数调用（`COUNT(*)`）。这些都需要把 `ast_expr_t` 改成指针树，是后续扩展方向。

### 4.6 列定义（CREATE TABLE 用）

```c
typedef enum { AST_COL_INT32, AST_COL_FLOAT } ast_col_type_t;

typedef struct {
    char name[AST_MAX_NAME];     // 列名
    ast_col_type_t type;         // INT 或 FLOAT
    bool nullable;               // 是否允许 NULL
    bool primary_key;            // 是否主键
} ast_col_def_t;
```

例如 `id INT PRIMARY KEY` 解析后：

```
ast_col_def_t {
    name        = "id"
    type        = AST_COL_INT32
    nullable    = false     // 主键隐含 NOT NULL
    primary_key = true
}
```

### 4.7 AST 打印函数

`ast.c` 里的 `ast_print` 把 AST 反向序列化成 SQL 文本，用于调试：

```c
void ast_print(const ast_stmt_t *s) {
    switch (s->type) {
        case AST_SELECT:
            printf("SELECT ");
            if (s->select_all) printf("*");
            else for (int i = 0; i < s->num_cols; i++)
                printf("%s%s", i ? ", " : "", s->columns[i]);
            printf(" FROM %s", s->table);
            break;
        // ... INSERT/DELETE/CREATE 同理
    }
    // 公共的 WHERE 子句打印
    if (s->num_where > 0) {
        printf(" WHERE ");
        for (int i = 0; i < s->num_where; i++) {
            if (i) printf(" AND ");
            printf("%s %s ", s->where[i].column, s->where[i].op);
            print_value(&s->where[i].value);
        }
    }
    printf("\n");
}
```

`i ? ", " : ""` 是个常用技巧：第一个元素前不加逗号，后续元素前加逗号空格。

### 4.8 小结

- AST 是源代码的树形语义表示，丢掉所有非语义细节。
- 本项目用扁平结构体，简单但上限固定。
- AST 是解析器和执行器之间的"接口"，两边互不依赖。

---

## 5. 语法规则详解

### 5.1 BNF 简介

**BNF**（Backus-Naur Form，巴科斯-诺尔范式）是描述语法的标准记法。规则形如：

```
<非终结符> → <符号1> <符号2> ...
```

其中 `<...>` 是非终结符（可继续展开），裸字符是终结符（Token）。例如：

```
<select_stmt> → SELECT <col_list> FROM <table> [WHERE <expr_list>]
<col_list>    → * | <ident> (, <ident>)*
```

读法：`<select_stmt>` 可以展开成 `SELECT` 后跟列列表、`FROM`、表名、可选的 `WHERE` 子句。

### 5.2 SELECT 语句

#### 5.2.1 BNF

```
<select_stmt> → SELECT <select_cols> FROM <ident> [WHERE <where_clause>]
<select_cols> → * | <ident> (, <ident>)*
<where_clause> → <expr> (AND <expr>)*
<expr>        → <ident> <op> <value>
<op>          → = | != | < | > | <= | >=
<value>       → <number> | <string> | NULL
```

#### 5.2.2 流程图

```
parse_select():
   ┌────────────┐
   │ 进入函数   │  cur == KW_SELECT
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ advance()  │  消费 SELECT
   └─────┬──────┘
         ▼
   ┌────────────────────────┐
   │ cur 是 OP 且 text=="*" │
   └────┬───────────┬───────┘
        │是          │否
        ▼            ▼
  ┌──────────┐  ┌───────────────────┐
  │select_all│  │ 循环:             │
  │ =true    │  │  while cur==IDENT │
  │ advance  │  │    记录列名       │
  └────┬─────┘  │    advance        │
       │        │    若非逗号则break │
       │        │    否则 advance   │
       │        └─────────┬─────────┘
       │                  │
       └────────┬─────────┘
                ▼
       ┌────────────────┐
       │ expect_kw(FROM)│  必须有 FROM
       └───────┬────────┘
               ▼
       ┌────────────────┐
       │ 记录表名       │  table = cur.text
       │ advance()      │
       └───────┬────────┘
               ▼
       ┌────────────────┐
       │ parse_where()  │  可选 WHERE
       └───────┬────────┘
               ▼
       ┌────────────────┐
       │ 返回 AST 节点  │
       └────────────────┘
```

#### 5.2.3 代码

```c
static ast_stmt_t *parse_select(parser_t *p) {
    advance(p);   // 消费 SELECT
    ast_stmt_t *s = calloc(1, sizeof(ast_stmt_t));
    s->type = AST_SELECT;

    if (p->cur.type == TOK_OP && p->cur.text[0] == '*') {
        s->select_all = true;
        advance(p);
    } else {
        while (p->cur.type == TOK_IDENT) {
            strncpy(s->columns[s->num_cols++], p->cur.text, AST_MAX_NAME - 1);
            advance(p);
            if (!accept_kw(p, KW_AND) && p->cur.type != TOK_COMMA) break;
            if (p->cur.type == TOK_COMMA) advance(p);
        }
    }

    if (!expect_kw(p, KW_FROM)) { free(s); return NULL; }
    strncpy(s->table, p->cur.text, AST_MAX_NAME - 1);
    advance(p);
    parse_where(p, s);
    return s;
}
```

#### 5.2.4 示例追踪

输入 `SELECT id, name FROM users WHERE age >= 18`：

| 步骤 | cur | 动作 | AST 状态 |
|---|---|---|---|
| 1 | `KW_SELECT` | advance | type=AST_SELECT |
| 2 | `IDENT(id)` | 不是 `*`，进列名循环 | columns[0]="id" |
| 3 | `,` | 是逗号，advance | — |
| 4 | `IDENT(name)` | 记录列名 | columns[1]="name" |
| 5 | `KW_FROM` | 非逗号，break 循环 | — |
| 6 | `KW_FROM` | expect_kw 通过 | — |
| 7 | `IDENT(users)` | 记录表名 | table="users" |
| 8 | `KW_WHERE` | parse_where 进入 | — |
| 9 | `IDENT(age)` | parse_expr | where[0].column="age" |
| 10 | `>=` | 记录运算符 | where[0].op=">=" |
| 11 | `18` | parse_value | where[0].value=18 |
| 12 | `EOF` | 无 AND，结束 | num_where=1 |

### 5.3 INSERT 语句

#### 5.3.1 BNF

```
<insert_stmt> → INSERT INTO <ident> ( <col_list> ) VALUES ( <val_list> )
<col_list>    → <ident> (, <ident>)*
<val_list>    → <value> (, <value>)*
```

#### 5.3.2 流程图

```
parse_insert():
   ┌────────────┐
   │ advance    │  消费 INSERT
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ expect INTO│
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ 记录表名   │
   │ advance    │
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ cur == "(" │
   └────┬───────┘
        │是
        ▼
   ┌────────────┐
   │ advance    │
   └─────┬──────┘
         ▼
   ┌────────────────────┐
   │ while cur==IDENT:  │  列名循环
   │   记录列名         │
   │   advance          │
   │   非逗号则 break   │
   └─────────┬──────────┘
             ▼
   ┌────────────┐
   │ expect ")" │
   └─────┬──────┘
         ▼
   ┌──────────────┐
   │ expect VALUES│
   └─────┬────────┘
         ▼
   ┌────────────┐
   │ expect "("  │
   └─────┬──────┘
         ▼
   ┌────────────────────────┐
   │ while parse_value 成功 │  值循环
   │   非逗号则 break       │
   └─────────┬──────────────┘
             ▼
   ┌────────────┐
   │ expect ")" │
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ 返回 AST   │
   └────────────┘
```

#### 5.3.3 代码

```c
static ast_stmt_t *parse_insert(parser_t *p) {
    advance(p);
    expect_kw(p, KW_INTO);
    ast_stmt_t *s = calloc(1, sizeof(ast_stmt_t));
    s->type = AST_INSERT;

    strncpy(s->table, p->cur.text, AST_MAX_NAME - 1);
    advance(p);

    if (p->cur.type == TOK_LPAREN) {
        advance(p);
        while (p->cur.type == TOK_IDENT) {
            strncpy(s->columns[s->num_cols++], p->cur.text, AST_MAX_NAME - 1);
            advance(p);
            if (p->cur.type != TOK_COMMA) break;
            advance(p);
        }
        if (p->cur.type == TOK_RPAREN) advance(p);
    }

    expect_kw(p, KW_VALUES);
    if (p->cur.type == TOK_LPAREN) {
        advance(p);
        while (parse_value(p, &s->values[s->num_values])) {
            s->num_values++;
            if (p->cur.type != TOK_COMMA) break;
            advance(p);
        }
        if (p->cur.type == TOK_RPAREN) advance(p);
    }
    return s;
}
```

### 5.4 DELETE 语句

#### 5.4.1 BNF

```
<delete_stmt> → DELETE FROM <ident> [WHERE <where_clause>]
```

#### 5.4.2 代码

```c
static ast_stmt_t *parse_delete(parser_t *p) {
    advance(p);                       // 消费 DELETE
    expect_kw(p, KW_FROM);            // 必须有 FROM
    ast_stmt_t *s = calloc(1, sizeof(ast_stmt_t));
    s->type = AST_DELETE;
    strncpy(s->table, p->cur.text, AST_MAX_NAME - 1);
    advance(p);
    parse_where(p, s);                // 可选 WHERE
    return s;
}
```

DELETE 是最简单的语句：`DELETE FROM` + 表名 + 可选 WHERE。复用 `parse_where` 是设计上的小亮点——SELECT 和 DELETE 的 WHERE 子句语法完全相同，没必要写两遍。

### 5.5 CREATE 语句

#### 5.5.1 BNF

```
<create_stmt>   → CREATE TABLE <ident> ( <col_def_list> )
<col_def_list>  → <col_def> (, <col_def>)*
<col_def>       → <ident> <col_type> [PRIMARY KEY]
<col_type>      → INT | FLOAT
```

#### 5.5.2 流程图

```
parse_create():
   ┌────────────┐
   │ advance    │  消费 CREATE
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ expect TABLE│
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ 记录表名   │
   │ advance    │
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ expect "("  │
   └─────┬──────┘
         ▼
   ┌─────────────────────────────┐
   │ while cur==IDENT:           │
   │   c = &col_defs[num_col_defs]│
   │   记录列名                  │
   │   advance                   │
   │   accept INT  → c->type=INT │
   │   accept FLOAT→ c->type=FLOAT│
   │   c->nullable = true        │
   │   if accept PRIMARY:        │
   │     expect KEY              │
   │     c->primary_key = true   │
   │     c->nullable = false     │
   │   num_col_defs++            │
   │   非逗号则 break            │
   └─────────┬───────────────────┘
             ▼
   ┌────────────┐
   │ expect ")" │
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ 返回 AST   │
   └────────────┘
```

#### 5.5.3 代码

```c
static ast_stmt_t *parse_create(parser_t *p) {
    advance(p);
    expect_kw(p, KW_TABLE);
    ast_stmt_t *s = calloc(1, sizeof(ast_stmt_t));
    s->type = AST_CREATE;

    strncpy(s->table, p->cur.text, AST_MAX_NAME - 1);
    advance(p);

    if (p->cur.type == TOK_LPAREN) {
        advance(p);
        while (p->cur.type == TOK_IDENT) {
            ast_col_def_t *c = &s->col_defs[s->num_col_defs];
            strncpy(c->name, p->cur.text, AST_MAX_NAME - 1);
            advance(p);
            if (accept_kw(p, KW_INT))       c->type = AST_COL_INT32;
            else if (accept_kw(p, KW_FLOAT)) c->type = AST_COL_FLOAT;
            c->nullable = true;
            if (accept_kw(p, KW_PRIMARY)) {
                expect_kw(p, KW_KEY);
                c->primary_key = true;
                c->nullable    = false;
            }
            s->num_col_defs++;
            if (p->cur.type != TOK_COMMA) break;
            advance(p);
        }
        if (p->cur.type == TOK_RPAREN) advance(p);
    }
    return s;
}
```

### 5.6 WHERE 子句

```c
static void parse_where(parser_t *p, ast_stmt_t *stmt) {
    if (!accept_kw(p, KW_WHERE)) return;     // 没有 WHERE，直接返回
    if (!parse_expr(p, &stmt->where[stmt->num_where])) return;
    stmt->num_where++;
    while (accept_kw(p, KW_AND) && stmt->num_where < AST_MAX_WHERE) {
        if (!parse_expr(p, &stmt->where[stmt->num_where])) break;
        stmt->num_where++;
    }
}
```

逻辑：

1. 没有 `WHERE` 关键字？返回，stmt 的 `num_where` 保持 0。
2. 有 `WHERE`？解析第一个表达式。
3. 之后每遇到一个 `AND`，就再解析一个表达式，直到没有 `AND` 或达到上限 `AST_MAX_WHERE`（8）。

**注意**：`accept_kw(p, KW_AND)` 既判断又消费，是循环条件的精髓——`AND` 被吃掉后才进入循环体解析下一个表达式。

### 5.7 表达式

```c
static bool parse_expr(parser_t *p, ast_expr_t *expr) {
    if (p->cur.type != TOK_IDENT) return false;   // 必须列名开头
    expr->type = EXPR_COMPARE;
    strncpy(expr->column, p->cur.text, AST_MAX_NAME - 1);
    advance(p);
    if (p->cur.type != TOK_OP) return false;      // 必须运算符
    strncpy(expr->op, p->cur.text, 3);
    advance(p);
    return parse_value(p, &expr->value);          // 必须值
}
```

形式固定：`列名 运算符 值`。任何不符合这个顺序的都返回 false。

### 5.8 支持的 SQL 子集汇总

| 语句 | 示例 |
|---|---|
| SELECT | `SELECT * FROM users WHERE id >= 10 AND age < 30` |
| INSERT | `INSERT INTO users (id, name) VALUES (1, 'Alice')` |
| DELETE | `DELETE FROM users WHERE id = 5` |
| CREATE | `CREATE TABLE users (id INT PRIMARY KEY, age INT, score FLOAT)` |

### 5.9 不支持（后续可扩展）

- UPDATE 语句（AST 已预留 `set_cols/set_values` 字段）
- JOIN / 子查询
- GROUP BY / HAVING / ORDER BY
- 聚合函数 (COUNT, SUM, AVG)
- OR 条件（仅 AND）
- 嵌套表达式、算术表达式
- 字符串转义、注释

### 5.10 小结

- 每条 SQL 语句对应一个 BNF 规则和一个 C 函数。
- 流程图直观展示"看 Token → 决定分支 → 消费 → 填 AST"的循环。
- WHERE 子句被 SELECT 和 DELETE 复用，体现"规则共享"。

---

## 6. 错误处理

### 6.1 错误的分类

| 类别 | 例子 | 谁负责 |
|---|---|---|
| **词法错误** | `SELECT @ FROM users`（不认识的字符 `@`） | Lexer |
| **语法错误** | `SELECT FROM users`（缺列名） | Parser |
| **语义错误** | `SELECT * FROM nonexistent_table` | 后续章节 |

本项目只处理前两类，且策略简单：**返回 NULL 表示失败**。

### 6.2 词法错误

词法分析器遇到不认识的字符时，返回 `TOK_ERROR`：

```c
token_t err = make_tok(TOK_ERROR);
err.text[0] = c;
err.text[1] = '\0';
lex->pos++;
return err;
```

解析器看到 `TOK_ERROR` 时，由于它既不是关键字也不是标识符等期望类型，相关 `expect_kw` 或循环条件会失败，最终 `parser_parse` 返回 NULL。

### 6.3 语法错误

语法错误通过 `expect_kw` 返回 false 体现：

```c
static bool expect_kw(parser_t *p, keyword_t kw) {
    if (!accept_kw(p, kw)) return false;   // 不是期望的关键字
    return true;
}
```

例如 `SELECT FROM users`（缺列名或 `*`）：

1. `parse_select` 消费 SELECT。
2. cur 是 `KW_FROM`，既不是 `*` 也不是 `TOK_IDENT`，列名循环不进入。
3. `expect_kw(p, KW_FROM)` 成功，继续。
4. 但此时 `num_cols=0` 且 `select_all=false`，AST 处于"既没选 * 也没列名"的非法状态。

当前实现**没有严格检查这种情况**，会返回一个不完整的 AST。这是教学项目的简化。生产级解析器会在 `parse_select` 末尾加：

```c
if (!s->select_all && s->num_cols == 0) {
    free(s);
    return NULL;   // 既不是 * 也没有列名，报错
}
```

### 6.4 错误恢复

**错误恢复**（Error Recovery）指遇到错误后不立即退出，而是跳过一些 Token 继续解析，从而一次性报告多个错误。例如 GCC 一次编译能报 10 个错，而不是第一个就停。

本项目**不做错误恢复**：遇到第一个错误就返回 NULL。理由：

- 教学项目，简单优先。
- SQL 通常一次执行一条，报一个错就够。
- 错误恢复会大幅增加代码复杂度。

生产数据库的错误恢复策略：

| 数据库 | 策略 |
|---|---|
| SQLite | 跳到下一条语句（`;` 之后）继续 |
| PostgreSQL | 跳过出错 Token，尝试同步到语句边界 |
| MySQL | 报错后停止当前语句 |

### 6.5 错误信息改进建议

当前实现只返回 NULL，不告诉用户**错在哪**。改进方向：

```c
// 改进后的 expect_kw
static bool expect_kw(parser_t *p, keyword_t kw) {
    if (!accept_kw(p, kw)) {
        fprintf(stderr, "语法错误: 期望 %s，但遇到 '%s' (位置 %d)\n",
                keyword_name(kw), p->cur.text, p->lex->pos);
        return false;
    }
    return true;
}
```

更进一步可以记录行列号：

```
语法错误: 期望 FROM，但遇到 'WHERE' (第 1 行 第 12 列)
```

这需要 lexer 在扫描时记录换行符位置。教学项目从简，但这是走向"用户友好"的第一步。

### 6.6 小结

- 错误分词法、语法、语义三类，本项目只处理前两类。
- 策略：返回 NULL 表示失败，不做错误恢复。
- 改进方向：加位置信息、加错误消息、加恢复机制。

---

## 7. 代码逐行解读

本节对六个源文件的关键函数逐行解读，作为"参考手册"。新手读代码时遇到不懂的函数，来这里查。

### 7.1 lexer.h

```c
#ifndef MINIDB_LEXER_H
#define MINIDB_LEXER_H

#include <stdint.h>
#include <stdbool.h>
```

`#ifndef` 是头文件保护，防止重复包含。`<stdint.h>` 提供 `int32_t` 等定宽整数，`<stdbool.h>` 提供 `bool`。

```c
typedef enum {
    TOK_KEYWORD = 0,
    TOK_IDENT = 1,
    // ... 11 种类型
} token_type_t;
```

显式从 0 开始编号，便于调试时 `(int)type` 直接看值。

```c
typedef enum {
    KW_SELECT, KW_FROM, KW_WHERE, ...
} keyword_t;
```

关键字枚举，顺序就是 `kw_table` 里的顺序。

```c
typedef struct {
    token_type_t type;
    keyword_t    keyword;
    char         text[64];
    int32_t      int_val;
    float        float_val;
} token_t;
```

Token 结构体。`text[64]` 限制 Token 文本最长 63 字符（留一个给 `\0`）。

```c
typedef struct lexer lexer_t;   // 不透明类型

lexer_t *lexer_create(const char *sql);
void     lexer_destroy(lexer_t *lex);
token_t  lexer_next(lexer_t *lex);
token_t  lexer_peek(lexer_t *lex);
```

**不透明指针**（Opaque Pointer）模式：头文件只声明 `struct lexer`，不定义其字段。定义藏在 `lexer.c` 里。好处：外部代码不能直接访问 lexer 内部字段，只能通过这四个函数操作，**封装**自然成立。

### 7.2 lexer.c

#### 7.2.1 结构体定义

```c
struct lexer {
    const char *src;     // 源字符串，不拥有，不 free
    int pos;             // 当前扫描位置
    int len;             // 源串长度
    token_t peeked;      // 预读缓存
    bool has_peek;       // 缓存是否有效
};
```

`src` 是 `const char *`，lexer 不拥有这块内存，`lexer_destroy` 不 free 它。这是"借用"语义，调用方负责源串的生命周期。

#### 7.2.2 lexer_create / lexer_destroy

```c
lexer_t *lexer_create(const char *sql) {
    lexer_t *lex = malloc(sizeof(lexer_t));
    lex->src = sql;
    lex->pos = 0;
    lex->len = (int)strlen(sql);
    lex->has_peek = false;
    return lex;
}

void lexer_destroy(lexer_t *lex) {
    free(lex);
}
```

`create` 分配并初始化，`destroy` 释放。注意 `has_peek` 初始化为 false——还没有预读任何 Token。

#### 7.2.3 make_tok

```c
static token_t make_tok(token_type_t type) {
    token_t t;
    memset(&t, 0, sizeof(t));   // 全部清零
    t.type = type;
    return t;
}
```

辅助函数：创建一个指定类型的 Token，其余字段清零。`memset` 保证 `text`、`int_val` 等字段不会残留垃圾值。

#### 7.2.4 lex_number

```c
static token_t lex_number(lexer_t *lex) {
    token_t t = make_tok(TOK_NUMBER);
    int start = lex->pos;
    bool is_float = false;

    while (lex->pos < lex->len &&
           (isdigit((unsigned char)lex->src[lex->pos]) ||
            lex->src[lex->pos] == '.')) {
        if (lex->src[lex->pos] == '.') is_float = true;
        lex->pos++;
    }
```

`start` 记录数字开始位置，循环结束后用 `lex->pos - start` 算长度。`(unsigned char)` 强制转换避免 `isdigit` 在负 char 上的未定义行为。

```c
    int n = lex->pos - start;
    if (n >= 64) n = 63;                 // 防溢出
    strncpy(t.text, lex->src + start, n);
    t.text[n] = '\0';

    if (is_float) t.float_val = (float)atof(t.text);
    else          t.int_val   = atoi(t.text);
    return t;
}
```

复制文本到 `t.text`，手动加 `\0`（`strncpy` 不保证加）。然后根据是否有小数点调 `atof` 或 `atoi` 预转换。

#### 7.2.5 lexer_next

```c
token_t lexer_next(lexer_t *lex) {
    if (lex->has_peek) {                 // 优先返回缓存
        lex->has_peek = false;
        return lex->peeked;
    }

    while (lex->pos < lex->len && isspace((unsigned char)lex->src[lex->pos]))
        lex->pos++;                      // 跳空白

    if (lex->pos >= lex->len) return make_tok(TOK_EOF);

    char c = lex->src[lex->pos];
    if (isdigit((unsigned char)c)) return lex_number(lex);
    if (isalpha((unsigned char)c) || c == '_') return lex_ident(lex);
    if (c == '\'') return lex_string(lex);
    if (c == '(') { lex->pos++; return make_tok(TOK_LPAREN); }
    if (c == ')') { lex->pos++; return make_tok(TOK_RPAREN); }
    if (c == ',') { lex->pos++; return make_tok(TOK_COMMA); }
    if (c == ';') { lex->pos++; return make_tok(TOK_SEMICOLON); }
    if (c == '*') { token_t t = make_tok(TOK_OP); t.text[0]='*'; t.text[1]='\0'; lex->pos++; return t; }

    if (c == '=' || c == '!' || c == '<' || c == '>') {
        token_t t = make_tok(TOK_OP);
        t.text[0] = c;
        lex->pos++;
        if (lex->pos < lex->len && lex->src[lex->pos] == '=') {
            t.text[1] = '=';
            t.text[2] = '\0';
            lex->pos++;
        } else {
            t.text[1] = '\0';
        }
        return t;
    }

    token_t err = make_tok(TOK_ERROR);
    err.text[0] = c;
    err.text[1] = '\0';
    lex->pos++;
    return err;
}
```

逐段读：

1. **缓存优先**：如果 `has_peek` 为真，返回缓存并清空标志。
2. **跳空白**：`while` 循环吞掉所有空白字符。
3. **EOF 检查**：到达末尾返回 `TOK_EOF`。
4. **首字符分发**：根据 `c` 调用对应的 `lex_*` 函数或直接构造标点 Token。
5. **双字符运算符**：`=`、`!`、`<`、`>` 后可能跟 `=`，检查后吞掉。
6. **兜底错误**：以上都不匹配，返回 `TOK_ERROR`。

#### 7.2.6 lexer_peek

```c
token_t lexer_peek(lexer_t *lex) {
    if (!lex->has_peek) {
        lex->peeked = lexer_next(lex);   // 真正读一次
        lex->has_peek = true;
    }
    return lex->peeked;
}
```

如果缓存空就调 `lexer_next` 填充，然后返回缓存。**关键**：`lexer_next` 内部会检查 `has_peek`，所以这里调 `lexer_next` 不会无限递归——此时 `has_peek` 是 false，`lexer_next` 会走真正的扫描路径。

### 7.3 parser.h

```c
#ifndef MINIDB_PARSER_H
#define MINIDB_PARSER_H

#include "ast.h"

typedef struct parser parser_t;   // 不透明

parser_t    *parser_create(const char *sql);
void         parser_destroy(parser_t *p);
ast_stmt_t  *parser_parse(parser_t *p);

#endif
```

同样是不透明指针模式。接口极简：创建、解析、销毁。

### 7.4 parser.c

#### 7.4.1 结构体与辅助函数

```c
struct parser {
    lexer_t *lex;
    token_t  cur;       // 当前 lookahead
};

static void advance(parser_t *p) { p->cur = lexer_next(p->lex); }
static bool is_kw(parser_t *p, keyword_t kw) {
    return p->cur.type == TOK_KEYWORD && p->cur.keyword == kw;
}
static bool accept_kw(parser_t *p, keyword_t kw) {
    if (is_kw(p, kw)) { advance(p); return true; }
    return false;
}
static bool expect_kw(parser_t *p, keyword_t kw) {
    if (!accept_kw(p, kw)) return false;
    return true;
}
```

`cur` 是"当前 Token"，始终代表"下一个待处理的 Token"。每次 `advance` 都从 lexer 拉一个新的进来。

#### 7.4.2 parser_create

```c
parser_t *parser_create(const char *sql) {
    parser_t *p = malloc(sizeof(parser_t));
    p->lex = lexer_create(sql);
    p->cur = lexer_next(p->lex);   // 预读第一个 Token
    return p;
}
```

创建时立即预读第一个 Token，让 `cur` 有值，后续 `parser_parse` 可以直接看 `p->cur`。

#### 7.4.3 parse_value / parse_expr / parse_where

已在第 3、5 章详述，此处不重复。

#### 7.4.4 parser_parse

```c
ast_stmt_t *parser_parse(parser_t *p) {
    if (p->cur.type != TOK_KEYWORD) return NULL;
    switch (p->cur.keyword) {
        case KW_SELECT: return parse_select(p);
        case KW_INSERT: return parse_insert(p);
        case KW_DELETE: return parse_delete(p);
        case KW_CREATE: return parse_create(p);
        default: return NULL;
    }
}
```

顶层分发：看第一个 Token 是哪个关键字，调对应函数。`KW_UPDATE` 等未实现的关键字走 `default` 返回 NULL。

### 7.5 ast.h

```c
#define AST_MAX_COLS 16
#define AST_MAX_NAME 32
#define AST_MAX_WHERE 8
```

三个容量上限。改这三个宏就能调整 AST 的容量，但会改变 `sizeof(ast_stmt_t)`。

```c
typedef enum {
    AST_SELECT = 0,
    AST_INSERT = 1,
    AST_UPDATE = 2,
    AST_DELETE = 3,
    AST_CREATE = 4,
} stmt_type_t;
```

语句类型枚举。`AST_UPDATE` 已定义但解析器未实现，是"预留位"。

```c
typedef struct {
    stmt_type_t type;
    char table[AST_MAX_NAME];
    char columns[AST_MAX_COLS][AST_MAX_NAME];
    int num_cols;
    bool select_all;
    ast_expr_t where[AST_MAX_WHERE];
    int num_where;
    ast_value_t values[AST_MAX_COLS];
    int num_values;
    char set_cols[AST_MAX_COLS][AST_MAX_NAME];
    ast_value_t set_values[AST_MAX_COLS];
    int num_set;
    ast_col_def_t col_defs[AST_MAX_COLS];
    int num_col_defs;
} ast_stmt_t;
```

**联合体思路**：不同语句类型用同一结构体的不同字段。`SELECT` 用 `columns/select_all`，`INSERT` 用 `columns/values`，`CREATE` 用 `col_defs`，`UPDATE` 用 `set_cols/set_values`。所有语句共用 `table` 和 `where`。

这种"胖结构体"比" tagged union"省事：不必每次 `switch(type)` 选不同子结构体，但浪费内存（一条 SELECT 语句也带着 `col_defs` 的空间）。教学项目可接受。

### 7.6 ast.c

```c
static void print_value(const ast_value_t *v) {
    switch (v->type) {
        case VAL_INT:    printf("%d", v->int_val); break;
        case VAL_FLOAT:  printf("%f", v->float_val); break;
        case VAL_STRING: printf("'%s'", v->str_val); break;
        case VAL_NULL:   printf("NULL"); break;
    }
}
```

值打印：字符串加单引号还原 SQL 语法，NULL 大写。

```c
void ast_print(const ast_stmt_t *s) {
    switch (s->type) {
        case AST_SELECT:
            printf("SELECT ");
            if (s->select_all) printf("*");
            else for (int i = 0; i < s->num_cols; i++)
                printf("%s%s", i ? ", " : "", s->columns[i]);
            printf(" FROM %s", s->table);
            break;
        // ... 其他语句类型
    }
    // 公共 WHERE 打印
    if (s->num_where > 0) {
        printf(" WHERE ");
        for (int i = 0; i < s->num_where; i++) {
            if (i) printf(" AND ");
            printf("%s %s ", s->where[i].column, s->where[i].op);
            print_value(&s->where[i].value);
        }
    }
    printf("\n");
}
```

`ast_print` 是**反向操作**：AST → SQL 文本。主要用于调试，确认解析正确。注意它不是"原样还原"——格式可能和输入不同（如多余空格会被规范化）。

### 7.7 小结

- 六个文件分工清晰：lexer.h/c 管词法，parser.h/c 管语法，ast.h/c 管数据结构。
- 不透明指针封装内部状态。
- 辅助函数 `make_tok`、`advance`、`is_kw` 等消除重复代码。

---

## 8. 枚举陷阱

### 8.1 问题描述

看 `lookup_keyword` 的签名：

```c
static int lookup_keyword(const char *text) {
    for (int i = 0; kw_table[i].text; i++) {
        if (strcasecmp(text, kw_table[i].text) == 0)
            return (int)kw_table[i].kw;
    }
    return -1;
}
```

**为什么返回 `int` 而不是 `keyword_t`？**

直觉上，这个函数查找关键字，返回 `keyword_t` 似乎更自然。但如果写成：

```c
static keyword_t lookup_keyword(const char *text) {
    // ...
    return (keyword_t)-1;   // 不是关键字
}
```

调用方：

```c
keyword_t kw = lookup_keyword(text);
if (kw >= 0) {   // 是关键字？
    // ...
}
```

**这里藏着一个致命陷阱**。

### 8.2 C 标准怎么说

C 标准（C11 6.7.2.2）规定：

> 枚举的底层类型由实现定义（implementation-defined），可以是 `int` 或任何足够表示所有枚举值的整数类型。枚举常量本身的类型是 `int`，但枚举**类型**的底层类型可能是有符号或无符号。

翻译成人话：

- `enum { A, B, C }` 里的 `A/B/C` 是 `int` 类型，值 0/1/2。
- 但 `enum E { A, B, C }` 这个**类型 E** 的底层可能是 `int`，也可能是 `unsigned int`，**编译器说了算**。

### 8.3 GCC 的选择

GCC 在满足以下条件时，会把枚举类型选为**无符号**：

- 枚举所有非负值；
- 没有显式负值；
- 用 `-fshort-enums`（ARM 默认开启，x86 也可能）。

`keyword_t` 全是非负值：

```c
typedef enum {
    KW_SELECT, KW_FROM, KW_WHERE, ...   // 0, 1, 2, ...
} keyword_t;
```

GCC 可能选 `unsigned int` 作为 `keyword_t` 的底层类型。

### 8.4 陷阱触发

假设 `keyword_t` 底层是 `unsigned int`，看这段代码：

```c
keyword_t kw = (keyword_t)(-1);   // 0xFFFFFFFF
if (kw >= 0) {
    // 以为这里不会执行？
    printf("这是关键字 %d\n", (int)kw);
}
```

**`kw >= 0` 永远为真**！因为无符号数永远 >= 0。`(int)kw` 会变成 -1，于是 `lookup_keyword` 对任何非关键字字符串都返回 -1，调用方却把它当成"是关键字"。

### 8.5 本项目的规避

```c
static int lookup_keyword(const char *text) {
    // ...
    return -1;   // 返回 int，不是 keyword_t
}
```

返回 `int` 而非 `keyword_t`。`int` 是有符号的，`-1 >= 0` 正确地为假。调用方：

```c
int kw = lookup_keyword(t.text);
if (kw >= 0) {                 // 安全
    t.type = TOK_KEYWORD;
    t.keyword = (keyword_t)kw; // 此时再转回 keyword_t
}
```

### 8.6 其他规避方式

| 方式 | 代码 | 评价 |
|---|---|---|
| 返回 int | `int lookup_keyword(...)` | 本项目采用，最简单 |
| 加哨兵枚举 | `enum { KW_INVALID=-1, KW_SELECT, ... }` | 强制枚举有负值，底层必为有符号 |
| 用 bool 出参 | `bool lookup_keyword(const char*, keyword_t *out)` | 最严谨，但啰嗦 |
| 编译选项 | `-fno-short-enums` | 平台相关，不可移植 |

**哨兵枚举**是最优雅的方案：

```c
typedef enum {
    KW_INVALID = -1,   // 强制底层为有符号
    KW_SELECT = 0,
    KW_FROM,
    // ...
} keyword_t;
```

有了 `KW_INVALID = -1`，编译器必须选有符号类型，`(keyword_t)(-1)` 就是真正的 -1。

### 8.7 如何检测这类 bug

```bash
# 用 -Wsign-compare 让编译器警告有符号/无符号比较
gcc -Wsign-compare -c lexer.c

# 用 -Wenum-compare 警告枚举混用
gcc -Wenum-compare -c lexer.c
```

但**这个陷阱不会被 `-Wsign-compare` 抓到**——因为 `kw >= 0` 中 `0` 会被提升到 `unsigned`，比较是合法的。最可靠的防御是**返回 int** 或**加哨兵枚举**。

### 8.8 真实案例

这类 bug 在嵌入式和 ARM 平台多发（ARM 默认 `-fshort-enums`）。Linux 内核、SQLite 都踩过类似的坑。SQLite 的做法：

```c
// SQLite 的 token 类型用 int 而非 enum
#define TK_SELECT  1
#define TK_FROM    2
// ...
```

直接用 `#define`，彻底绕开枚举类型问题。这是另一种思路：**不用枚举类型，只用枚举常量**。

### 8.9 小结

- C 枚举类型的底层有符号性由编译器决定，GCC 可能选无符号。
- 全非负值的枚举赋 `-1` 后，`>= 0` 永远为真——陷阱。
- 规避：返回 `int`、加哨兵枚举、或用 `#define`。
- 本项目用"返回 int"，简单有效。

---

## 9. 手写 vs 工具生成

### 9.1 工具简介

| 工具 | 作用 | 输入 | 输出 |
|---|---|---|---|
| **flex** | 词法分析器生成器 | `.l` 规则文件 | C 词法分析器 |
| **bison** | 语法分析器生成器 | `.y` 规则文件 | C 语法分析器 |
| **re2c** | 词法分析器生成器 | `.re` 规则文件 | C 词法分析器 |
| **ANTLR** | 语法分析器生成器 | `.g4` 规则文件 | 多语言解析器 |

### 9.2 flex 示例

同样的词法规则，用 flex 写：

```flex
%{
#include "tokens.h"
%}

%%

SELECT|select    { return KW_SELECT; }
FROM|from        { return KW_FROM; }
WHERE|where      { return KW_WHERE; }
[a-zA-Z_][a-zA-Z0-9_]*  { yylval.str = strdup(yytext); return IDENT; }
[0-9]+           { yylval.ival = atoi(yytext); return NUMBER; }
[0-9]+"."[0-9]+  { yylval.fval = atof(yytext); return FLOAT; }
'[^']*'          { yylval.str = strdup(yytext+1); return STRING; }
=|!=|<=|>=|<|>   { yylval.str = strdup(yytext); return OP; }
[ \t\n]+         { /* 跳空白 */ }
.                { return ERROR; }

%%
```

20 行规则生成几百行 C 代码。看起来省事，但：

- 你得装 flex。
- 生成代码不可读，调试痛苦。
- 错误信息是 flex 默认的，定制困难。
- `yylval` 全局变量不线程安全（除非加 `%option reentrant`）。

### 9.3 bison 示例

```bison
%token KW_SELECT KW_FROM KW_WHERE IDENT NUMBER STRING OP

%%

select_stmt : KW_SELECT select_cols KW_FROM IDENT where_clause
            ;

select_cols : '*'
            | ident_list
            ;

ident_list  : IDENT
            | ident_list ',' IDENT
            ;

where_clause : /* 空 */
             | KW_WHERE expr_list
             ;

expr_list   : expr
            | expr_list KW_AND expr
            ;

expr        : IDENT OP value
            ;

value       : NUMBER | STRING | KW_NULL
            ;

%%
```

bison 帮你处理 LALR(1) 状态机、移进-归约冲突、左递归。但代价是：

- 学习曲线陡（理解 LALR、移进-归约不是新手友好）。
- 生成的 `yyparse` 函数是个巨大状态机，单步调试几乎不可能。
- 错误信息默认是 `syntax error`，要定制得写 `yyerror` 和 `%error-verbose`。

### 9.4 全面对比

| 维度 | 手写（本项目） | flex/bison |
|---|---|---|
| **依赖** | 零 | 需要工具链 |
| **学习价值** | 高——每行可读 | 低——生成代码难读 |
| **代码量（手写部分）** | 150+180=330 行 | 20+20=40 行规则 |
| **代码量（实际编译）** | 330 行 | 几千行生成代码 |
| **错误信息** | 完全可控 | 默认差，定制麻烦 |
| **性能** | 好 | 好 |
| **可读性** | 高 | 低 |
| **可调试性** | 高（单步进入自己的函数） | 低（进入生成代码） |
| **可扩展性** | 中（改函数） | 中（改规则再生成） |
| **线程安全** | 是（无全局变量） | 默认否（yylval 全局） |
| **左递归** | 需手动改循环 | 自动处理 |
| **回溯** | 容易加 | LALR 不支持 |
| **语法表达能力** | 任意 LL(k) | LALR(1) |

### 9.5 工业界的选择

| 项目 | Lexer | Parser | 理由 |
|---|---|---|---|
| **SQLite** | 手写 | 手写 | 嵌入式、零依赖、错误信息可控 |
| **MySQL** | 手写 | 手写 (yacc 历史已替换) | 性能、可读性 |
| **PostgreSQL** | flex | bison | 历史原因、语法复杂 |
| **Oracle** | 手写 | 手写 | 商业机密、性能 |
| **DuckDB** | 手写 | 手写 | 嵌入式、现代 |
| **Clang** | 手写 | 手写 | C++ 语法需要无限回溯 |

**趋势**：现代数据库越来越多选择手写。原因：错误信息可控、可读性强、易扩展、零依赖。

### 9.6 何时该用工具

| 场景 | 推荐 |
|---|---|
| 教学项目 | 手写 |
| 嵌入式 / 零依赖 | 手写 |
| 语法极复杂（如 C++） | 手写 + 递归下降 + 回溯 |
| 语法中等复杂、团队不熟编译原理 | flex/bison |
| 需要快速原型 | flex/bison 或 ANTLR |
| 跨语言（生成 C++/Java/Python） | ANTLR |

### 9.7 小结

- 手写和工具生成各有适用场景。
- 教学项目选手写：零依赖、高学习价值、错误信息可控。
- 现代数据库趋势是手写。

---

## 10. 与真实数据库对比

### 10.1 整体对比

| 特性 | miniDB | SQLite | PostgreSQL | MySQL |
|---|---|---|---|---|
| Lexer | 手写 | 手写 | flex | 手写 |
| Parser | 递归下降 | 递归下降 (Lemon) | bison (LALR) | 手写 |
| AST | 扁平结构 | 标签联合 | 指针树 | 指针树 |
| 错误恢复 | 无 | 有 | 有 | 有 |
| 子查询 | 不支持 | 支持 | 支持 | 支持 |
| JOIN | 不支持 | 支持 | 支持 | 支持 |
| 聚合 | 不支持 | 支持 | 支持 | 支持 |
| CTE | 不支持 | 支持 | 支持 | 支持 |
| 窗口函数 | 不支持 | 支持 | 支持 | 支持 |

### 10.2 SQLite 的解析器

SQLite 用自研的 **Lemon** 解析器生成器（类似 bison 但线程安全、错误信息更好）。Lemon 生成 LALR(1) 解析器。SQLite 的 AST 用**标签联合**：

```c
// SQLite 简化
struct Select {
    ExprList *pEList;       // SELECT 列
    SrcList *pSrc;          // FROM 子句
    Expr *pWhere;           // WHERE
    Expr *pGroupBy;         // GROUP BY
    Expr *pHaving;          // HAVING
    Select *pPrior;         // 复合查询链
    // ...
};
```

每个字段都是指针，动态分配，支持任意嵌套。比 miniDB 的扁平结构灵活得多，但管理复杂。

### 10.3 PostgreSQL 的解析器

PostgreSQL 用 flex + bison。语法规则文件 `gram.y` 有 17000+ 行！AST 节点种类上百个，每种语句一个结构体：

```c
// PostgreSQL 简化
typedef struct SelectStmt {
    NodeTag type;
    List *targetList;       // SELECT 列
    List *fromClause;       // FROM
    Node *whereClause;      // WHERE
    List *groupClause;      // GROUP BY
    // ... 几十个字段
} SelectStmt;
```

PostgreSQL 的解析器能处理：

- 任意嵌套子查询
- CTE (WITH 子句)
- 窗口函数
- LATERAL JOIN
- 集合操作 (UNION/INTERSECT/EXCEPT)
- 类型转换、类型推断

miniDB 只实现了其中的"SELECT 列 + FROM + WHERE"这一小角。

### 10.4 复杂度对比

| | miniDB | SQLite | PostgreSQL |
|---|---|---|---|
| 语法规则行数 | ~30 (BNF) | ~600 (Lemon) | ~17000 (bison) |
| AST 节点种类 | 1 (ast_stmt_t) | ~20 | ~100 |
| 支持语句种类 | 4 | ~30 | ~50 |
| 关键字数 | 19 | ~250 | ~500 |

### 10.5 miniDB 的位置

miniDB 的解析器是一个**教学级最小实现**，它的价值在于：

- 用 330 行代码完整展示"词法 + 语法 → AST"的管线。
- 每行代码都能讲清楚，没有黑盒。
- 覆盖 SQL 最核心的 4 条语句（SELECT/INSERT/DELETE/CREATE）。

它**不是**生产级实现，缺：

- 子查询、JOIN、聚合、GROUP BY、ORDER BY、LIMIT
- OR 条件、嵌套表达式、算术表达式
- 类型系统、约束检查
- 错误恢复、错误位置
- 注释、字符串转义、标识符引号

但这些"缺"正是教学的意义——先理解最小核心，再逐步加复杂度。

### 10.6 小结

- miniDB 是教学级最小实现，330 行覆盖 4 条语句。
- SQLite/PostgreSQL 是生产级，几万行规则、上百种 AST 节点。
- 理解 miniDB 后，看 SQLite 源码会容易很多——结构相同，只是更复杂。

---

## 11. 习题

### 11.1 基础题

**题 1**：画出 `DELETE FROM users WHERE id = 5 AND age > 20` 的 Token 流（类似第 2.8 节的表格）。

**题 2**：写出 `SELECT name, age FROM students` 解析后的 AST（类似第 4.1 节的展开）。

**题 3**：解释为什么 `lexer_peek` 不会导致无限递归。

**题 4**：`expect_kw` 和 `accept_kw` 的区别是什么？各举一个使用场景。

**题 5**：为什么 `lookup_keyword` 用线性查找而不是哈希表？

### 11.2 实现题

**题 6**：修改 `lex_number`，使其在遇到多个小数点时返回 `TOK_ERROR`。

提示：

```c
int dot_count = 0;
while (...) {
    if (lex->src[lex->pos] == '.') {
        if (++dot_count > 1) return make_tok(TOK_ERROR);
        is_float = true;
    }
    lex->pos++;
}
```

**题 7**：实现 `parse_update`，支持 `UPDATE users SET age = 20 WHERE id = 1`。

提示：AST 已预留 `set_cols/set_values/num_set` 字段。BNF：

```
<update_stmt> → UPDATE <ident> SET <set_list> [WHERE <where_clause>]
<set_list>    → <set_pair> (, <set_pair>)*
<set_pair>    → <ident> = <value>
```

在 `parser_parse` 的 switch 里加 `case KW_UPDATE: return parse_update(p);`。

**题 8**：实现 OR 条件支持。当前 `parse_where` 只处理 AND，改为同时支持 OR。

挑战：OR 的优先级低于 AND（`a AND b OR c` = `(a AND b) OR c`），需要构造嵌套表达式树。如果坚持扁平结构，可以只支持"全 AND 或全 OR"的简化版。

**题 9**：给 `expect_kw` 加错误信息输出，格式：`语法错误: 期望 FROM，但遇到 'WHERE'`。

**题 10**：实现注释支持。SQL 注释有两种：

```
-- 单行注释
/* 多行注释 */
```

在 `lexer_next` 跳空白的地方加注释跳过逻辑。

### 11.3 思考题

**题 11**：如果把 `keyword_t` 改成有哨兵版本（`KW_INVALID = -1`），需要修改哪些代码？列出所有改动点。

**题 12**：当前 AST 用扁平数组，上限 16 列、8 条件。如果要做"无上限"版本，你会怎么改？画出新的结构体定义。

**题 13**：为什么 `parse_select` 里检查 `*` 用 `p->cur.text[0] == '*'`，而不是 `strcmp(p->cur.text, "*") == 0`？两者有什么区别？

**题 14**：`lexer_create` 不复制源字符串，只保存指针。如果调用方在解析前 `free` 了源串，会发生什么？如何修复？

**题 15**：如果要支持 SQL 注释 `-- ...`，应该改 lexer 还是 parser？为什么？

### 11.4 扩展题

**题 16**：实现 `ast_print` 的"美化打印"模式，输出带缩进的多行 SQL：

```
SELECT
    id,
    name,
    age
FROM
    users
WHERE
    id >= 10
    AND age < 30
```

**题 17**：实现 `ast_equal(a, b)` 函数，比较两个 AST 是否语义相同（忽略列顺序等）。用于测试。

**题 18**：研究 SQLite 的 `src/tokenize.c` 和 `src/parse.y`，写一段对比报告：SQLite 的词法/语法分析和本项目有哪些相同点和不同点？

### 11.5 参考答案（部分）

**题 3 答案**：`lexer_peek` 调 `lexer_next` 时，`has_peek` 是 false（否则不会进 if 分支）。`lexer_next` 开头检查 `has_peek`，为 false 就走真正的扫描路径，不会再调 `lexer_peek`。所以不会递归。

**题 5 答案**：19 个关键字，线性查找平均 10 次比较。哈希表需要算哈希、处理冲突，开销大于 10 次字符串比较。小规模下简单就是快。

**题 13 答案**：`text[0] == '*'` 只比一个字符，`strcmp` 比整个字符串。运算符 Token 的 text 只有一个字符（`*` 后跟 `\0`），两种写法等价，但前者更快、更直接。

**题 15 答案**：改 lexer。注释是词法层面的——它不参与语法结构，应该像空白一样被丢弃。如果在 parser 里处理，每条规则都得考虑注释 Token，复杂度爆炸。

---

## 附录 A：文件清单

| 文件 | 行数 | 职责 |
|---|---|---|
| `phase1/src/sql/lexer.h` | 43 | 词法分析器接口 |
| `phase1/src/sql/lexer.c` | 158 | 词法分析器实现 |
| `phase1/src/sql/parser.h` | 12 | 语法分析器接口 |
| `phase1/src/sql/parser.c` | 183 | 语法分析器实现 |
| `phase1/src/sql/ast.h` | 80 | AST 节点定义 |
| `phase1/src/sql/ast.c` | 58 | AST 打印函数 |
| `phase1/src/sql/test_parser.c` | — | 11 个测试 |

## 附录 B：测试覆盖

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

## 附录 C：使用方式

```c
#include "parser.h"
#include "ast.h"

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

    // 调试打印
    ast_print(stmt);   // SELECT * FROM users WHERE id = 42

    free(stmt);
} else {
    printf("解析失败\n");
}
parser_destroy(p);
```

## 附录 D：关键字速查表

| 关键字 | 枚举 | 用于 |
|---|---|---|
| SELECT | KW_SELECT | SELECT 语句 |
| FROM | KW_FROM | SELECT/DELETE |
| WHERE | KW_WHERE | SELECT/DELETE |
| INSERT | KW_INSERT | INSERT 语句 |
| INTO | KW_INTO | INSERT 语句 |
| VALUES | KW_VALUES | INSERT 语句 |
| UPDATE | KW_UPDATE | （预留）UPDATE 语句 |
| SET | KW_SET | （预留）UPDATE 语句 |
| DELETE | KW_DELETE | DELETE 语句 |
| CREATE | KW_CREATE | CREATE 语句 |
| TABLE | KW_TABLE | CREATE 语句 |
| AND | KW_AND | WHERE 条件连接 |
| OR | KW_OR | （预留）WHERE 条件连接 |
| NULL | KW_NULL | 空值字面量 |
| INT | KW_INT | 列类型 |
| FLOAT | KW_FLOAT | 列类型 |
| PRIMARY | KW_PRIMARY | 主键约束 |
| KEY | KW_KEY | 主键约束 |
| NOT | KW_NOT | （预留）NOT NULL |

## 附录 E：Token 类型速查表

| 类型 | 文本例 | 何时产生 |
|---|---|---|
| TOK_KEYWORD | `SELECT`, `FROM` | `lex_ident` 命中关键字表 |
| TOK_IDENT | `users`, `id` | `lex_ident` 未命中 |
| TOK_NUMBER | `42`, `3.14` | `lex_number` |
| TOK_STRING | `'Alice'` | `lex_string` |
| TOK_OP | `=`, `>=`, `*` | `lex_op` 或 `*` 分支 |
| TOK_LPAREN | `(` | 单字符分支 |
| TOK_RPAREN | `)` | 单字符分支 |
| TOK_COMMA | `,` | 单字符分支 |
| TOK_SEMICOLON | `;` | 单字符分支 |
| TOK_EOF | (无) | 输入结束 |
| TOK_ERROR | `@`, `#` | 不认识字符 |

## 附录 F：常见错误与排查

| 现象 | 可能原因 | 排查方法 |
|---|---|---|
| `parser_parse` 返回 NULL | SQL 语法错 | 检查第一个 Token 是否是支持的关键字 |
| 表名乱码 | `strncpy` 没加 `\0` | 检查名字长度是否超过 `AST_MAX_NAME-1` |
| 列数被截断 | 超过 `AST_MAX_COLS` | 改 `ast.h` 的宏定义 |
| 条件被截断 | 超过 `AST_MAX_WHERE` | 同上 |
| 浮点值变 0 | `atof` 失败 | 检查 Token 文本是否合法数字 |
| 关键字被当标识符 | `kw_table` 没登记 | 在 `kw_table` 加一行 |

---

## 下一步

AST 生成后，下一章实现**查询优化器**：将 AST 转化为逻辑计划，应用启发式规则（谓词下推、投影裁剪）和代价模型选择最优 Join 顺序。

读完本章你应该能回答：

1. 词法分析器和语法分析器各做什么？它们的接口是什么？
2. 递归下降的核心思想是什么？为什么每条规则对应一个函数？
3. AST 为什么用扁平结构？它的上限是什么？
4. `keyword_t` 的无符号陷阱是什么？如何规避？
5. 手写解析器相比 flex/bison 有什么优劣？

如果以上都能答上来，恭喜你已经掌握了一个最小可用的 SQL 解析器。下一步，我们让这个解析器输出的 AST 真正"跑起来"。
