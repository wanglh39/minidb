#include "optimizer.h"
#include <stdlib.h>
#include <string.h>
#include <math.h>

plan_node_t *plan_from_ast(ast_stmt_t *stmt) {
    if (!stmt) return NULL;

    plan_node_t *scan = plan_create(PLAN_SEQ_SCAN);
    strncpy(scan->table_name, stmt->table, PLAN_MAX_NAME - 1);

    plan_node_t *node = scan;

    if (stmt->num_where > 0) {
        for (int i = stmt->num_where - 1; i >= 0; i--) {
            plan_node_t *f = plan_create(PLAN_FILTER);
            f->predicate = stmt->where[i];
            f->has_predicate = true;
            f->left = node;
            node = f;
        }
    }

    if (stmt->type == AST_SELECT && !stmt->select_all && stmt->num_cols > 0) {
        plan_node_t *proj = plan_create(PLAN_PROJECT);
        proj->select_all = false;
        proj->num_cols = stmt->num_cols;
        for (int i = 0; i < stmt->num_cols; i++)
            strncpy(proj->columns[i], stmt->columns[i], PLAN_MAX_NAME - 1);
        proj->left = node;
        node = proj;
    }

    return node;
}


void opt_pushdown_predicates(plan_node_t *plan) {
    (void)plan;
}

static void try_index_scan(plan_node_t *plan, catalog_t *cat) {
    if (plan->type != PLAN_FILTER || !plan->has_predicate) return;

    plan_node_t *node = plan->left;
    while (node && node->type == PLAN_FILTER)
        node = node->left;

    if (!node || node->type != PLAN_SEQ_SCAN) return;

    const catalog_entry_t *e = catalog_lookup(cat, node->table_name);
    if (!e || !e->has_index) return;

    if (strcmp(plan->predicate.column, e->index_col) == 0) {
        node->type = PLAN_INDEX_SCAN;
        strncpy(node->index_col, e->index_col, PLAN_MAX_NAME - 1);
    }
}

void opt_choose_index(plan_node_t *plan, catalog_t *cat) {
    if (!plan) return;
    try_index_scan(plan, cat);
    opt_choose_index(plan->left, cat);
    opt_choose_index(plan->right, cat);
}

static double selectivity(const ast_expr_t *expr) {
    if (strcmp(expr->op, "=") == 0)  return 0.01;
    if (strcmp(expr->op, "!=") == 0) return 0.99;
    if (strcmp(expr->op, "<") == 0 || strcmp(expr->op, ">") == 0) return 0.33;
    if (strcmp(expr->op, "<=") == 0 || strcmp(expr->op, ">=") == 0) return 0.33;
    return 0.5;
}

void estimate_costs(plan_node_t *plan, catalog_t *cat) {
    if (!plan) return;

    estimate_costs(plan->left, cat);
    estimate_costs(plan->right, cat);

    switch (plan->type) {
        case PLAN_SEQ_SCAN: {
            const catalog_entry_t *e = catalog_lookup(cat, plan->table_name);
            if (e) {
                plan->estimated_rows = e->num_rows;
                plan->estimated_cost = e->num_pages;
            } else {
                plan->estimated_rows = 100;
                plan->estimated_cost = 10;
            }
            break;
        }
        case PLAN_INDEX_SCAN: {
            const catalog_entry_t *e = catalog_lookup(cat, plan->table_name);
            if (e) {
                plan->estimated_rows = e->num_rows * 0.01;
                plan->estimated_cost = log2(e->num_pages > 0 ? e->num_pages : 1) + 1;
            } else {
                plan->estimated_rows = 1;
                plan->estimated_cost = 3;
            }
            break;
        }
        case PLAN_FILTER: {
            double sel = plan->has_predicate ? selectivity(&plan->predicate) : 1.0;
            plan->estimated_rows = plan->left->estimated_rows * sel;
            plan->estimated_cost = plan->left->estimated_cost + plan->left->estimated_rows * 0.1;
            break;
        }
        case PLAN_PROJECT: {
            plan->estimated_rows = plan->left->estimated_rows;
            plan->estimated_cost = plan->left->estimated_cost + plan->left->estimated_rows * 0.05;
            break;
        }
        case PLAN_NESTED_LOOP_JOIN: {
            plan->estimated_rows = plan->left->estimated_rows * plan->right->estimated_rows * 0.1;
            plan->estimated_cost = plan->left->estimated_cost
                                 + plan->left->estimated_rows * plan->right->estimated_cost;
            break;
        }
        case PLAN_HASH_JOIN: {
            plan->estimated_rows = plan->left->estimated_rows * plan->right->estimated_rows * 0.1;
            plan->estimated_cost = plan->left->estimated_cost + plan->right->estimated_cost
                                 + plan->left->estimated_rows + plan->right->estimated_rows;
            break;
        }
    }
}

plan_node_t *optimizer_optimize(ast_stmt_t *stmt, catalog_t *cat) {
    plan_node_t *plan = plan_from_ast(stmt);
    if (!plan) return NULL;

    opt_pushdown_predicates(plan);
    opt_choose_index(plan, cat);
    estimate_costs(plan, cat);

    return plan;
}