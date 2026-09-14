#include "unity.h"
#include "transaction.h"
#include "lock_manager.h"
#include "mvcc.h"
#include "wal.h"
#include <stdio.h>

void setUp(void) {}
void tearDown(void) {}

static const char *WAL_FILE = "test_txn_tmp.log";
static void cleanup(void) { remove(WAL_FILE); }

/* ---------- 事务管理器 ---------- */

void test_txn_begin_commit(void) {
    cleanup();
    wal_t *wal = wal_open(WAL_FILE);
    transaction_manager_t *mgr = txn_mgr_create(wal);

    txn_id_t t1 = txn_begin(mgr);
    TEST_ASSERT_TRUE(t1 > 0);
    TEST_ASSERT_TRUE(txn_is_active(mgr, t1));

    TEST_ASSERT_TRUE(txn_commit(mgr, t1));
    TEST_ASSERT_TRUE(txn_is_committed(mgr, t1));
    TEST_ASSERT_FALSE(txn_is_active(mgr, t1));

    txn_mgr_destroy(mgr);
    wal_close(wal);
    cleanup();
}

void test_txn_abort(void) {
    cleanup();
    wal_t *wal = wal_open(WAL_FILE);
    transaction_manager_t *mgr = txn_mgr_create(wal);

    txn_id_t t1 = txn_begin(mgr);
    TEST_ASSERT_TRUE(txn_abort(mgr, t1));
    TEST_ASSERT_EQUAL_INT(TXN_ABORTED, txn_status(mgr, t1));

    txn_mgr_destroy(mgr);
    wal_close(wal);
    cleanup();
}

void test_txn_double_commit_fails(void) {
    cleanup();
    wal_t *wal = wal_open(WAL_FILE);
    transaction_manager_t *mgr = txn_mgr_create(wal);

    txn_id_t t1 = txn_begin(mgr);
    TEST_ASSERT_TRUE(txn_commit(mgr, t1));
    TEST_ASSERT_FALSE(txn_commit(mgr, t1));

    txn_mgr_destroy(mgr);
    wal_close(wal);
    cleanup();
}

void test_txn_snapshot(void) {
    cleanup();
    wal_t *wal = wal_open(WAL_FILE);
    transaction_manager_t *mgr = txn_mgr_create(wal);

    txn_id_t t1 = txn_begin(mgr);
    txn_commit(mgr, t1);

    txn_id_t t2 = txn_begin(mgr);
    txn_commit(mgr, t2);

    txn_id_t t3 = txn_begin(mgr);

    txn_snapshot_t snap;
    txn_get_snapshot(mgr, &snap);
    TEST_ASSERT_EQUAL_INT(2, snap.count);
    TEST_ASSERT_TRUE(txn_snapshot_contains(&snap, t1));
    TEST_ASSERT_TRUE(txn_snapshot_contains(&snap, t2));
    TEST_ASSERT_FALSE(txn_snapshot_contains(&snap, t3));

    txn_mgr_destroy(mgr);
    wal_close(wal);
    cleanup();
}

/* ---------- 锁管理器 ---------- */

void test_lock_shared_compatible(void) {
    lock_manager_t *lm = lm_create();
    rid_t rid = { 1, 0 };

    TEST_ASSERT_TRUE(lm_lock(lm, 1, rid, LOCK_SHARED));
    TEST_ASSERT_TRUE(lm_lock(lm, 2, rid, LOCK_SHARED));
    TEST_ASSERT_EQUAL_INT(2, lm_num_locks(lm));

    lm_destroy(lm);
}

void test_lock_exclusive_conflict(void) {
    lock_manager_t *lm = lm_create();
    rid_t rid = { 1, 0 };

    TEST_ASSERT_TRUE(lm_lock(lm, 1, rid, LOCK_SHARED));
    TEST_ASSERT_FALSE(lm_lock(lm, 2, rid, LOCK_EXCLUSIVE));

    lm_destroy(lm);
}

void test_lock_x_x_conflict(void) {
    lock_manager_t *lm = lm_create();
    rid_t rid = { 1, 0 };

    TEST_ASSERT_TRUE(lm_lock(lm, 1, rid, LOCK_EXCLUSIVE));
    TEST_ASSERT_FALSE(lm_lock(lm, 2, rid, LOCK_EXCLUSIVE));
    TEST_ASSERT_FALSE(lm_lock(lm, 2, rid, LOCK_SHARED));

    lm_destroy(lm);
}

void test_lock_same_txn_upgrade(void) {
    lock_manager_t *lm = lm_create();
    rid_t rid = { 1, 0 };

    TEST_ASSERT_TRUE(lm_lock(lm, 1, rid, LOCK_SHARED));
    TEST_ASSERT_TRUE(lm_lock(lm, 1, rid, LOCK_EXCLUSIVE));
    TEST_ASSERT_EQUAL_INT(1, lm_num_locks(lm));

    lm_destroy(lm);
}

void test_lock_unlock(void) {
    lock_manager_t *lm = lm_create();
    rid_t rid = { 1, 0 };

    lm_lock(lm, 1, rid, LOCK_EXCLUSIVE);
    TEST_ASSERT_EQUAL_INT(1, lm_num_locks(lm));

    lm_unlock(lm, 1, rid);
    TEST_ASSERT_EQUAL_INT(0, lm_num_locks(lm));

    TEST_ASSERT_TRUE(lm_lock(lm, 2, rid, LOCK_EXCLUSIVE));

    lm_destroy(lm);
}

