#include "unity.h"
#include "btree.h"
#include "buffer_pool.h"
#include "pager.h"
#include <stdio.h>

void setUp(void) {}
void tearDown(void) {}

static const char *TEST_FILE = "test_btree_tmp.db";

static void cleanup(void) { remove(TEST_FILE); }

static rid_t make_rid(uint32_t pid, uint16_t sid) {
    rid_t r = { pid, sid };
    return r;
}

void test_btree_create(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 32, REPLACER_LRU);
    btree_t *tree = btree_create(bp);
    TEST_ASSERT_NOT_NULL(tree);
    TEST_ASSERT_NOT_EQUAL(INVALID_PAGE_ID, btree_root_pid(tree));
    btree_destroy(tree);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_btree_insert_find_small(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 32, REPLACER_LRU);
    btree_t *tree = btree_create(bp);

    for (int i = 1; i <= 10; i++) {
        TEST_ASSERT_TRUE(btree_insert(tree, i * 10, make_rid(i, i)));
    }

    for (int i = 1; i <= 10; i++) {
        rid_t rid;
        TEST_ASSERT_TRUE(btree_find(tree, i * 10, &rid));
        TEST_ASSERT_EQUAL_UINT32(i, rid.page_id);
        TEST_ASSERT_EQUAL_UINT16(i, rid.slot_id);
    }

    TEST_ASSERT_FALSE(btree_find(tree, 5, NULL));
    TEST_ASSERT_FALSE(btree_find(tree, 105, NULL));

    btree_destroy(tree);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_btree_duplicate_key_rejected(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 32, REPLACER_LRU);
    btree_t *tree = btree_create(bp);

    TEST_ASSERT_TRUE(btree_insert(tree, 42, make_rid(1, 1)));
    TEST_ASSERT_FALSE(btree_insert(tree, 42, make_rid(2, 2)));

    rid_t rid;
    btree_find(tree, 42, &rid);
    TEST_ASSERT_EQUAL_UINT32(1, rid.page_id);

    btree_destroy(tree);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_btree_insert_trigger_split(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 64, REPLACER_LRU);
    btree_t *tree = btree_create(bp);

    int count = 200;
    for (int i = 1; i <= count; i++) {
        TEST_ASSERT_TRUE(btree_insert(tree, i, make_rid(i, 0)));
    }

    for (int i = 1; i <= count; i++) {
        rid_t rid;
        TEST_ASSERT_TRUE(btree_find(tree, i, &rid));
        TEST_ASSERT_EQUAL_UINT32(i, rid.page_id);
    }

    TEST_ASSERT_FALSE(btree_find(tree, 0, NULL));
    TEST_ASSERT_FALSE(btree_find(tree, count + 1, NULL));

    btree_destroy(tree);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_btree_insert_reverse_order(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 64, REPLACER_LRU);
    btree_t *tree = btree_create(bp);

    for (int i = 200; i >= 1; i--) {
        TEST_ASSERT_TRUE(btree_insert(tree, i, make_rid(i, 0)));
    }

    for (int i = 1; i <= 200; i++) {
        rid_t rid;
        TEST_ASSERT_TRUE(btree_find(tree, i, &rid));
        TEST_ASSERT_EQUAL_UINT32(i, rid.page_id);
    }

    btree_destroy(tree);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_btree_delete(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 64, REPLACER_LRU);
    btree_t *tree = btree_create(bp);

    for (int i = 1; i <= 50; i++) {
        btree_insert(tree, i, make_rid(i, 0));
    }

    TEST_ASSERT_TRUE(btree_delete(tree, 25));
    TEST_ASSERT_FALSE(btree_find(tree, 25, NULL));
    TEST_ASSERT_TRUE(btree_find(tree, 24, NULL));
    TEST_ASSERT_TRUE(btree_find(tree, 26, NULL));

    TEST_ASSERT_FALSE(btree_delete(tree, 25));
    TEST_ASSERT_FALSE(btree_delete(tree, 999));

    for (int i = 1; i <= 50; i += 2) {
        btree_delete(tree, i);
    }
    for (int i = 1; i <= 50; i++) {
        if (i % 2 == 1) {
            TEST_ASSERT_FALSE(btree_find(tree, i, NULL));
        } else {
            TEST_ASSERT_TRUE(btree_find(tree, i, NULL));
        }
    }

    btree_destroy(tree);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_btree_range_query(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 64, REPLACER_LRU);
    btree_t *tree = btree_create(bp);

    for (int i = 1; i <= 100; i++) {
        btree_insert(tree, i * 10, make_rid(i, 0));
    }

    btree_cursor_t *cur = btree_range_open(tree, 250, 450);
    int count = 0;
    btree_key_t key;
    rid_t rid;
    while (btree_range_next(cur, &key, &rid)) {
        count++;
        TEST_ASSERT_TRUE(key >= 250 && key <= 450);
    }
    btree_range_close(cur);
    TEST_ASSERT_EQUAL_INT(21, count);

    btree_destroy(tree);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_btree_range_empty(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 32, REPLACER_LRU);
    btree_t *tree = btree_create(bp);

    for (int i = 1; i <= 10; i++) {
        btree_insert(tree, i, make_rid(i, 0));
    }

    btree_cursor_t *cur = btree_range_open(tree, 100, 200);
    btree_key_t key;
    rid_t rid;
    TEST_ASSERT_FALSE(btree_range_next(cur, &key, &rid));
    btree_range_close(cur);

    btree_destroy(tree);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_btree_persistence(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 64, REPLACER_LRU);
    btree_t *tree = btree_create(bp);

    for (int i = 1; i <= 100; i++) {
        btree_insert(tree, i, make_rid(i, 0));
    }
    page_id_t root = btree_root_pid(tree);

    btree_destroy(tree);
    bp_destroy(bp);
    pager_close(pager);

    pager = pager_open(TEST_FILE);
    bp = bp_create(pager, 64, REPLACER_LRU);
    tree = btree_open(bp, root);

    for (int i = 1; i <= 100; i++) {
        rid_t rid;
        TEST_ASSERT_TRUE(btree_find(tree, i, &rid));
        TEST_ASSERT_EQUAL_UINT32(i, rid.page_id);
    }

    btree_destroy(tree);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_btree_create);
    RUN_TEST(test_btree_insert_find_small);
    RUN_TEST(test_btree_duplicate_key_rejected);
    RUN_TEST(test_btree_insert_trigger_split);
    RUN_TEST(test_btree_insert_reverse_order);
    RUN_TEST(test_btree_delete);
    RUN_TEST(test_btree_range_query);
    RUN_TEST(test_btree_range_empty);
    RUN_TEST(test_btree_persistence);
    return UNITY_END();
}