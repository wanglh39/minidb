#include "unity.h"
#include "buffer_pool.h"
#include "pager.h"
#include "page.h"
#include <string.h>
#include <stdio.h>

void setUp(void) {}
void tearDown(void) {}

static const char *TEST_FILE = "test_bp_tmp.db";

static void cleanup(void) {
    remove(TEST_FILE);
}

void test_bp_create_destroy(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 4, REPLACER_LRU);
    TEST_ASSERT_NOT_NULL(bp);

    bp_stats_t stats;
    bp_get_stats(bp, &stats);
    TEST_ASSERT_EQUAL_INT(0, stats.hits);
    TEST_ASSERT_EQUAL_INT(0, stats.misses);

    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_bp_fetch_hit_miss(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);

    page_id_t pid = pager_allocate(pager);
    page_t raw;
    page_init(&raw, pid, PAGE_TYPE_HEAP);
    page_add_tuple(&raw, "data", 5);
    pager_write(pager, pid, &raw);

    buffer_pool_t *bp = bp_create(pager, 4, REPLACER_LRU);

    page_t *p1 = bp_fetch_page(bp, pid);
    TEST_ASSERT_NOT_NULL(p1);
    bp_unpin_page(bp, pid, false);

    page_t *p2 = bp_fetch_page(bp, pid);
    TEST_ASSERT_NOT_NULL(p2);

    bp_stats_t stats;
    bp_get_stats(bp, &stats);
    TEST_ASSERT_EQUAL_INT(1, stats.misses);
    TEST_ASSERT_EQUAL_INT(1, stats.hits);

    bp_unpin_page(bp, pid, false);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_bp_eviction(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);

    for (int i = 0; i < 5; i++) {
        page_id_t pid = pager_allocate(pager);
        page_t raw;
        page_init(&raw, pid, PAGE_TYPE_HEAP);
        pager_write(pager, pid, &raw);
    }

    buffer_pool_t *bp = bp_create(pager, 2, REPLACER_LRU);

    bp_fetch_page(bp, 0);
    bp_unpin_page(bp, 0, false);
    bp_fetch_page(bp, 1);
    bp_unpin_page(bp, 1, false);
    bp_fetch_page(bp, 2);
    bp_unpin_page(bp, 2, false);

    bp_stats_t stats;
    bp_get_stats(bp, &stats);
    TEST_ASSERT_EQUAL_INT(3, stats.misses);
    TEST_ASSERT_TRUE(stats.evictions >= 1);

    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_bp_dirty_writeback(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    page_id_t pid = pager_allocate(pager);
    page_t raw;
    page_init(&raw, pid, PAGE_TYPE_HEAP);
    pager_write(pager, pid, &raw);

    buffer_pool_t *bp = bp_create(pager, 4, REPLACER_LRU);

    page_t *p = bp_fetch_page(bp, pid);
    TEST_ASSERT_NOT_NULL(p);
    page_add_tuple(p, "modified", 9);
    bp_unpin_page(bp, pid, true);

    bp_destroy(bp);
    pager_close(pager);

    pager = pager_open(TEST_FILE);
    page_t check;
    pager_read(pager, pid, &check);
    uint16_t len;
    const char *data = (const char *)page_get_tuple(&check, 0, &len);
    TEST_ASSERT_EQUAL_STRING("modified", data);
    pager_close(pager);
    cleanup();
}

void test_bp_new_page(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 4, REPLACER_LRU);

    page_t *p;
    page_id_t pid = bp_new_page(bp, &p);
    TEST_ASSERT_NOT_EQUAL(INVALID_PAGE_ID, pid);
    TEST_ASSERT_NOT_NULL(p);

    page_add_tuple(p, "new", 4);
    bp_unpin_page(bp, pid, true);

    bp_destroy(bp);
    pager_close(pager);

    pager = pager_open(TEST_FILE);
    TEST_ASSERT_EQUAL_UINT32(1, pager_num_pages(pager));
    page_t check;
    pager_read(pager, pid, &check);
    uint16_t len;
    const char *data = (const char *)page_get_tuple(&check, 0, &len);
    TEST_ASSERT_EQUAL_STRING("new", data);
    pager_close(pager);
    cleanup();
}

void test_bp_flush_page(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    page_id_t pid = pager_allocate(pager);
    page_t raw;
    page_init(&raw, pid, PAGE_TYPE_HEAP);
    pager_write(pager, pid, &raw);

    buffer_pool_t *bp = bp_create(pager, 4, REPLACER_LRU);

    page_t *p = bp_fetch_page(bp, pid);
    page_add_tuple(p, "flushed", 8);
    bp_unpin_page(bp, pid, true);

    TEST_ASSERT_TRUE(bp_flush_page(bp, pid));

    bp_stats_t stats;
    bp_get_stats(bp, &stats);
    TEST_ASSERT_TRUE(stats.dirty_writes >= 1);

    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_bp_multiple_replacer_types(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    for (int i = 0; i < 4; i++) {
        page_id_t pid = pager_allocate(pager);
        page_t raw;
        page_init(&raw, pid, PAGE_TYPE_HEAP);
        pager_write(pager, pid, &raw);
    }

    replacer_type_t types[] = {REPLACER_LRU, REPLACER_CLOCK, REPLACER_LRU_K};
    for (int t = 0; t < 3; t++) {
        buffer_pool_t *bp = bp_create(pager, 2, types[t]);
        page_t *p = bp_fetch_page(bp, 0);
        TEST_ASSERT_NOT_NULL(p);
        bp_unpin_page(bp, 0, false);
        bp_destroy(bp);
    }

    pager_close(pager);
    cleanup();
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_bp_create_destroy);
    RUN_TEST(test_bp_fetch_hit_miss);
    RUN_TEST(test_bp_eviction);
    RUN_TEST(test_bp_dirty_writeback);
    RUN_TEST(test_bp_new_page);
    RUN_TEST(test_bp_flush_page);
    RUN_TEST(test_bp_multiple_replacer_types);
    return UNITY_END();
}