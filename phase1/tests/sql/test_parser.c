#include "unity.h"
#include "parser.h"
#include "ast.h"
#include <stdlib.h>
#include <string.h>

void setUp(void) {}
void tearDown(void) {}

void test_parse_select_star(void) {
    parser_t *p = parser_create("SELECT * FROM users");
    ast_stmt_t *s = parser_parse(p);
    TEST_ASSERT_NOT_NULL(s);
    TEST_ASSERT_EQUAL_INT(AST_SELECT, s->type);
    TEST_ASSERT_TRUE(s->select_all);
    TEST_ASSERT_EQUAL_STRING("users", s->table);
    free(s);
    parser_destroy(p);
}

void test_parse_select_cols(void) {
    parser_t *p = parser_create("SELECT id, name FROM users");
    ast_stmt_t *s = parser_parse(p);
    TEST_ASSERT_NOT_NULL(s);
    TEST_ASSERT_FALSE(s->select_all);
    TEST_ASSERT_EQUAL_INT(2, s->num_cols);
    TEST_ASSERT_EQUAL_STRING("id", s->columns[0]);
    TEST_ASSERT_EQUAL_STRING("name", s->columns[1]);
    free(s);
    parser_destroy(p);
}

void test_parse_select_where(void) {
    parser_t *p = parser_create("SELECT * FROM users WHERE id = 42");
    ast_stmt_t *s = parser_parse(p);
    TEST_ASSERT_NOT_NULL(s);
    TEST_ASSERT_EQUAL_INT(1, s->num_where);
    TEST_ASSERT_EQUAL_STRING("id", s->where[0].column);
    TEST_ASSERT_EQUAL_STRING("=", s->where[0].op);
    TEST_ASSERT_EQUAL_INT(VAL_INT, s->where[0].value.type);
    TEST_ASSERT_EQUAL_INT32(42, s->where[0].value.int_val);
    free(s);
    parser_destroy(p);
}

void test_parse_select_where_multi(void) {
    parser_t *p = parser_create("SELECT * FROM users WHERE id >= 10 AND age < 30");
    ast_stmt_t *s = parser_parse(p);
    TEST_ASSERT_NOT_NULL(s);
    TEST_ASSERT_EQUAL_INT(2, s->num_where);
    TEST_ASSERT_EQUAL_STRING(">=", s->where[0].op);
    TEST_ASSERT_EQUAL_STRING("<", s->where[1].op);
    TEST_ASSERT_EQUAL_INT32(10, s->where[0].value.int_val);
    TEST_ASSERT_EQUAL_INT32(30, s->where[1].value.int_val);
    free(s);
    parser_destroy(p);
}

void test_parse_insert(void) {
    parser_t *p = parser_create("INSERT INTO users (id, age) VALUES (1, 25)");
    ast_stmt_t *s = parser_parse(p);
    TEST_ASSERT_NOT_NULL(s);
    TEST_ASSERT_EQUAL_INT(AST_INSERT, s->type);
    TEST_ASSERT_EQUAL_STRING("users", s->table);
    TEST_ASSERT_EQUAL_INT(2, s->num_cols);
    TEST_ASSERT_EQUAL_INT(2, s->num_values);
    TEST_ASSERT_EQUAL_INT32(1, s->values[0].int_val);
    TEST_ASSERT_EQUAL_INT32(25, s->values[1].int_val);
    free(s);
    parser_destroy(p);
}

void test_parse_insert_string_value(void) {
    parser_t *p = parser_create("INSERT INTO users (id, name) VALUES (1, 'Alice')");
    ast_stmt_t *s = parser_parse(p);
    TEST_ASSERT_NOT_NULL(s);
    TEST_ASSERT_EQUAL_INT(VAL_STRING, s->values[1].type);
    TEST_ASSERT_EQUAL_STRING("Alice", s->values[1].str_val);
    free(s);
    parser_destroy(p);
}

void test_parse_delete(void) {
    parser_t *p = parser_create("DELETE FROM users WHERE id = 5");
    ast_stmt_t *s = parser_parse(p);
    TEST_ASSERT_NOT_NULL(s);
    TEST_ASSERT_EQUAL_INT(AST_DELETE, s->type);
    TEST_ASSERT_EQUAL_STRING("users", s->table);
    TEST_ASSERT_EQUAL_INT(1, s->num_where);
    free(s);
    parser_destroy(p);
}

void test_parse_delete_all(void) {
    parser_t *p = parser_create("DELETE FROM users");
    ast_stmt_t *s = parser_parse(p);
    TEST_ASSERT_NOT_NULL(s);
    TEST_ASSERT_EQUAL_INT(AST_DELETE, s->type);
    TEST_ASSERT_EQUAL_INT(0, s->num_where);
    free(s);
    parser_destroy(p);
}

void test_parse_create(void) {
    parser_t *p = parser_create("CREATE TABLE users (id INT PRIMARY KEY, age INT, score FLOAT)");
    ast_stmt_t *s = parser_parse(p);
    TEST_ASSERT_NOT_NULL(s);
    TEST_ASSERT_EQUAL_INT(AST_CREATE, s->type);
    TEST_ASSERT_EQUAL_STRING("users", s->table);
    TEST_ASSERT_EQUAL_INT(3, s->num_col_defs);
    TEST_ASSERT_EQUAL_STRING("id", s->col_defs[0].name);
    TEST_ASSERT_TRUE(s->col_defs[0].primary_key);
    TEST_ASSERT_EQUAL_INT(AST_COL_INT32, s->col_defs[1].type);
    TEST_ASSERT_EQUAL_INT(AST_COL_FLOAT, s->col_defs[2].type);
    free(s);
    parser_destroy(p);
}

void test_parse_select_float_where(void) {
    parser_t *p = parser_create("SELECT * FROM scores WHERE value >= 3.14");
    ast_stmt_t *s = parser_parse(p);
    TEST_ASSERT_NOT_NULL(s);
    TEST_ASSERT_EQUAL_INT(VAL_FLOAT, s->where[0].value.type);
    free(s);
    parser_destroy(p);
}

void test_parse_invalid(void) {
    parser_t *p = parser_create("NOT SQL");
    ast_stmt_t *s = parser_parse(p);
    TEST_ASSERT_NULL(s);
    parser_destroy(p);
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_parse_select_star);
    RUN_TEST(test_parse_select_cols);
    RUN_TEST(test_parse_select_where);
    RUN_TEST(test_parse_select_where_multi);
    RUN_TEST(test_parse_insert);
    RUN_TEST(test_parse_insert_string_value);
    RUN_TEST(test_parse_delete);
    RUN_TEST(test_parse_delete_all);
    RUN_TEST(test_parse_create);
    RUN_TEST(test_parse_select_float_where);
    RUN_TEST(test_parse_invalid);
    return UNITY_END();
}