#include "unity.h"
#include "executor.h"
#include "optimizer.h"
#include "catalog.h"
#include "logical_plan.h"
#include "ast.h"
#include "parser.h"
#include <stdlib.h>
#include <string.h>

void setUp(void) {}
void tearDown(void) {}

#define LARGE_ROWS 100

static exec_row_t big_rows[LARGE_ROWS];
static exec_col_meta_t big_cols[2];
static exec_table_t big_table;
static catalog_t *cat;

static void init_large_table(void) {
    strcpy(big_table.name, "big");
    big_table.num_cols = 2;
    strcpy(big_cols[0].name, "id");
    strcpy(big_cols[1].name, "val");
    big_table.cols[0] = big_cols[0];
    big_table.cols[1] = big_cols[1];

    for (int i = 0; i < LARGE_ROWS; i++) {
        big_rows[i].num_cols = 2;
        big_rows[i].values[0] = i;
        big_rows[i].values[1] = i * 10;
    }
    big_table.rows = big_rows;
    big_table.num_rows = LARGE_ROWS;

    cat = catalog_create();
    catalog_add_table(cat, "big", LARGE_ROWS, LARGE_ROWS / 10 + 1, true, "id");
}

static result_set_t *run_query(const char *sql) {
    exec_table_t tables[] = {big_table};
    parser_t *p = parser_create(sql);
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, cat);
    result_set_t *rs = executor_run(plan, tables, 1);
    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    return rs;
}

void test_integ_full_scan(void) {
    init_large_table();
    result_set_t *rs = run_query("SELECT * FROM big");
    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(LARGE_ROWS, rs->num_rows);
    TEST_ASSERT_EQUAL_INT32(0, rs->rows[0].values[0]);
    TEST_ASSERT_EQUAL_INT32(99, rs->rows[99].values[0]);
    result_set_destroy(rs);
    catalog_destroy(cat);
}

void test_integ_index_lookup(void) {
    init_large_table();
    result_set_t *rs = run_query("SELECT * FROM big WHERE id = 50");
    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(1, rs->num_rows);
    TEST_ASSERT_EQUAL_INT32(50, rs->rows[0].values[0]);
    TEST_ASSERT_EQUAL_INT32(500, rs->rows[0].values[1]);
    result_set_destroy(rs);
    catalog_destroy(cat);
}

void test_integ_range_query(void) {
    init_large_table();
    result_set_t *rs = run_query("SELECT * FROM big WHERE id >= 10 AND id < 20");
    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(10, rs->num_rows);
    TEST_ASSERT_EQUAL_INT32(10, rs->rows[0].values[0]);
    TEST_ASSERT_EQUAL_INT32(19, rs->rows[9].values[0]);
    result_set_destroy(rs);
    catalog_destroy(cat);
}

void test_integ_project_filter(void) {
    init_large_table();
    result_set_t *rs = run_query("SELECT val FROM big WHERE id >= 90");
    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(10, rs->num_rows);
    TEST_ASSERT_EQUAL_INT32(900, rs->rows[0].values[0]);
    result_set_destroy(rs);
    catalog_destroy(cat);
}

void test_integ_no_results(void) {
    init_large_table();
    result_set_t *rs = run_query("SELECT * FROM big WHERE id = 99999");
    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(0, rs->num_rows);
    result_set_destroy(rs);
    catalog_destroy(cat);
}

void test_integ_not_equal(void) {
    init_large_table();
    result_set_t *rs = run_query("SELECT * FROM big WHERE id != 0");
    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(99, rs->num_rows);
    result_set_destroy(rs);
    catalog_destroy(cat);
}

void test_integ_first_half(void) {
    init_large_table();
    result_set_t *rs = run_query("SELECT * FROM big WHERE id < 50");
    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(50, rs->num_rows);
    result_set_destroy(rs);
    catalog_destroy(cat);
}

void test_integ_second_half(void) {
    init_large_table();
    result_set_t *rs = run_query("SELECT * FROM big WHERE id >= 50");
    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(50, rs->num_rows);
    result_set_destroy(rs);
    catalog_destroy(cat);
}

void test_integ_project_all(void) {
    init_large_table();
    result_set_t *rs = run_query("SELECT id FROM big");
    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(LARGE_ROWS, rs->num_rows);
    for (int i = 0; i < 10; i++)
        TEST_ASSERT_EQUAL_INT32(i, rs->rows[i].values[0]);
    result_set_destroy(rs);
    catalog_destroy(cat);
}

void test_integ_delete_parse(void) {
    init_large_table();
    parser_t *p = parser_create("DELETE FROM big WHERE id = 5");
    ast_stmt_t *s = parser_parse(p);
    TEST_ASSERT_NOT_NULL(s);
    TEST_ASSERT_EQUAL_INT(AST_DELETE, s->type);
    TEST_ASSERT_EQUAL_STRING("big", s->table);
    TEST_ASSERT_EQUAL_INT(1, s->num_where);
    free(s);
    parser_destroy(p);
    catalog_destroy(cat);
}

void test_integ_create_parse(void) {
    init_large_table();
    parser_t *p = parser_create("CREATE TABLE test (a INT PRIMARY KEY, b INT)");
    ast_stmt_t *s = parser_parse(p);
    TEST_ASSERT_NOT_NULL(s);
    TEST_ASSERT_EQUAL_INT(AST_CREATE, s->type);
    TEST_ASSERT_EQUAL_INT(2, s->num_col_defs);
    TEST_ASSERT_TRUE(s->col_defs[0].primary_key);
    TEST_ASSERT_FALSE(s->col_defs[1].primary_key);
    free(s);
    parser_destroy(p);
    catalog_destroy(cat);
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_integ_full_scan);
    RUN_TEST(test_integ_index_lookup);
    RUN_TEST(test_integ_range_query);
    RUN_TEST(test_integ_project_filter);
    RUN_TEST(test_integ_no_results);
    RUN_TEST(test_integ_not_equal);
    RUN_TEST(test_integ_first_half);
    RUN_TEST(test_integ_second_half);
    RUN_TEST(test_integ_project_all);
    RUN_TEST(test_integ_delete_parse);
    RUN_TEST(test_integ_create_parse);
    return UNITY_END();
}