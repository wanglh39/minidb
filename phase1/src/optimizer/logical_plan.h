#ifndef MINIDB_LOGICAL_PLAN_H
#define MINIDB_LOGICAL_PLAN_H

#include "ast.h"
#include <stdbool.h>

#define PLAN_MAX_COLS 16
#define PLAN_MAX_NAME 32

typedef enum {
    PLAN_SEQ_SCAN = 0,
    PLAN_INDEX_SCAN = 1,
    PLAN_FILTER = 2,
    PLAN_PROJECT = 3,
    PLAN_NESTED_LOOP_JOIN = 4,
    PLAN_HASH_JOIN = 5,
} plan_type_t;

typedef struct plan_node plan_node_t;

struct plan_node {
    plan_type_t type;

    plan_node_t *left;
    plan_node_t *right;

    char table_name[PLAN_MAX_NAME];
    char index_col[PLAN_MAX_NAME];

    ast_expr_t predicate;
    bool has_predicate;

    char columns[PLAN_MAX_COLS][PLAN_MAX_NAME];
    int num_cols;
    bool select_all;

    char join_left_col[PLAN_MAX_NAME];
    char join_right_col[PLAN_MAX_NAME];

    double estimated_rows;
    double estimated_cost;
};

plan_node_t *plan_create(plan_type_t type);
void         plan_destroy(plan_node_t *p);
void         plan_print(const plan_node_t *p, int indent);

#endif