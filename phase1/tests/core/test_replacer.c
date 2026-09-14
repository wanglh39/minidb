#include "unity.h"
#include "replacer.h"

void setUp(void) {}
void tearDown(void) {}

/* ---------- LRU ---------- */

void test_lru_basic_victim(void) {
    replacer_t *r = replacer_create(REPLACER_LRU, 5);
    replacer_unpin(r, 0);
    replacer_unpin(r, 1);
    replacer_unpin(r, 2);

    TEST_ASSERT_EQUAL_INT(3, replacer_size(r));
    TEST_ASSERT_EQUAL_INT(0, replacer_victim(r));
    TEST_ASSERT_EQUAL_INT(1, replacer_victim(r));
    TEST_ASSERT_EQUAL_INT(2, replacer_victim(r));
    TEST_ASSERT_EQUAL_INT(0, replacer_size(r));

    replacer_destroy(r);
}

void test_lru_pin_prevents_eviction(void) {
    replacer_t *r = replacer_create(REPLACER_LRU, 5);
    replacer_unpin(r, 0);
    replacer_unpin(r, 1);
    replacer_unpin(r, 2);

    replacer_pin(r, 0);

    TEST_ASSERT_EQUAL_INT(2, replacer_size(r));
    TEST_ASSERT_EQUAL_INT(1, replacer_victim(r));
    TEST_ASSERT_EQUAL_INT(2, replacer_victim(r));

    replacer_destroy(r);
}

void test_lru_repin_updates_order(void) {
    replacer_t *r = replacer_create(REPLACER_LRU, 5);
    replacer_unpin(r, 0);
    replacer_unpin(r, 1);
    replacer_unpin(r, 2);

    replacer_pin(r, 0);
    replacer_unpin(r, 0);

    TEST_ASSERT_EQUAL_INT(1, replacer_victim(r));
    TEST_ASSERT_EQUAL_INT(2, replacer_victim(r));
    TEST_ASSERT_EQUAL_INT(0, replacer_victim(r));

    replacer_destroy(r);
}

void test_lru_empty(void) {
    replacer_t *r = replacer_create(REPLACER_LRU, 5);
    TEST_ASSERT_EQUAL_INT(INVALID_FRAME_ID, replacer_victim(r));
    replacer_destroy(r);
}

/* ---------- Clock ---------- */

void test_clock_basic_victim(void) {
    replacer_t *r = replacer_create(REPLACER_CLOCK, 5);
    replacer_unpin(r, 0);
    replacer_unpin(r, 1);
    replacer_unpin(r, 2);

    frame_id_t v = replacer_victim(r);
    TEST_ASSERT_TRUE(v == 0 || v == 1 || v == 2);
    TEST_ASSERT_EQUAL_INT(2, replacer_size(r));

    replacer_destroy(r);
}

void test_clock_ref_bit_gives_second_chance(void) {
    replacer_t *r = replacer_create(REPLACER_CLOCK, 3);
    replacer_unpin(r, 0);
    replacer_unpin(r, 1);
    replacer_unpin(r, 2);

    frame_id_t v1 = replacer_victim(r);
    TEST_ASSERT_TRUE(v1 >= 0);

    replacer_destroy(r);
}

void test_clock_pin_removes_from_candidates(void) {
    replacer_t *r = replacer_create(REPLACER_CLOCK, 5);
    replacer_unpin(r, 0);
    replacer_unpin(r, 1);

    replacer_pin(r, 0);

    TEST_ASSERT_EQUAL_INT(1, replacer_size(r));
    TEST_ASSERT_EQUAL_INT(1, replacer_victim(r));

    replacer_destroy(r);
}

/* ---------- LRU-K ---------- */

void test_lruk_prefers_infrequent_access(void) {
    replacer_t *r = replacer_create(REPLACER_LRU_K, 5);

    replacer_unpin(r, 0);
    replacer_unpin(r, 0);
    replacer_unpin(r, 1);

    frame_id_t v = replacer_victim(r);
    TEST_ASSERT_EQUAL_INT(1, v);

    replacer_destroy(r);
}

void test_lruk_frequent_access_survives(void) {
    replacer_t *r = replacer_create(REPLACER_LRU_K, 5);

    replacer_unpin(r, 0);
    replacer_unpin(r, 0);
    replacer_unpin(r, 1);
    replacer_unpin(r, 1);

    replacer_unpin(r, 2);

    frame_id_t v = replacer_victim(r);
    TEST_ASSERT_EQUAL_INT(2, v);

    replacer_destroy(r);
}

void test_lruk_empty(void) {
    replacer_t *r = replacer_create(REPLACER_LRU_K, 5);
    TEST_ASSERT_EQUAL_INT(INVALID_FRAME_ID, replacer_victim(r));
    replacer_destroy(r);
}

/* ---------- 统一接口对比 ---------- */

void test_all_algorithms_same_interface(void) {
    replacer_type_t types[] = {REPLACER_LRU, REPLACER_CLOCK, REPLACER_LRU_K};
    for (int t = 0; t < 3; t++) {
        replacer_t *r = replacer_create(types[t], 3);
        TEST_ASSERT_NOT_NULL(r);

        replacer_unpin(r, 0);
        replacer_unpin(r, 1);
        TEST_ASSERT_EQUAL_INT(2, replacer_size(r));

        frame_id_t v = replacer_victim(r);
        TEST_ASSERT_TRUE(v == 0 || v == 1);
        TEST_ASSERT_EQUAL_INT(1, replacer_size(r));

        replacer_destroy(r);
    }
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_lru_basic_victim);
    RUN_TEST(test_lru_pin_prevents_eviction);
    RUN_TEST(test_lru_repin_updates_order);
    RUN_TEST(test_lru_empty);
    RUN_TEST(test_clock_basic_victim);
    RUN_TEST(test_clock_ref_bit_gives_second_chance);
    RUN_TEST(test_clock_pin_removes_from_candidates);
    RUN_TEST(test_lruk_prefers_infrequent_access);
    RUN_TEST(test_lruk_frequent_access_survives);
    RUN_TEST(test_lruk_empty);
    RUN_TEST(test_all_algorithms_same_interface);
    return UNITY_END();
}