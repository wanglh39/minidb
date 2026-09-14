#include "executor.h"
#include "optimizer.h"
#include "catalog.h"
#include "logical_plan.h"
#include "ast.h"
#include "parser.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define BENCH_ROWS 10000
#define BENCH_QUERIES 10000

static exec_row_t bench_rows[BENCH_ROWS];
static exec_col_meta_t bench_cols[2];
static exec_table_t bench_table;
static catalog_t *bench_cat;

static void init_bench_data(void) {
    strcpy(bench_table.name, "bench");
    bench_table.num_cols = 2;
    strcpy(bench_cols[0].name, "id");
    strcpy(bench_cols[1].name, "val");
    bench_table.cols[0] = bench_cols[0];
    bench_table.cols[1] = bench_cols[1];

    for (int i = 0; i < BENCH_ROWS; i++) {
        bench_rows[i].num_cols = 2;
        bench_rows[i].values[0] = i;
        bench_rows[i].values[1] = i * 2;
    }
    bench_table.rows = bench_rows;
    bench_table.num_rows = BENCH_ROWS;

    bench_cat = catalog_create();
    catalog_add_table(bench_cat, "bench", BENCH_ROWS,
                      BENCH_ROWS / 10 + 1, true, "id");
}

static double now_ms(void) {
    return (double)clock() / CLOCKS_PER_SEC * 1000.0;
}

static void bench_parse(void) {
    const char *sql = "SELECT id, val FROM bench WHERE id = 42 AND val > 100";
    double t0 = now_ms();
    for (int i = 0; i < BENCH_QUERIES; i++) {
        parser_t *p = parser_create(sql);
        ast_stmt_t *s = parser_parse(p);
        free(s);
        parser_destroy(p);
    }
    double dt = now_ms() - t0;
    printf("Parse:     %d queries in %.1f ms  (%.0f q/s)\n",
           BENCH_QUERIES, dt, BENCH_QUERIES / dt * 1000);
}

static void bench_optimize(void) {
    const char *sql = "SELECT id, val FROM bench WHERE id = 42 AND val > 100";
    double t0 = now_ms();
    for (int i = 0; i < BENCH_QUERIES; i++) {
        parser_t *p = parser_create(sql);
        ast_stmt_t *s = parser_parse(p);
        plan_node_t *plan = optimizer_optimize(s, bench_cat);
        plan_destroy(plan);
        free(s);
        parser_destroy(p);
    }
    double dt = now_ms() - t0;
    printf("Optimize:  %d plans in %.1f ms  (%.0f p/s)\n",
           BENCH_QUERIES, dt, BENCH_QUERIES / dt * 1000);
}

static void bench_execute_seqscan(void) {
    exec_table_t tables[] = {bench_table};
    const char *sql = "SELECT * FROM bench";
    double t0 = now_ms();
    int total_rows = 0;
    for (int i = 0; i < 100; i++) {
        parser_t *p = parser_create(sql);
        ast_stmt_t *s = parser_parse(p);
        plan_node_t *plan = optimizer_optimize(s, bench_cat);
        result_set_t *rs = executor_run(plan, tables, 1);
        total_rows += rs->num_rows;
        result_set_destroy(rs);
        plan_destroy(plan);
        free(s);
        parser_destroy(p);
    }
    double dt = now_ms() - t0;
    printf("SeqScan:   %d rows in %.1f ms  (%.0f rows/s)\n",
           total_rows, dt, total_rows / dt * 1000);
}

static void bench_execute_filter(void) {
    exec_table_t tables[] = {bench_table};
    const char *sql = "SELECT * FROM bench WHERE val > 5000";
    double t0 = now_ms();
    int total_rows = 0;
    for (int i = 0; i < 100; i++) {
        parser_t *p = parser_create(sql);
        ast_stmt_t *s = parser_parse(p);
        plan_node_t *plan = optimizer_optimize(s, bench_cat);
        result_set_t *rs = executor_run(plan, tables, 1);
        total_rows += rs->num_rows;
        result_set_destroy(rs);
        plan_destroy(plan);
        free(s);
        parser_destroy(p);
    }
    double dt = now_ms() - t0;
    printf("Filter:    %d rows in %.1f ms  (%.0f rows/s)\n",
           total_rows, dt, total_rows / dt * 1000);
}

static void bench_e2e(void) {
    exec_table_t tables[] = {bench_table};
    const char *queries[] = {
        "SELECT * FROM bench",
        "SELECT id FROM bench WHERE id = 5000",
        "SELECT * FROM bench WHERE id >= 1000 AND id < 2000",
        "SELECT val FROM bench WHERE id > 9000",
    };
    int nq = sizeof(queries) / sizeof(queries[0]);
    double t0 = now_ms();
    int total_rows = 0;
    int iterations = 100;
    for (int iter = 0; iter < iterations; iter++) {
        for (int q = 0; q < nq; q++) {
            parser_t *p = parser_create(queries[q]);
            ast_stmt_t *s = parser_parse(p);
            plan_node_t *plan = optimizer_optimize(s, bench_cat);
            result_set_t *rs = executor_run(plan, tables, 1);
            total_rows += rs->num_rows;
            result_set_destroy(rs);
            plan_destroy(plan);
            free(s);
            parser_destroy(p);
        }
    }
    double dt = now_ms() - t0;
    int total_queries = nq * iterations;
    printf("E2E:       %d queries, %d rows in %.1f ms  (%.0f q/s, %.0f rows/s)\n",
           total_queries, total_rows, dt,
           total_queries / dt * 1000, total_rows / dt * 1000);
}

int main(void) {
    printf("=== miniDB Benchmark ===\n");
    printf("Table: %d rows\n\n", BENCH_ROWS);

    init_bench_data();

    bench_parse();
    bench_optimize();
    bench_execute_seqscan();
    bench_execute_filter();
    bench_e2e();

    printf("\n=== Done ===\n");
    catalog_destroy(bench_cat);
    return 0;
}