#ifndef MINIDB_AST_H
#define MINIDB_AST_H

#include <stdint.h>
#include <stdbool.h>

#define AST_MAX_COLS 16
#define AST_MAX_NAME 32
#define AST_MAX_WHERE 8

typedef enum {
    AST_SELECT = 0,
    AST_INSERT = 1,
    AST_UPDATE = 2,
    AST_DELETE = 3,
    AST_CREATE = 4,
} stmt_type_t;

typedef enum {
    EXPR_COMPARE = 0,
} expr_type_t;

typedef enum {
    VAL_INT = 0,
    VAL_FLOAT = 1,
    VAL_STRING = 2,
    VAL_NULL = 3,
} val_type_t;

typedef struct {
    val_type_t type;
    int32_t int_val;
    float float_val;
    char str_val[64];
} ast_value_t;

typedef struct {
    expr_type_t type;
    char column[AST_MAX_NAME];
    char op[4];
    ast_value_t value;
} ast_expr_t;

typedef enum {
    AST_COL_INT32 = 0,
    AST_COL_FLOAT = 1,
} ast_col_type_t;

typedef struct {
    char name[AST_MAX_NAME];
    ast_col_type_t type;
    bool nullable;
    bool primary_key;
} ast_col_def_t;

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

void ast_print(const ast_stmt_t *stmt);

#endif