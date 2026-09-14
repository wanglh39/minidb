#include "executor.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

static const exec_table_t *find_table(exec_table_t *tables, int n, const char *name) {
    for (int i = 0; i < n; i++)
        if (strcmp(tables[i].name, name) == 0)
            return &tables[i];
    return NULL;
}

static int find_col_idx(const exec_table_t *t, const char *name) {
    for (int i = 0; i < t->num_cols; i++)
        if (strcmp(t->cols[i].name, name) == 0)
            return i;
    return -1;
}

static bool eval_predicate(const ast_expr_t *pred, const exec_row_t *row,
                           const exec_table_t *table) {
    int idx = find_col_idx(table, pred->column);
    if (idx < 0 || idx >= row->num_cols) return false;

    int32_t v = row->values[idx];
    int32_t target;

    if (pred->value.type == VAL_INT)
        target = pred->value.int_val;
    else if (pred->value.type == VAL_FLOAT)
        target = (int32_t)pred->value.float_val;
    else
        return false;

    if (strcmp(pred->op, "=") == 0)  return v == target;
    if (strcmp(pred->op, "!=") == 0) return v != target;
    if (strcmp(pred->op, "<") == 0)  return v <  target;
    if (strcmp(pred->op, ">") == 0)  return v >  target;
    if (strcmp(pred->op, "<=") == 0) return v <= target;
    if (strcmp(pred->op, ">=") == 0) return v >= target;
    return false;
}

operator_t *executor_build(const plan_node_t *plan,
                           exec_table_t *tables, int num_tables) {
    if (!plan) return NULL;

    operator_t *op = calloc(1, sizeof(operator_t));
    op->type = plan->type;

    switch (plan->type) {
        case PLAN_SEQ_SCAN:
        case PLAN_INDEX_SCAN:
            op->table = find_table(tables, num_tables, plan->table_name);
            break;
        case PLAN_FILTER:
            op->predicate = plan->predicate;
            op->has_predicate = plan->has_predicate;
            break;
        case PLAN_PROJECT:
            if (plan->select_all) {
                op->proj_num_cols = -1;
            } else {
                op->proj_num_cols = plan->num_cols;
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
        default:
            break;
    }

    op->left = executor_build(plan->left, tables, num_tables);
    op->right = executor_build(plan->right, tables, num_tables);

    return op;
}

void executor_open(operator_t *op) {
    if (!op || op->opened) return;
    op->opened = true;
    op->pos = 0;
    if (op->left)  executor_open(op->left);
    if (op->right) executor_open(op->right);
}

bool executor_next(operator_t *op, exec_row_t *out) {
    if (!op || !op->opened) return false;

    switch (op->type) {
        case PLAN_SEQ_SCAN:
        case PLAN_INDEX_SCAN: {
            if (!op->table) return false;
            if (op->pos >= op->table->num_rows) return false;
            *out = op->table->rows[op->pos++];
            return true;
        }
        case PLAN_FILTER: {
            exec_row_t row;
            while (executor_next(op->left, &row)) {
                const exec_table_t *t = NULL;
                operator_t *scan = op->left;
                while (scan && scan->type == PLAN_FILTER)
                    scan = scan->left;
                if (scan) t = scan->table;
                if (!op->has_predicate ||
                    eval_predicate(&op->predicate, &row, t)) {
                    *out = row;
                    return true;
                }
            }
            return false;
        }
        case PLAN_PROJECT: {
            exec_row_t row;
            if (!executor_next(op->left, &row)) return false;
            if (op->proj_num_cols == -1) {
                *out = row;
            } else {
                out->num_cols = op->proj_num_cols;
                for (int i = 0; i < op->proj_num_cols; i++) {
                    int idx = op->proj_indices[i];
                    out->values[i] = (idx >= 0 && idx < row.num_cols)
                                   ? row.values[idx] : 0;
                }
            }
            return true;
        }
        default:
            return false;
    }
}

void executor_close(operator_t *op) {
    if (!op) return;
    if (op->left)  executor_close(op->left);
    if (op->right) executor_close(op->right);
    op->opened = false;
}

void executor_destroy(operator_t *op) {
    if (!op) return;
    executor_destroy(op->left);
    executor_destroy(op->right);
    free(op);
}

result_set_t *executor_run(const plan_node_t *plan,
                           exec_table_t *tables, int num_tables) {
    operator_t *op = executor_build(plan, tables, num_tables);
    if (!op) return NULL;

    result_set_t *rs = calloc(1, sizeof(result_set_t));

    executor_open(op);

    exec_row_t row;
    while (executor_next(op, &row) && rs->num_rows < EXEC_MAX_ROWS) {
        rs->rows[rs->num_rows++] = row;
    }

    if (op->type == PLAN_PROJECT && op->proj_num_cols > 0) {
        rs->num_cols = op->proj_num_cols;
        operator_t *scan = op->left;
        while (scan && scan->type == PLAN_FILTER) scan = scan->left;
        if (scan && scan->table) {
            for (int i = 0; i < op->proj_num_cols; i++) {
                int idx = op->proj_indices[i];
                if (idx >= 0 && idx < scan->table->num_cols)
                    strcpy(rs->cols[i].name, scan->table->cols[idx].name);
            }
        }
    } else {
        operator_t *scan = op;
        while (scan && scan->type != PLAN_SEQ_SCAN &&
               scan->type != PLAN_INDEX_SCAN)
            scan = scan->left;
        if (scan && scan->table) {
            rs->num_cols = scan->table->num_cols;
            for (int i = 0; i < scan->table->num_cols; i++)
                rs->cols[i] = scan->table->cols[i];
        }
    }

    executor_close(op);
    executor_destroy(op);

    return rs;
}

void result_set_print(const result_set_t *rs) {
    if (!rs) { printf("(null result)\n"); return; }

    if (rs->num_cols > 0) {
        for (int c = 0; c < rs->num_cols; c++)
            printf("%s%s", c ? " | " : "", rs->cols[c].name);
        printf("\n");
        for (int c = 0; c < rs->num_cols; c++)
            printf("%s---", c ? " | " : "");
        printf("\n");
    }

    for (int r = 0; r < rs->num_rows; r++) {
        for (int c = 0; c < rs->rows[r].num_cols; c++)
            printf("%s%d", c ? " | " : "", rs->rows[r].values[c]);
        printf("\n");
    }

    printf("(%d rows)\n", rs->num_rows);
}

void result_set_destroy(result_set_t *rs) {
    free(rs);
}