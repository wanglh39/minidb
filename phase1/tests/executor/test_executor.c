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

static exec_table_t users_table;
static exec_row_t users_rows[5];

static void init_users_table(void) {
    strcpy(users_table.name, "users");
    users_table.num_cols = 3;
    strcpy(users_table.cols[0].name, "id");
    strcpy(users_table.cols[1].name, "age");
    strcpy(users_table.cols[2].name, "score");

    users_rows[0].values[0] = 1; users_rows[0].values[1] = 25; users_rows[0].values[2] = 85;
    users_rows[1].values[0] = 2; users_rows[1].values[1] = 30; users_rows[1].values[2] = 90;
    users_rows[2].values[0] = 3; users_rows[2].values[1] = 35; users_rows[2].values[2] = 75;
    users_rows[3].values[0] = 4; users_rows[3].values[1] = 28; users_rows[3].values[2] = 95;
    users_rows[4].values[0] = 5; users_rows[4].values[1] = 40; users_rows[4].values[2] = 60;

    for (int i = 0; i < 5; i++) users_rows[i].num_cols = 3;
    users_table.rows = users_rows;
    users_table.num_rows = 5;
}

static catalog_t *make_catalog(void) {
    catalog_t *c = catalog_create();
    catalog_add_table(c, "users", 5, 1, true, "id");
    return c;
}

void test_seq_scan_all(void) {
    init_users_table();
    catalog_t *c = make_catalog();
    exec_table_t tables[] = {users_table};

    parser_t *p = parser_create("SELECT * FROM users");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);
    result_set_t *rs = executor_run(plan, tables, 1);

    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(5, rs->num_rows);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    result_set_destroy(rs);
    catalog_destroy(c);
}

void test_filter_equal(void) {
    init_users_table();
    catalog_t *c = make_catalog();
    exec_table_t tables[] = {users_table};

    parser_t *p = parser_create("SELECT * FROM users WHERE id = 3");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);
    result_set_t *rs = executor_run(plan, tables, 1);

    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(1, rs->num_rows);
    TEST_ASSERT_EQUAL_INT32(3, rs->rows[0].values[0]);
    TEST_ASSERT_EQUAL_INT32(35, rs->rows[0].values[1]);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    result_set_destroy(rs);
    catalog_destroy(c);
}

void test_filter_less_than(void) {
    init_users_table();
    catalog_t *c = make_catalog();
    exec_table_t tables[] = {users_table};

    parser_t *p = parser_create("SELECT * FROM users WHERE age < 30");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);
    result_set_t *rs = executor_run(plan, tables, 1);

    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(2, rs->num_rows);
    TEST_ASSERT_EQUAL_INT32(1, rs->rows[0].values[0]);
    TEST_ASSERT_EQUAL_INT32(4, rs->rows[1].values[0]);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    result_set_destroy(rs);
    catalog_destroy(c);
}

void test_filter_greater_equal(void) {
    init_users_table();
    catalog_t *c = make_catalog();
    exec_table_t tables[] = {users_table};

    parser_t *p = parser_create("SELECT * FROM users WHERE score >= 90");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);
    result_set_t *rs = executor_run(plan, tables, 1);

    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(2, rs->num_rows);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    result_set_destroy(rs);
    catalog_destroy(c);
}

void test_project_single_col(void) {
    init_users_table();
    catalog_t *c = make_catalog();
    exec_table_t tables[] = {users_table};

    parser_t *p = parser_create("SELECT id FROM users");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);
    result_set_t *rs = executor_run(plan, tables, 1);

    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(5, rs->num_rows);
    for (int i = 0; i < 5; i++)
        TEST_ASSERT_EQUAL_INT32(i + 1, rs->rows[i].values[0]);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    result_set_destroy(rs);
    catalog_destroy(c);
}

void test_project_with_filter(void) {
    init_users_table();
    catalog_t *c = make_catalog();
    exec_table_t tables[] = {users_table};

    parser_t *p = parser_create("SELECT id FROM users WHERE age > 28");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);
    result_set_t *rs = executor_run(plan, tables, 1);

    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(3, rs->num_rows);
    TEST_ASSERT_EQUAL_INT32(2, rs->rows[0].values[0]);
    TEST_ASSERT_EQUAL_INT32(3, rs->rows[1].values[0]);
    TEST_ASSERT_EQUAL_INT32(5, rs->rows[2].values[0]);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    result_set_destroy(rs);
    catalog_destroy(c);
}

void test_multiple_filters(void) {
    init_users_table();
    catalog_t *c = make_catalog();
    exec_table_t tables[] = {users_table};

    parser_t *p = parser_create("SELECT * FROM users WHERE id >= 2 AND age < 35");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);
    result_set_t *rs = executor_run(plan, tables, 1);

    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(2, rs->num_rows);
    TEST_ASSERT_EQUAL_INT32(2, rs->rows[0].values[0]);
    TEST_ASSERT_EQUAL_INT32(4, rs->rows[1].values[0]);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    result_set_destroy(rs);
    catalog_destroy(c);
}

void test_filter_no_match(void) {
    init_users_table();
    catalog_t *c = make_catalog();
    exec_table_t tables[] = {users_table};

    parser_t *p = parser_create("SELECT * FROM users WHERE id = 999");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);
    result_set_t *rs = executor_run(plan, tables, 1);

    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(0, rs->num_rows);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    result_set_destroy(rs);
    catalog_destroy(c);
}

void test_filter_not_equal(void) {
    init_users_table();
    catalog_t *c = make_catalog();
    exec_table_t tables[] = {users_table};

    parser_t *p = parser_create("SELECT * FROM users WHERE id != 3");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);
    result_set_t *rs = executor_run(plan, tables, 1);

    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(4, rs->num_rows);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    result_set_destroy(rs);
    catalog_destroy(c);
}

void test_result_set_print(void) {
    init_users_table();
    catalog_t *c = make_catalog();
    exec_table_t tables[] = {users_table};

    parser_t *p = parser_create("SELECT * FROM users WHERE id <= 2");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);
    result_set_t *rs = executor_run(plan, tables, 1);

    TEST_ASSERT_NOT_NULL(rs);
    TEST_ASSERT_EQUAL_INT(2, rs->num_rows);
    result_set_print(rs);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    result_set_destroy(rs);
    catalog_destroy(c);
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_seq_scan_all);
    RUN_TEST(test_filter_equal);
    RUN_TEST(test_filter_less_than);
    RUN_TEST(test_filter_greater_equal);
    RUN_TEST(test_project_single_col);
    RUN_TEST(test_project_with_filter);
    RUN_TEST(test_multiple_filters);
    RUN_TEST(test_filter_no_match);
    RUN_TEST(test_filter_not_equal);
    RUN_TEST(test_result_set_print);
    return UNITY_END();
}