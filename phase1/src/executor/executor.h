#ifndef MINIDB_EXECUTOR_H
#define MINIDB_EXECUTOR_H

#include "logical_plan.h"
#include "ast.h"
#include <stdint.h>
#include <stdbool.h>

#define EXEC_MAX_COLS 16
#define EXEC_MAX_NAME 32
#define EXEC_MAX_ROWS 1024

typedef struct {
    int32_t values[EXEC_MAX_COLS];
    int num_cols;
} exec_row_t;

typedef struct {
    char name[EXEC_MAX_NAME];
} exec_col_meta_t;

typedef struct {
    char name[EXEC_MAX_NAME];
    exec_col_meta_t cols[EXEC_MAX_COLS];
    int num_cols;
    exec_row_t *rows;
    int num_rows;
} exec_table_t;

typedef struct operator operator_t;

struct operator {
    plan_type_t type;
    operator_t *left;
    operator_t *right;

    bool opened;
    int pos;

    const exec_table_t *table;
    ast_expr_t predicate;
    bool has_predicate;

    int proj_indices[EXEC_MAX_COLS];
    int proj_num_cols;

    exec_row_t current;
};

operator_t *executor_build(const plan_node_t *plan,
                           exec_table_t *tables, int num_tables);
void        executor_open(operator_t *op);
bool        executor_next(operator_t *op, exec_row_t *out);
void        executor_close(operator_t *op);
void        executor_destroy(operator_t *op);

typedef struct {
    exec_row_t rows[EXEC_MAX_ROWS];
    int num_rows;
    exec_col_meta_t cols[EXEC_MAX_COLS];
    int num_cols;
} result_set_t;

result_set_t *executor_run(const plan_node_t *plan,
                           exec_table_t *tables, int num_tables);
void          result_set_print(const result_set_t *rs);
void          result_set_destroy(result_set_t *rs);

#endif