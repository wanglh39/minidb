#include "unity.h"
#include "file_manager.h"
#include "page.h"
#include <string.h>
#include <stdio.h>

void setUp(void) {}
void tearDown(void) {}

static const char *TEST_FILE = "test_fm_tmp.db";

static void cleanup(void) {
    remove(TEST_FILE);
}

void test_fm_open_close(void) {
    cleanup();
    file_manager_t *fm = fm_open(TEST_FILE);
    TEST_ASSERT_NOT_NULL(fm);
    TEST_ASSERT_EQUAL_UINT32(0, fm_num_pages(fm));
    fm_close(fm);
    cleanup();
}

void test_fm_allocate_page(void) {
    cleanup();
    file_manager_t *fm = fm_open(TEST_FILE);

    page_id_t pid1 = fm_allocate_page(fm);
    TEST_ASSERT_EQUAL_UINT32(0, pid1);
    TEST_ASSERT_EQUAL_UINT32(1, fm_num_pages(fm));

    page_id_t pid2 = fm_allocate_page(fm);
    TEST_ASSERT_EQUAL_UINT32(1, pid2);
    TEST_ASSERT_EQUAL_UINT32(2, fm_num_pages(fm));

    fm_close(fm);
    cleanup();
}

void test_fm_write_read_page(void) {
    cleanup();
    file_manager_t *fm = fm_open(TEST_FILE);

    page_id_t pid = fm_allocate_page(fm);

    page_t page;
    page_init(&page, pid, PAGE_TYPE_HEAP);
    page_add_tuple(&page, "test data", 10);
    TEST_ASSERT_TRUE(fm_write_page(fm, pid, &page));

    page_t read_page;
    memset(&read_page, 0, sizeof(read_page));
    TEST_ASSERT_TRUE(fm_read_page(fm, pid, &read_page));

    TEST_ASSERT_EQUAL_UINT32(pid, page_get_id(&read_page));
    TEST_ASSERT_EQUAL_INT(PAGE_TYPE_HEAP, page_get_type(&read_page));

    uint16_t len;
    const char *data = (const char *)page_get_tuple(&read_page, 0, &len);
    TEST_ASSERT_NOT_NULL(data);
    TEST_ASSERT_EQUAL_STRING("test data", data);

    fm_close(fm);
    cleanup();
}

void test_fm_persistence(void) {
    cleanup();
    file_manager_t *fm = fm_open(TEST_FILE);
    page_id_t pid = fm_allocate_page(fm);

    page_t page;
    page_init(&page, pid, PAGE_TYPE_HEAP);
    page_add_tuple(&page, "persistent", 11);
    fm_write_page(fm, pid, &page);
    fm_close(fm);

    fm = fm_open(TEST_FILE);
    TEST_ASSERT_EQUAL_UINT32(1, fm_num_pages(fm));

    page_t read_page;
    fm_read_page(fm, 0, &read_page);

    uint16_t len;
    const char *data = (const char *)page_get_tuple(&read_page, 0, &len);
    TEST_ASSERT_EQUAL_STRING("persistent", data);

    fm_close(fm);
    cleanup();
}

void test_fm_read_invalid_page(void) {
    cleanup();
    file_manager_t *fm = fm_open(TEST_FILE);

    page_t page;
    TEST_ASSERT_FALSE(fm_read_page(fm, 0, &page));

    fm_close(fm);
    cleanup();
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_fm_open_close);
    RUN_TEST(test_fm_allocate_page);
    RUN_TEST(test_fm_write_read_page);
    RUN_TEST(test_fm_persistence);
    RUN_TEST(test_fm_read_invalid_page);
    return UNITY_END();
}