#include "cli.h"
#include "executor.h"
#include <stdio.h>
#include <string.h>

static const char *VERSION = "0.1.0";

static exec_row_t users_rows[5];
static exec_col_meta_t users_cols[3];
static exec_row_t orders_rows[4];
static exec_col_meta_t orders_cols[3];

static void init_sample_data(db_context_t *ctx) {
    strcpy(users_cols[0].name, "id");
    strcpy(users_cols[1].name, "age");
    strcpy(users_cols[2].name, "score");

    int users_data[5][3] = {
        {1, 25, 85}, {2, 30, 90}, {3, 35, 75}, {4, 28, 95}, {5, 40, 60}
    };
    for (int i = 0; i < 5; i++) {
        users_rows[i].num_cols = 3;
        users_rows[i].values[0] = users_data[i][0];
        users_rows[i].values[1] = users_data[i][1];
        users_rows[i].values[2] = users_data[i][2];
    }
    db_context_add_table(ctx, "users", users_rows, 5,
                         users_cols, 3, true, "id");

    strcpy(orders_cols[0].name, "order_id");
    strcpy(orders_cols[1].name, "user_id");
    strcpy(orders_cols[2].name, "amount");

    int orders_data[4][3] = {
        {101, 1, 500}, {102, 2, 300}, {103, 1, 700}, {104, 3, 200}
    };
    for (int i = 0; i < 4; i++) {
        orders_rows[i].num_cols = 3;
        orders_rows[i].values[0] = orders_data[i][0];
        orders_rows[i].values[1] = orders_data[i][1];
        orders_rows[i].values[2] = orders_data[i][2];
    }
    db_context_add_table(ctx, "orders", orders_rows, 4,
                         orders_cols, 3, true, "order_id");
}

static void print_help(void) {
    printf("miniDB v%s - Educational RDBMS\n", VERSION);
    printf("\n");
    printf("Phase 1: Building a database from scratch\n");
    printf("Chapters 1-10 complete\n");
    printf("\n");
    printf("Usage:\n");
    printf("  minidb              Start interactive CLI\n");
    printf("  minidb --version    Show version\n");
    printf("  minidb --help       Show this help\n");
}

int main(int argc, char *argv[]) {
    if (argc > 1) {
        if (strcmp(argv[1], "--version") == 0) {
            printf("miniDB v%s\n", VERSION);
            return 0;
        }
        if (strcmp(argv[1], "--help") == 0) {
            print_help();
            return 0;
        }
    }

    db_context_t *ctx = db_context_create();
    init_sample_data(ctx);

    cli_run(ctx);

    db_context_destroy(ctx);
    return 0;
}
