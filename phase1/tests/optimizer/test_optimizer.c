#include "unity.h"
#include "optimizer.h"
#include "catalog.h"
#include "logical_plan.h"
#include "ast.h"
#include "parser.h"
#include <stdlib.h>
#include <string.h>
#include <math.h>

void setUp(void) {}
void tearDown(void) {}

static catalog_t *make_test_catalog(void) {
    catalog_t *c = catalog_create();
    catalog_add_table(c, "users", 10000, 100, true, "id");
    catalog_add_table(c, "orders", 50000, 500, true, "user_id");
    catalog_add_table(c, "products", 1000, 10, false, "");
    return c;
}

void test_seq_scan_cost(void) {
    catalog_t *c = make_test_catalog();
    parser_t *p = parser_create("SELECT * FROM products");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);

    TEST_ASSERT_NOT_NULL(plan);
    TEST_ASSERT_EQUAL_INT(PLAN_SEQ_SCAN, plan->type);
    TEST_ASSERT_EQUAL_STRING("products", plan->table_name);
    TEST_ASSERT_EQUAL_INT(1000, (int)plan->estimated_rows);
    TEST_ASSERT_EQUAL_INT(10, (int)plan->estimated_cost);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    catalog_destroy(c);
}

void test_index_scan_selection(void) {
    catalog_t *c = make_test_catalog();
    parser_t *p = parser_create("SELECT * FROM users WHERE id = 42");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);

    TEST_ASSERT_NOT_NULL(plan);
    TEST_ASSERT_EQUAL_INT(PLAN_FILTER, plan->type);
    TEST_ASSERT_NOT_NULL(plan->left);
    TEST_ASSERT_EQUAL_INT(PLAN_INDEX_SCAN, plan->left->type);
    TEST_ASSERT_EQUAL_STRING("id", plan->left->index_col);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    catalog_destroy(c);
}

void test_filter_selectivity(void) {
    catalog_t *c = make_test_catalog();
    parser_t *p = parser_create("SELECT * FROM products WHERE id > 500");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);

    TEST_ASSERT_NOT_NULL(plan);
    TEST_ASSERT_EQUAL_INT(PLAN_FILTER, plan->type);
    TEST_ASSERT_EQUAL_INT(PLAN_SEQ_SCAN, plan->left->type);

    double expected_rows = 1000 * 0.33;
    TEST_ASSERT_TRUE(fabs(plan->estimated_rows - expected_rows) < 1.0);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    catalog_destroy(c);
}

void test_project_on_top(void) {
    catalog_t *c = make_test_catalog();
    parser_t *p = parser_create("SELECT id FROM users");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);

    TEST_ASSERT_NOT_NULL(plan);
    TEST_ASSERT_EQUAL_INT(PLAN_PROJECT, plan->type);
    TEST_ASSERT_FALSE(plan->select_all);
    TEST_ASSERT_EQUAL_INT(1, plan->num_cols);
    TEST_ASSERT_EQUAL_STRING("id", plan->columns[0]);
    TEST_ASSERT_EQUAL_INT(PLAN_SEQ_SCAN, plan->left->type);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    catalog_destroy(c);
}

void test_multiple_filters(void) {
    catalog_t *c = make_test_catalog();
    parser_t *p = parser_create("SELECT * FROM users WHERE id >= 10 AND age < 30");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);

    TEST_ASSERT_NOT_NULL(plan);
    TEST_ASSERT_EQUAL_INT(PLAN_FILTER, plan->type);
    TEST_ASSERT_NOT_NULL(plan->left);
    TEST_ASSERT_EQUAL_INT(PLAN_FILTER, plan->left->type);
    TEST_ASSERT_EQUAL_INT(PLAN_INDEX_SCAN, plan->left->left->type);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    catalog_destroy(c);
}

void test_index_scan_cheaper_than_seq(void) {
    catalog_t *c = make_test_catalog();

    parser_t *p1 = parser_create("SELECT * FROM users WHERE id = 42");
    ast_stmt_t *s1 = parser_parse(p1);
    plan_node_t *plan1 = optimizer_optimize(s1, c);

    parser_t *p2 = parser_create("SELECT * FROM users");
    ast_stmt_t *s2 = parser_parse(p2);
    plan_node_t *plan2 = optimizer_optimize(s2, c);

    TEST_ASSERT_TRUE(plan1->estimated_cost < plan2->estimated_cost);

    plan_destroy(plan1);
    plan_destroy(plan2);
    free(s1);
    free(s2);
    parser_destroy(p1);
    parser_destroy(p2);
    catalog_destroy(c);
}

void test_catalog_lookup(void) {
    catalog_t *c = make_test_catalog();

    const catalog_entry_t *e = catalog_lookup(c, "users");
    TEST_ASSERT_NOT_NULL(e);
    TEST_ASSERT_EQUAL_INT(10000, e->num_rows);
    TEST_ASSERT_TRUE(e->has_index);
    TEST_ASSERT_EQUAL_STRING("id", e->index_col);

    TEST_ASSERT_NULL(catalog_lookup(c, "nonexistent"));

    catalog_destroy(c);
}

void test_plan_print(void) {
    catalog_t *c = make_test_catalog();
    parser_t *p = parser_create("SELECT id FROM users WHERE id = 5");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);

    TEST_ASSERT_NOT_NULL(plan);
    plan_print(plan, 0);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    catalog_destroy(c);
}

void test_no_index_stays_seq_scan(void) {
    catalog_t *c = make_test_catalog();
    parser_t *p = parser_create("SELECT * FROM products WHERE id = 5");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);

    TEST_ASSERT_NOT_NULL(plan);
    TEST_ASSERT_EQUAL_INT(PLAN_FILTER, plan->type);
    TEST_ASSERT_EQUAL_INT(PLAN_SEQ_SCAN, plan->left->type);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    catalog_destroy(c);
}

void test_large_table_index_better(void) {
    catalog_t *c = catalog_create();
    catalog_add_table(c, "big", 1000000, 100000, true, "pk");

    parser_t *p = parser_create("SELECT * FROM big WHERE pk = 123");
    ast_stmt_t *s = parser_parse(p);
    plan_node_t *plan = optimizer_optimize(s, c);

    TEST_ASSERT_NOT_NULL(plan);
    TEST_ASSERT_EQUAL_INT(PLAN_FILTER, plan->type);
    TEST_ASSERT_EQUAL_INT(PLAN_INDEX_SCAN, plan->left->type);

    TEST_ASSERT_TRUE(plan->estimated_rows < 1000);

    plan_destroy(plan);
    free(s);
    parser_destroy(p);
    catalog_destroy(c);
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_seq_scan_cost);
    RUN_TEST(test_index_scan_selection);
    RUN_TEST(test_filter_selectivity);
    RUN_TEST(test_project_on_top);
    RUN_TEST(test_multiple_filters);
    RUN_TEST(test_index_scan_cheaper_than_seq);
    RUN_TEST(test_catalog_lookup);
    RUN_TEST(test_plan_print);
    RUN_TEST(test_no_index_stays_seq_scan);
    RUN_TEST(test_large_table_index_better);
    return UNITY_END();
}