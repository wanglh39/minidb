#include "unity.h"
#include "schema.h"
#include "tuple.h"
#include "heap.h"
#include "table.h"
#include "buffer_pool.h"
#include "pager.h"
#include <stdio.h>

void setUp(void) {}
void tearDown(void) {}

static const char *TEST_FILE = "test_heap_tmp.db";
static void cleanup(void) { remove(TEST_FILE); }

static schema_t *make_user_schema(void) {
    static schema_t schema;
    schema_init(&schema, "users");
    schema_add_col(&schema, "id", COL_INT32, false);
    schema_add_col(&schema, "age", COL_INT32, true);
    schema_add_col(&schema, "score", COL_FLOAT, true);
    return &schema;
}

void test_schema_create(void) {
    schema_t s;
    schema_init(&s, "test");
    TEST_ASSERT_EQUAL_INT(0, s.num_cols);

    int c0 = schema_add_col(&s, "id", COL_INT32, false);
    int c1 = schema_add_col(&s, "name", COL_INT64, true);
    TEST_ASSERT_EQUAL_INT(0, c0);
    TEST_ASSERT_EQUAL_INT(1, c1);
    TEST_ASSERT_EQUAL_INT(2, s.num_cols);

    TEST_ASSERT_EQUAL_INT(0, schema_find_col(&s, "id"));
    TEST_ASSERT_EQUAL_INT(1, schema_find_col(&s, "name"));
    TEST_ASSERT_EQUAL_INT(-1, schema_find_col(&s, "missing"));
}

void test_tuple_set_get(void) {
    schema_t *s = make_user_schema();
    tuple_t *t = tuple_create(s);

    tuple_set_int32(t, 0, 42);
    tuple_set_int32(t, 1, 25);
    tuple_set_float(t, 2, 95.5f);

    TEST_ASSERT_EQUAL_INT32(42, tuple_get_int32(t, 0));
    TEST_ASSERT_EQUAL_INT32(25, tuple_get_int32(t, 1));
    TEST_ASSERT_EQUAL_FLOAT(95.5f, tuple_get_float(t, 2));

    TEST_ASSERT_FALSE(tuple_is_null(t, 0));
    TEST_ASSERT_FALSE(tuple_is_null(t, 1));

    tuple_destroy(t);
}

void test_tuple_null(void) {
    schema_t *s = make_user_schema();
    tuple_t *t = tuple_create(s);

    tuple_set_int32(t, 0, 1);
    tuple_set_null(t, 1);
    tuple_set_null(t, 2);

    TEST_ASSERT_FALSE(tuple_is_null(t, 0));
    TEST_ASSERT_TRUE(tuple_is_null(t, 1));
    TEST_ASSERT_TRUE(tuple_is_null(t, 2));

    tuple_destroy(t);
}

void test_tuple_serialize_deserialize(void) {
    schema_t *s = make_user_schema();
    tuple_t *t1 = tuple_create(s);
    tuple_set_int32(t1, 0, 100);
    tuple_set_int32(t1, 1, 30);
    tuple_set_float(t1, 2, 88.5f);

    uint8_t buf[PAGE_SIZE];
    uint16_t len = tuple_serialize(t1, buf);

    tuple_t *t2 = tuple_create(s);
    tuple_deserialize(t2, buf, len);

    TEST_ASSERT_EQUAL_INT32(100, tuple_get_int32(t2, 0));
    TEST_ASSERT_EQUAL_INT32(30, tuple_get_int32(t2, 1));
    TEST_ASSERT_EQUAL_FLOAT(88.5f, tuple_get_float(t2, 2));

    tuple_destroy(t1);
    tuple_destroy(t2);
}

