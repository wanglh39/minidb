#include "unity.h"
#include "page.h"
#include <string.h>

void setUp(void) {}
void tearDown(void) {}

void test_page_init(void) {
    page_t page;
    page_init(&page, 42, PAGE_TYPE_HEAP);

    TEST_ASSERT_EQUAL_UINT32(42, page_get_id(&page));
    TEST_ASSERT_EQUAL_INT(PAGE_TYPE_HEAP, page_get_type(&page));
    TEST_ASSERT_EQUAL_UINT16(0, page_get_num_slots(&page));
    TEST_ASSERT_EQUAL_UINT16(PAGE_SIZE - PAGE_HEADER_SIZE, page_free_space(&page));
    TEST_ASSERT_EQUAL_UINT32(INVALID_PAGE_ID, page_get_next_page(&page));
}

void test_page_add_and_get_tuple(void) {
    page_t page;
    page_init(&page, 1, PAGE_TYPE_HEAP);

    const char *data = "hello world";
    uint16_t len = (uint16_t)(strlen(data) + 1);

    slot_id_t sid = page_add_tuple(&page, data, len);
    TEST_ASSERT_NOT_EQUAL(INVALID_SLOT_ID, sid);
    TEST_ASSERT_EQUAL_UINT16(0, sid);

    uint16_t got_len;
    const void *got = page_get_tuple(&page, sid, &got_len);
    TEST_ASSERT_NOT_NULL(got);
    TEST_ASSERT_EQUAL_UINT16(len, got_len);
    TEST_ASSERT_EQUAL_STRING(data, (const char *)got);

    TEST_ASSERT_EQUAL_UINT16(1, page_get_num_slots(&page));
}

void test_page_add_multiple_tuples(void) {
    page_t page;
    page_init(&page, 1, PAGE_TYPE_HEAP);

    const char *d1 = "first";
    const char *d2 = "second";
    const char *d3 = "third";

    slot_id_t s1 = page_add_tuple(&page, d1, 6);
    slot_id_t s2 = page_add_tuple(&page, d2, 7);
    slot_id_t s3 = page_add_tuple(&page, d3, 6);

    TEST_ASSERT_EQUAL_UINT16(0, s1);
    TEST_ASSERT_EQUAL_UINT16(1, s2);
    TEST_ASSERT_EQUAL_UINT16(2, s3);

    uint16_t len;
    TEST_ASSERT_EQUAL_STRING(d1, (const char *)page_get_tuple(&page, s1, &len));
    TEST_ASSERT_EQUAL_STRING(d2, (const char *)page_get_tuple(&page, s2, &len));
    TEST_ASSERT_EQUAL_STRING(d3, (const char *)page_get_tuple(&page, s3, &len));

    TEST_ASSERT_EQUAL_UINT16(3, page_get_num_slots(&page));
}

void test_page_delete_tuple(void) {
    page_t page;
    page_init(&page, 1, PAGE_TYPE_HEAP);

    slot_id_t sid = page_add_tuple(&page, "to be deleted", 14);
    TEST_ASSERT_FALSE(page_is_slot_deleted(&page, sid));

    TEST_ASSERT_TRUE(page_delete_tuple(&page, sid));
    TEST_ASSERT_TRUE(page_is_slot_deleted(&page, sid));

    uint16_t len;
    TEST_ASSERT_NULL(page_get_tuple(&page, sid, &len));
    TEST_ASSERT_EQUAL_UINT16(0, len);
}

void test_page_free_space_after_add(void) {
    page_t page;
    page_init(&page, 1, PAGE_TYPE_HEAP);

    uint16_t initial = page_free_space(&page);

    page_add_tuple(&page, "test", 5);

    TEST_ASSERT_EQUAL_UINT16(initial - 5 - SLOT_SIZE, page_free_space(&page));
}

void test_page_has_space(void) {
    page_t page;
    page_init(&page, 1, PAGE_TYPE_HEAP);

    uint16_t free = page_free_space(&page);

    TEST_ASSERT_TRUE(page_has_space(&page, (uint16_t)(free - SLOT_SIZE)));
    TEST_ASSERT_FALSE(page_has_space(&page, (uint16_t)(free - SLOT_SIZE + 1)));
}

void test_page_add_until_full(void) {
    page_t page;
    page_init(&page, 1, PAGE_TYPE_HEAP);

    int count = 0;
    char data[100] = {0};
    memset(data, 'x', 99);

    while (page_has_space(&page, 100)) {
        slot_id_t sid = page_add_tuple(&page, data, 100);
        TEST_ASSERT_NOT_EQUAL(INVALID_SLOT_ID, sid);
        count++;
    }

    TEST_ASSERT_EQUAL_INT(count, page_get_num_slots(&page));

    slot_id_t sid = page_add_tuple(&page, data, 100);
    TEST_ASSERT_EQUAL_UINT16(INVALID_SLOT_ID, sid);
}

void test_page_validate(void) {
    page_t page;
    page_init(&page, 5, PAGE_TYPE_HEAP);

    TEST_ASSERT_TRUE(page_validate(&page, 5));
    TEST_ASSERT_FALSE(page_validate(&page, 6));
}

void test_page_next_page_link(void) {
    page_t page;
    page_init(&page, 1, PAGE_TYPE_HEAP);

    TEST_ASSERT_EQUAL_UINT32(INVALID_PAGE_ID, page_get_next_page(&page));

    page_set_next_page(&page, 7);
    TEST_ASSERT_EQUAL_UINT32(7, page_get_next_page(&page));
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_page_init);
    RUN_TEST(test_page_add_and_get_tuple);
    RUN_TEST(test_page_add_multiple_tuples);
    RUN_TEST(test_page_delete_tuple);
    RUN_TEST(test_page_free_space_after_add);
    RUN_TEST(test_page_has_space);
    RUN_TEST(test_page_add_until_full);
    RUN_TEST(test_page_validate);
    RUN_TEST(test_page_next_page_link);
    return UNITY_END();
}