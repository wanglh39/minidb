#include "logical_plan.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

plan_node_t *plan_create(plan_type_t type) {
    plan_node_t *p = calloc(1, sizeof(plan_node_t));
    p->type = type;
    return p;
}

void plan_destroy(plan_node_t *p) {
    if (!p) return;
    plan_destroy(p->left);
    plan_destroy(p->right);
    free(p);
}

static const char *type_str(plan_type_t t) {
    switch (t) {
        case PLAN_SEQ_SCAN:        return "SeqScan";
        case PLAN_INDEX_SCAN:      return "IndexScan";
        case PLAN_FILTER:          return "Filter";
        case PLAN_PROJECT:         return "Project";
        case PLAN_NESTED_LOOP_JOIN: return "NestedLoopJoin";
        case PLAN_HASH_JOIN:       return "HashJoin";
        default: return "Unknown";
    }
}

static void print_indent(int n) {
    for (int i = 0; i < n; i++) printf("  ");
}

void plan_print(const plan_node_t *p, int indent) {
    if (!p) return;
    print_indent(indent);
    printf("%s", type_str(p->type));

    switch (p->type) {
        case PLAN_SEQ_SCAN:
        case PLAN_INDEX_SCAN:
            printf("(%s)", p->table_name);
            if (p->type == PLAN_INDEX_SCAN)
                printf(" [idx:%s]", p->index_col);
            break;
        case PLAN_FILTER:
            if (p->has_predicate)
                printf("(%s %s)", p->predicate.column, p->predicate.op);
            break;
        case PLAN_PROJECT:
            if (p->select_all) printf("(*)");
            else {
                printf("(");
                for (int i = 0; i < p->num_cols; i++)
                    printf("%s%s", i ? "," : "", p->columns[i]);
                printf(")");
            }
            break;
        case PLAN_NESTED_LOOP_JOIN:
        case PLAN_HASH_JOIN:
            printf("(%s=%s)", p->join_left_col, p->join_right_col);
            break;
    }

    printf("  [rows=%.0f cost=%.1f]\n", p->estimated_rows, p->estimated_cost);

    if (p->left)  plan_print(p->left, indent + 1);
    if (p->right) plan_print(p->right, indent + 1);
}