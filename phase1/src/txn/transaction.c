#include "transaction.h"
#include <stdlib.h>
#include <string.h>

struct transaction_manager {
    wal_t *wal;
    txn_id_t next_txn_id;
    txn_info_t txns[MAX_TXNS];
};

transaction_manager_t *txn_mgr_create(wal_t *wal) {
    transaction_manager_t *mgr = calloc(1, sizeof(transaction_manager_t));
    mgr->wal = wal;
    mgr->next_txn_id = 1;
    return mgr;
}

void txn_mgr_destroy(transaction_manager_t *mgr) {
    free(mgr);
}

txn_id_t txn_begin(transaction_manager_t *mgr) {
    txn_id_t id = mgr->next_txn_id++;
    lsn_t lsn = wal_begin(mgr->wal, id);

    if (id < MAX_TXNS) {
        mgr->txns[id].id = id;
        mgr->txns[id].status = TXN_ACTIVE;
        mgr->txns[id].begin_lsn = lsn;
        mgr->txns[id].commit_lsn = INVALID_LSN;
    }
    return id;
}

bool txn_commit(transaction_manager_t *mgr, txn_id_t txn_id) {
    if (txn_id == INVALID_TXN_ID || txn_id >= MAX_TXNS) return false;
    if (mgr->txns[txn_id].status != TXN_ACTIVE) return false;

    lsn_t lsn = wal_commit(mgr->wal, txn_id);
    mgr->txns[txn_id].status = TXN_COMMITTED;
    mgr->txns[txn_id].commit_lsn = lsn;
    return true;
}

bool txn_abort(transaction_manager_t *mgr, txn_id_t txn_id) {
    if (txn_id == INVALID_TXN_ID || txn_id >= MAX_TXNS) return false;
    if (mgr->txns[txn_id].status != TXN_ACTIVE) return false;

    wal_abort(mgr->wal, txn_id);
    mgr->txns[txn_id].status = TXN_ABORTED;
    return true;
}

txn_status_t txn_status(transaction_manager_t *mgr, txn_id_t txn_id) {
    if (txn_id >= MAX_TXNS) return TXN_ABORTED;
    return mgr->txns[txn_id].status;
}

bool txn_is_active(transaction_manager_t *mgr, txn_id_t txn_id) {
    return txn_status(mgr, txn_id) == TXN_ACTIVE;
}

bool txn_is_committed(transaction_manager_t *mgr, txn_id_t txn_id) {
    return txn_status(mgr, txn_id) == TXN_COMMITTED;
}

void txn_get_snapshot(transaction_manager_t *mgr, txn_snapshot_t *snap) {
    snap->count = 0;
    for (txn_id_t i = 1; i < mgr->next_txn_id && snap->count < SNAPSHOT_CAPACITY; i++) {
        if (i < MAX_TXNS && mgr->txns[i].status == TXN_COMMITTED) {
            snap->txn_ids[snap->count++] = i;
        }
    }
}

bool txn_snapshot_contains(const txn_snapshot_t *snap, txn_id_t txn_id) {
    for (int i = 0; i < snap->count; i++) {
        if (snap->txn_ids[i] == txn_id) return true;
    }
    return false;
}