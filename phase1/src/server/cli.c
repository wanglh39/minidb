#include "cli.h"
#include "parser.h"
#include "ast.h"
#include "optimizer.h"
#include "logical_plan.h"
#include "executor.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

db_context_t *db_context_create(void) {
    db_context_t *ctx = calloc(1, sizeof(db_context_t));
    ctx->catalog = catalog_create();
    return ctx;
}

void db_context_destroy(db_context_t *ctx) {
    if (!ctx) return;
    catalog_destroy(ctx->catalog);
    free(ctx);
}

void db_context_add_table(db_context_t *ctx, const char *name,
                          exec_row_t *rows, int num_rows,
                          exec_col_meta_t *cols, int num_cols,
                          bool has_index, const char *index_col) {
    if (ctx->num_tables >= 16) return;
    exec_table_t *t = &ctx->tables[ctx->num_tables++];
    strncpy(t->name, name, EXEC_MAX_NAME - 1);
    t->rows = rows;
    t->num_rows = num_rows;
    t->num_cols = num_cols;
    for (int i = 0; i < num_cols && i < EXEC_MAX_COLS; i++)
        t->cols[i] = cols[i];
    catalog_add_table(ctx->catalog, name, num_rows,
                      num_rows / 10 + 1, has_index, index_col);
}

static void process_sql(db_context_t *ctx, const char *sql) {
    parser_t *p = parser_create(sql);
    ast_stmt_t *stmt = parser_parse(p);

    if (!stmt) {
        printf("Error: invalid SQL\n");
        parser_destroy(p);
        return;
    }

    printf("AST: ");
    ast_print(stmt);

    plan_node_t *plan = optimizer_optimize(stmt, ctx->catalog);
    if (!plan) {
        printf("Error: optimization failed\n");
        free(stmt);
        parser_destroy(p);
        return;
    }

    printf("Plan:\n");
    plan_print(plan, 1);

    if (stmt->type == AST_SELECT) {
        result_set_t *rs = executor_run(plan, ctx->tables, ctx->num_tables);
        printf("Result:\n");
        result_set_print(rs);
        result_set_destroy(rs);
    } else if (stmt->type == AST_INSERT) {
        printf("INSERT OK (0 rows affected)\n");
    } else if (stmt->type == AST_DELETE) {
        printf("DELETE OK (0 rows affected)\n");
    } else if (stmt->type == AST_CREATE) {
        printf("CREATE OK\n");
    }

    plan_destroy(plan);
    free(stmt);
    parser_destroy(p);
}

static void print_help(void) {
    printf("miniDB Commands:\n");
    printf("  SQL statements end with ';'\n");
    printf("  .help     Show this help\n");
    printf("  .tables   List tables\n");
    printf("  .exit     Quit\n");
    printf("\nSupported SQL:\n");
    printf("  SELECT * | col1, col2 FROM table [WHERE col OP value [AND ...]]\n");
    printf("  INSERT INTO table (cols) VALUES (vals)\n");
    printf("  DELETE FROM table [WHERE ...]\n");
    printf("  CREATE TABLE table (col TYPE [PRIMARY KEY], ...)\n");
}

static void list_tables(db_context_t *ctx) {
    for (int i = 0; i < ctx->num_tables; i++) {
        printf("%s (%d rows, %d cols", ctx->tables[i].name,
               ctx->tables[i].num_rows, ctx->tables[i].num_cols);
        const catalog_entry_t *e = catalog_lookup(ctx->catalog, ctx->tables[i].name);
        if (e && e->has_index)
            printf(", index on %s", e->index_col);
        printf(")\n");
    }
}

void cli_run(db_context_t *ctx) {
    char buf[4096];
    int len = 0;

    printf("miniDB v0.1  (type .help for help)\n");

    while (1) {
        printf("miniDB> ");
        fflush(stdout);

        if (!fgets(buf + len, sizeof(buf) - len, stdin))
            break;

        len += strlen(buf + len);

        if (len > 0 && buf[len - 1] == '\n')
            buf[--len] = '\0';

        if (len == 0)
            continue;

        if (buf[0] == '.') {
            if (strcmp(buf, ".exit") == 0 || strcmp(buf, ".quit") == 0)
                break;
            else if (strcmp(buf, ".help") == 0)
                print_help();
            else if (strcmp(buf, ".tables") == 0)
                list_tables(ctx);
            else
                printf("Unknown command: %s (try .help)\n", buf);
            len = 0;
            continue;
        }

        if (buf[len - 1] == ';') {
            buf[--len] = '\0';
            while (len > 0 && (buf[len-1] == ' ' || buf[len-1] == '\t'))
                buf[--len] = '\0';
            if (len > 0)
                process_sql(ctx, buf);
            len = 0;
        }
    }

    printf("Bye!\n");
}