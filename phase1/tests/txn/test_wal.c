#include "unity.h"
#include "wal.h"
#include "recovery.h"
#include "buffer_pool.h"
#include "pager.h"
#include "page.h"
#include <stdio.h>
#include <string.h>

void setUp(void) {}
void tearDown(void) {}

static const char *WAL_FILE = "test_wal_tmp.log";
static const char *DB_FILE  = "test_wal_tmp.db";

static void cleanup(void) {
    remove(WAL_FILE);
    remove(DB_FILE);
}

void test_wal_open_close(void) {
    cleanup();
    wal_t *wal = wal_open(WAL_FILE);
    TEST_ASSERT_NOT_NULL(wal);
    wal_close(wal);
    cleanup();
}

void test_wal_begin_commit(void) {
    cleanup();
    wal_t *wal = wal_open(WAL_FILE);

    lsn_t lsn1 = wal_begin(wal, 1);
    lsn_t lsn2 = wal_commit(wal, 1);

    TEST_ASSERT_TRUE(lsn1 > 0);
    TEST_ASSERT_TRUE(lsn2 > lsn1);

    wal_close(wal);

    wal = wal_open(WAL_FILE);
    wal_iter_t *it = wal_iter_open(wal);
    log_record_t rec;

    TEST_ASSERT_TRUE(wal_iter_next(it, &rec));
    TEST_ASSERT_EQUAL_INT(LOG_BEGIN, rec.type);
    TEST_ASSERT_EQUAL_UINT32(1, rec.txn_id);

    TEST_ASSERT_TRUE(wal_iter_next(it, &rec));
    TEST_ASSERT_EQUAL_INT(LOG_COMMIT, rec.type);
    TEST_ASSERT_EQUAL_UINT32(1, rec.txn_id);

    TEST_ASSERT_FALSE(wal_iter_next(it, &rec));

    wal_iter_close(it);
    wal_close(wal);
    cleanup();
}

void test_wal_update_record(void) {
    cleanup();
    wal_t *wal = wal_open(WAL_FILE);

    wal_begin(wal, 1);

    uint8_t old_data[4] = { 0, 0, 0, 0 };
    uint8_t new_data[4] = { 42, 0, 0, 0 };
    wal_update(wal, 1, 0, 100, 4, old_data, new_data);

    wal_commit(wal, 1);
    wal_close(wal);

    wal = wal_open(WAL_FILE);
    wal_iter_t *it = wal_iter_open(wal);
    log_record_t rec;

    wal_iter_next(it, &rec);
    TEST_ASSERT_EQUAL_INT(LOG_BEGIN, rec.type);

    wal_iter_next(it, &rec);
    TEST_ASSERT_EQUAL_INT(LOG_UPDATE, rec.type);
    TEST_ASSERT_EQUAL_UINT32(1, rec.txn_id);
    TEST_ASSERT_EQUAL_UINT32(0, rec.page_id);
    TEST_ASSERT_EQUAL_UINT16(100, rec.offset);
    TEST_ASSERT_EQUAL_UINT16(4, rec.length);
    TEST_ASSERT_EQUAL_UINT8(42, rec.new_data[0]);

    wal_iter_next(it, &rec);
    TEST_ASSERT_EQUAL_INT(LOG_COMMIT, rec.type);

    wal_iter_close(it);
    wal_close(wal);
    cleanup();
}

void test_wal_multiple_transactions(void) {
    cleanup();
    wal_t *wal = wal_open(WAL_FILE);

    wal_begin(wal, 1);
    wal_commit(wal, 1);

    wal_begin(wal, 2);
    wal_abort(wal, 2);

    wal_begin(wal, 3);
    wal_commit(wal, 3);

    wal_close(wal);

    wal = wal_open(WAL_FILE);
    wal_iter_t *it = wal_iter_open(wal);
    log_record_t rec;
    int begins = 0, commits = 0, aborts = 0;

    while (wal_iter_next(it, &rec)) {
        switch (rec.type) {
            case LOG_BEGIN:  begins++; break;
            case LOG_COMMIT: commits++; break;
            case LOG_ABORT:  aborts++; break;
            default: break;
        }
    }

    TEST_ASSERT_EQUAL_INT(3, begins);
    TEST_ASSERT_EQUAL_INT(2, commits);
    TEST_ASSERT_EQUAL_INT(1, aborts);

    wal_iter_close(it);
    wal_close(wal);
    cleanup();
}