void test_heap_insert_fetch(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 32, REPLACER_LRU);
    schema_t *s = make_user_schema();
    heap_t *heap = heap_create(bp, s);

    tuple_t *t = tuple_create(s);
    tuple_set_int32(t, 0, 1);
    tuple_set_int32(t, 1, 20);
    tuple_set_float(t, 2, 90.0f);

    rid_t rid = heap_insert(heap, t);
    TEST_ASSERT_NOT_EQUAL(INVALID_PAGE_ID, rid.page_id);

    tuple_t *fetched = heap_fetch(heap, rid);
    TEST_ASSERT_NOT_NULL(fetched);
    TEST_ASSERT_EQUAL_INT32(1, tuple_get_int32(fetched, 0));
    TEST_ASSERT_EQUAL_INT32(20, tuple_get_int32(fetched, 1));
    TEST_ASSERT_EQUAL_FLOAT(90.0f, tuple_get_float(fetched, 2));

    tuple_destroy(t);
    tuple_destroy(fetched);
    heap_destroy(heap);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_heap_scan(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 32, REPLACER_LRU);
    schema_t *s = make_user_schema();
    heap_t *heap = heap_create(bp, s);

    for (int i = 1; i <= 20; i++) {
        tuple_t *t = tuple_create(s);
        tuple_set_int32(t, 0, i);
        tuple_set_int32(t, 1, i * 2);
        tuple_set_float(t, 2, (float)i * 1.5f);
        heap_insert(heap, t);
        tuple_destroy(t);
    }

    heap_scan_t *scan = heap_scan_open(heap);
    tuple_t *t = tuple_create(s);
    rid_t rid;
    int count = 0;
    while (heap_scan_next(scan, &rid, t)) {
        count++;
        int32_t id = tuple_get_int32(t, 0);
        TEST_ASSERT_TRUE(id >= 1 && id <= 20);
    }
    heap_scan_close(scan);
    tuple_destroy(t);

    TEST_ASSERT_EQUAL_INT(20, count);

    heap_destroy(heap);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_heap_delete(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 32, REPLACER_LRU);
    schema_t *s = make_user_schema();
    heap_t *heap = heap_create(bp, s);

    tuple_t *t = tuple_create(s);
    tuple_set_int32(t, 0, 1);
    tuple_set_int32(t, 1, 20);
    rid_t rid = heap_insert(heap, t);
    tuple_destroy(t);

    TEST_ASSERT_TRUE(heap_delete(heap, rid));

    tuple_t *fetched = heap_fetch(heap, rid);
    TEST_ASSERT_NULL(fetched);

    heap_destroy(heap);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_table_insert_find(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 64, REPLACER_LRU);
    schema_t *s = make_user_schema();
    table_t *table = table_create(bp, s, 0);

    for (int i = 1; i <= 50; i++) {
        tuple_t *t = tuple_create(s);
        tuple_set_int32(t, 0, i * 10);
        tuple_set_int32(t, 1, 20 + i);
        tuple_set_float(t, 2, (float)i * 2.5f);
        table_insert(table, t);
        tuple_destroy(t);
    }

    tuple_t *found = table_find(table, 250);
    TEST_ASSERT_NOT_NULL(found);
    TEST_ASSERT_EQUAL_INT32(250, tuple_get_int32(found, 0));
    TEST_ASSERT_EQUAL_INT32(45, tuple_get_int32(found, 1));
    tuple_destroy(found);

    TEST_ASSERT_NULL(table_find(table, 999));

    table_destroy(table);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_table_delete(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 32, REPLACER_LRU);
    schema_t *s = make_user_schema();
    table_t *table = table_create(bp, s, 0);

    for (int i = 1; i <= 10; i++) {
        tuple_t *t = tuple_create(s);
        tuple_set_int32(t, 0, i);
        tuple_set_int32(t, 1, i * 10);
        table_insert(table, t);
        tuple_destroy(t);
    }

    TEST_ASSERT_TRUE(table_delete(table, 5));
    TEST_ASSERT_NULL(table_find(table, 5));
    TEST_ASSERT_NOT_NULL(table_find(table, 4));
    TEST_ASSERT_NOT_NULL(table_find(table, 6));

    table_destroy(table);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

void test_table_range_scan(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    buffer_pool_t *bp = bp_create(pager, 64, REPLACER_LRU);
    schema_t *s = make_user_schema();
    table_t *table = table_create(bp, s, 0);

    for (int i = 1; i <= 100; i++) {
        tuple_t *t = tuple_create(s);
        tuple_set_int32(t, 0, i);
        tuple_set_int32(t, 1, i * 2);
        table_insert(table, t);
        tuple_destroy(t);
    }

    table_scan_t *scan = table_scan_open(table, 30, 50);
    tuple_t *t = tuple_create(s);
    int count = 0;
    while (table_scan_next(scan, t)) {
        int32_t id = tuple_get_int32(t, 0);
        TEST_ASSERT_TRUE(id >= 30 && id <= 50);
        count++;
    }
    table_scan_close(scan);
    tuple_destroy(t);

    TEST_ASSERT_EQUAL_INT(21, count);

    table_destroy(table);
    bp_destroy(bp);
    pager_close(pager);
    cleanup();
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_schema_create);
    RUN_TEST(test_tuple_set_get);
    RUN_TEST(test_tuple_null);
    RUN_TEST(test_tuple_serialize_deserialize);
    RUN_TEST(test_heap_insert_fetch);
    RUN_TEST(test_heap_scan);
    RUN_TEST(test_heap_delete);
    RUN_TEST(test_table_insert_find);
    RUN_TEST(test_table_delete);
    RUN_TEST(test_table_range_scan);
    return UNITY_END();
}