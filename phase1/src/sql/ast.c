#include "ast.h"
#include <stdio.h>

static void print_value(const ast_value_t *v) {
    switch (v->type) {
        case VAL_INT:    printf("%d", v->int_val); break;
        case VAL_FLOAT:  printf("%f", v->float_val); break;
        case VAL_STRING: printf("'%s'", v->str_val); break;
        case VAL_NULL:   printf("NULL"); break;
    }
}

void ast_print(const ast_stmt_t *s) {
    switch (s->type) {
        case AST_SELECT:
            printf("SELECT ");
            if (s->select_all) printf("*");
            else for (int i = 0; i < s->num_cols; i++)
                printf("%s%s", i ? ", " : "", s->columns[i]);
            printf(" FROM %s", s->table);
            break;
        case AST_INSERT:
            printf("INSERT INTO %s (", s->table);
            for (int i = 0; i < s->num_cols; i++)
                printf("%s%s", i ? ", " : "", s->columns[i]);
            printf(") VALUES (");
            for (int i = 0; i < s->num_values; i++) {
                if (i) printf(", ");
                print_value(&s->values[i]);
            }
            printf(")");
            break;
        case AST_DELETE:
            printf("DELETE FROM %s", s->table);
            break;
        case AST_CREATE:
            printf("CREATE TABLE %s (", s->table);
            for (int i = 0; i < s->num_col_defs; i++) {
                if (i) printf(", ");
                printf("%s %s", s->col_defs[i].name,
                       s->col_defs[i].type == AST_COL_INT32 ? "INT" : "FLOAT");
                if (s->col_defs[i].primary_key) printf(" PRIMARY KEY");
            }
            printf(")");
            break;
        default:
            printf("(unknown stmt)");
    }
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