void test_recovery_redo(void) {
    cleanup();
    pager_t *pager = pager_open(DB_FILE);
    page_id_t pid = pager_allocate(pager);

    page_t raw;
    page_init(&raw, pid, PAGE_TYPE_HEAP);
    pager_write(pager, pid, &raw);
    pager_close(pager);

    wal_t *wal = wal_open(WAL_FILE);
    wal_begin(wal, 1);

    uint8_t old_data[8] = {0};
    uint8_t new_data[8] = { 1, 2, 3, 4, 5, 6, 7, 8 };
    wal_update(wal, 1, pid, 20, 8, old_data, new_data);

    wal_commit(wal, 1);
    wal_close(wal);

    pager = pager_open(DB_FILE);
    buffer_pool_t *bp = bp_create(pager, 16, REPLACER_LRU);

    wal = wal_open(WAL_FILE);
    recovery_redo(bp, wal);
    wal_close(wal);

    bp_flush_all(bp);
    bp_destroy(bp);
    pager_close(pager);

    pager = pager_open(DB_FILE);
    page_t check;
    pager_read(pager, pid, &check);
    TEST_ASSERT_EQUAL_UINT8(1, check.data[20]);
    TEST_ASSERT_EQUAL_UINT8(8, check.data[27]);
    pager_close(pager);
    cleanup();
}

void test_recovery_skips_uncommitted(void) {
    cleanup();
    pager_t *pager = pager_open(DB_FILE);
    page_id_t pid = pager_allocate(pager);
    page_t raw;
    page_init(&raw, pid, PAGE_TYPE_HEAP);
    pager_write(pager, pid, &raw);
    pager_close(pager);

    wal_t *wal = wal_open(WAL_FILE);

    wal_begin(wal, 1);
    uint8_t old_d[4] = {0};
    uint8_t new_d[4] = { 99, 99, 99, 99 };
    wal_update(wal, 1, pid, 20, 4, old_d, new_d);
    wal_commit(wal, 1);

    wal_begin(wal, 2);
    uint8_t new_d2[4] = { 11, 22, 33, 44 };
    wal_update(wal, 2, pid, 20, 4, old_d, new_d2);

    wal_close(wal);

    pager = pager_open(DB_FILE);
    buffer_pool_t *bp = bp_create(pager, 16, REPLACER_LRU);
    wal = wal_open(WAL_FILE);
    recovery_redo(bp, wal);
    wal_close(wal);
    bp_flush_all(bp);
    bp_destroy(bp);
    pager_close(pager);

    pager = pager_open(DB_FILE);
    page_t check;
    pager_read(pager, pid, &check);
    TEST_ASSERT_EQUAL_UINT8(99, check.data[20]);
    pager_close(pager);
    cleanup();
}

void test_wal_checkpoint(void) {
    cleanup();
    wal_t *wal = wal_open(WAL_FILE);

    wal_begin(wal, 1);
    wal_commit(wal, 1);

    lsn_t cp_lsn = wal_checkpoint(wal, 100);
    TEST_ASSERT_TRUE(cp_lsn > 0);

    wal_close(wal);

    wal = wal_open(WAL_FILE);
    wal_iter_t *it = wal_iter_open(wal);
    log_record_t rec;

    wal_iter_next(it, &rec);
    wal_iter_next(it, &rec);
    TEST_ASSERT_TRUE(wal_iter_next(it, &rec));
    TEST_ASSERT_EQUAL_INT(LOG_CHECKPOINT, rec.type);

    wal_iter_close(it);
    wal_close(wal);
    cleanup();
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_wal_open_close);
    RUN_TEST(test_wal_begin_commit);
    RUN_TEST(test_wal_update_record);
    RUN_TEST(test_wal_multiple_transactions);
    RUN_TEST(test_recovery_redo);
    RUN_TEST(test_recovery_skips_uncommitted);
    RUN_TEST(test_wal_checkpoint);
    return UNITY_END();
}