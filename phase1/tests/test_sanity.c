#include "unity.h"

void setUp(void) {}
void tearDown(void) {}

void test_integer_arithmetic(void) {
    TEST_ASSERT_EQUAL_INT(4, 2 + 2);
    TEST_ASSERT_EQUAL_INT(0, 1 - 1);
    TEST_ASSERT_EQUAL_INT(6, 2 * 3);
}

void test_string_equality(void) {
    TEST_ASSERT_EQUAL_STRING("miniDB", "miniDB");
    TEST_ASSERT_EQUAL_STRING_LEN("hello", "hello world", 5);
}

void test_boolean_logic(void) {
    TEST_ASSERT_TRUE(1);
    TEST_ASSERT_FALSE(0);
    TEST_ASSERT_TRUE(1 || 0);
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_integer_arithmetic);
    RUN_TEST(test_string_equality);
    RUN_TEST(test_boolean_logic);
    return UNITY_END();
}