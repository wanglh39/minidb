#ifndef MINIDB_OPTIMIZER_H
#define MINIDB_OPTIMIZER_H

#include "ast.h"
#include "catalog.h"
#include "logical_plan.h"

plan_node_t *optimizer_optimize(ast_stmt_t *stmt, catalog_t *cat);

plan_node_t *plan_from_ast(ast_stmt_t *stmt);
void         opt_pushdown_predicates(plan_node_t *plan);
void         opt_choose_index(plan_node_t *plan, catalog_t *cat);
void         estimate_costs(plan_node_t *plan, catalog_t *cat);

#endif