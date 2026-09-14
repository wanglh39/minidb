#ifndef MINIDB_TRANSACTION_H
#define MINIDB_TRANSACTION_H

#include "wal.h"
#include <stdint.h>
#include <stdbool.h>

typedef uint32_t txn_id_t;
#define INVALID_TXN_ID ((txn_id_t)0)

typedef enum {
    TXN_ACTIVE = 0,
    TXN_COMMITTED = 1,
    TXN_ABORTED = 2,
} txn_status_t;

typedef struct {
    txn_id_t id;
    txn_status_t status;
    lsn_t begin_lsn;
    lsn_t commit_lsn;
} txn_info_t;

#define MAX_TXNS 1024
#define SNAPSHOT_CAPACITY 256

typedef struct {
    txn_id_t txn_ids[SNAPSHOT_CAPACITY];
    int count;
} txn_snapshot_t;

typedef struct transaction_manager transaction_manager_t;

transaction_manager_t *txn_mgr_create(wal_t *wal);
void txn_mgr_destroy(transaction_manager_t *mgr);

txn_id_t txn_begin(transaction_manager_t *mgr);
bool     txn_commit(transaction_manager_t *mgr, txn_id_t txn_id);
bool     txn_abort(transaction_manager_t *mgr, txn_id_t txn_id);

txn_status_t txn_status(transaction_manager_t *mgr, txn_id_t txn_id);
bool txn_is_active(transaction_manager_t *mgr, txn_id_t txn_id);
bool txn_is_committed(transaction_manager_t *mgr, txn_id_t txn_id);

void txn_get_snapshot(transaction_manager_t *mgr, txn_snapshot_t *snap);
bool txn_snapshot_contains(const txn_snapshot_t *snap, txn_id_t txn_id);

#endif