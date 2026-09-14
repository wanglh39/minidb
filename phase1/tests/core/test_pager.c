#include "unity.h"
#include "pager.h"
#include "page.h"
#include <string.h>
#include <stdio.h>

void setUp(void) {}
void tearDown(void) {}

static const char *TEST_FILE = "test_pager_tmp.db";

static void cleanup(void) {
    remove(TEST_FILE);
}

void test_pager_open_close(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);
    TEST_ASSERT_NOT_NULL(pager);
    TEST_ASSERT_EQUAL_UINT32(0, pager_num_pages(pager));
    pager_close(pager);
    cleanup();
}

void test_pager_allocate_read_write(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);

    page_id_t pid = pager_allocate(pager);
    TEST_ASSERT_EQUAL_UINT32(0, pid);

    page_t page;
    page_init(&page, pid, PAGE_TYPE_HEAP);
    page_add_tuple(&page, "via pager", 10);
    TEST_ASSERT_TRUE(pager_write(pager, pid, &page));

    page_t read_page;
    memset(&read_page, 0, sizeof(read_page));
    TEST_ASSERT_TRUE(pager_read(pager, pid, &read_page));

    uint16_t len;
    const char *data = (const char *)page_get_tuple(&read_page, 0, &len);
    TEST_ASSERT_EQUAL_STRING("via pager", data);

    pager_close(pager);
    cleanup();
}

void test_pager_multiple_pages(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);

    for (int i = 0; i < 5; i++) {
        page_id_t pid = pager_allocate(pager);
        page_t page;
        page_init(&page, pid, PAGE_TYPE_HEAP);

        char buf[32];
        snprintf(buf, sizeof(buf), "page %d", i);
        page_add_tuple(&page, buf, (uint16_t)(strlen(buf) + 1));

        pager_write(pager, pid, &page);
    }

    TEST_ASSERT_EQUAL_UINT32(5, pager_num_pages(pager));

    for (int i = 0; i < 5; i++) {
        page_t page;
        pager_read(pager, (page_id_t)i, &page);
        TEST_ASSERT_EQUAL_UINT32(i, page_get_id(&page));

        uint16_t len;
        const char *data = (const char *)page_get_tuple(&page, 0, &len);

        char expected[32];
        snprintf(expected, sizeof(expected), "page %d", i);
        TEST_ASSERT_EQUAL_STRING(expected, data);
    }

    pager_close(pager);
    cleanup();
}

void test_pager_persistence(void) {
    cleanup();
    pager_t *pager = pager_open(TEST_FILE);

    page_id_t pid = pager_allocate(pager);
    page_t page;
    page_init(&page, pid, PAGE_TYPE_HEAP);
    page_add_tuple(&page, "survive reopen", 15);
    pager_write(pager, pid, &page);
    pager_close(pager);

    pager = pager_open(TEST_FILE);
    TEST_ASSERT_EQUAL_UINT32(1, pager_num_pages(pager));

    page_t read_page;
    pager_read(pager, 0, &read_page);

    uint16_t len;
    const char *data = (const char *)page_get_tuple(&read_page, 0, &len);
    TEST_ASSERT_EQUAL_STRING("survive reopen", data);

    pager_close(pager);
    cleanup();
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_pager_open_close);
    RUN_TEST(test_pager_allocate_read_write);
    RUN_TEST(test_pager_multiple_pages);
    RUN_TEST(test_pager_persistence);
    return UNITY_END();
}