void test_lock_unlock_all(void) {
    lock_manager_t *lm = lm_create();
    rid_t r1 = { 1, 0 }, r2 = { 1, 1 }, r3 = { 2, 0 };

    lm_lock(lm, 1, r1, LOCK_EXCLUSIVE);
    lm_lock(lm, 1, r2, LOCK_SHARED);
    lm_lock(lm, 1, r3, LOCK_EXCLUSIVE);
    TEST_ASSERT_EQUAL_INT(3, lm_num_locks(lm));

    lm_unlock_all(lm, 1);
    TEST_ASSERT_EQUAL_INT(0, lm_num_locks(lm));

    lm_destroy(lm);
}

/* ---------- MVCC ---------- */

void test_mvcc_visible_own_version(void) {
    txn_snapshot_t snap = { .count = 0 };
    mvcc_header_t h;
    mvcc_init(&h, 5);

    TEST_ASSERT_TRUE(mvcc_visible(&h, &snap, 5));
}

void test_mvcc_invisible_uncommitted_creator(void) {
    txn_snapshot_t snap = { .count = 0 };
    mvcc_header_t h;
    mvcc_init(&h, 3);

    TEST_ASSERT_FALSE(mvcc_visible(&h, &snap, 5));
}

void test_mvcc_visible_committed_creator(void) {
    txn_snapshot_t snap = { .count = 1, .txn_ids = { 3 } };
    mvcc_header_t h;
    mvcc_init(&h, 3);

    TEST_ASSERT_TRUE(mvcc_visible(&h, &snap, 5));
}

void test_mvcc_invisible_deleted_by_committed(void) {
    txn_snapshot_t snap = { .count = 2, .txn_ids = { 3, 7 } };
    mvcc_header_t h = { .xmin = 3, .xmax = 7 };

    TEST_ASSERT_FALSE(mvcc_visible(&h, &snap, 5));
}

void test_mvcc_visible_deleted_by_uncommitted(void) {
    txn_snapshot_t snap = { .count = 1, .txn_ids = { 3 } };
    mvcc_header_t h = { .xmin = 3, .xmax = 7 };

    TEST_ASSERT_TRUE(mvcc_visible(&h, &snap, 5));
}

void test_mvcc_deleted_check(void) {
    txn_snapshot_t snap = { .count = 1, .txn_ids = { 3 } };
    mvcc_header_t h1 = { .xmin = 3, .xmax = MVCC_NOT_DELETED };
    mvcc_header_t h2 = { .xmin = 3, .xmax = 7 };

    TEST_ASSERT_FALSE(mvcc_deleted(&h1, &snap, 5));
    TEST_ASSERT_FALSE(mvcc_deleted(&h2, &snap, 5));

    txn_snapshot_t snap2 = { .count = 2, .txn_ids = { 3, 7 } };
    TEST_ASSERT_TRUE(mvcc_deleted(&h2, &snap2, 5));
}

void test_mvcc_read_does_not_block_write(void) {
    cleanup();
    wal_t *wal = wal_open(WAL_FILE);
    transaction_manager_t *mgr = txn_mgr_create(wal);

    txn_id_t t1 = txn_begin(mgr);
    txn_commit(mgr, t1);

    txn_id_t reader = txn_begin(mgr);
    txn_snapshot_t snap;
    txn_get_snapshot(mgr, &snap);

    txn_id_t writer = txn_begin(mgr);

    mvcc_header_t old_version;
    mvcc_init(&old_version, t1);

    TEST_ASSERT_TRUE(mvcc_visible(&old_version, &snap, reader));

    old_version.xmax = writer;
    mvcc_header_t new_version;
    mvcc_init(&new_version, writer);

    TEST_ASSERT_TRUE(mvcc_visible(&old_version, &snap, reader));
    TEST_ASSERT_FALSE(mvcc_visible(&new_version, &snap, reader));

    txn_commit(mgr, writer);
    txn_snapshot_t snap2;
    txn_get_snapshot(mgr, &snap2);
    TEST_ASSERT_FALSE(mvcc_visible(&old_version, &snap2, reader));
    TEST_ASSERT_TRUE(mvcc_visible(&new_version, &snap2, reader));

    txn_abort(mgr, reader);
    txn_mgr_destroy(mgr);
    wal_close(wal);
    cleanup();
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_txn_begin_commit);
    RUN_TEST(test_txn_abort);
    RUN_TEST(test_txn_double_commit_fails);
    RUN_TEST(test_txn_snapshot);
    RUN_TEST(test_lock_shared_compatible);
    RUN_TEST(test_lock_exclusive_conflict);
    RUN_TEST(test_lock_x_x_conflict);
    RUN_TEST(test_lock_same_txn_upgrade);
    RUN_TEST(test_lock_unlock);
    RUN_TEST(test_lock_unlock_all);
    RUN_TEST(test_mvcc_visible_own_version);
    RUN_TEST(test_mvcc_invisible_uncommitted_creator);
    RUN_TEST(test_mvcc_visible_committed_creator);
    RUN_TEST(test_mvcc_invisible_deleted_by_committed);
    RUN_TEST(test_mvcc_visible_deleted_by_uncommitted);
    RUN_TEST(test_mvcc_deleted_check);
    RUN_TEST(test_mvcc_read_does_not_block_write);
    return UNITY_END();
}