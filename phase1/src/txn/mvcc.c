#include "mvcc.h"

void mvcc_init(mvcc_header_t *h, txn_id_t xmin) {
    h->xmin = xmin;
    h->xmax = MVCC_NOT_DELETED;
}

bool mvcc_visible(const mvcc_header_t *h,
                  const txn_snapshot_t *snapshot,
                  txn_id_t current_txn) {
    if (h->xmin == current_txn) {
        if (h->xmax == current_txn) return false;
        if (h->xmax != MVCC_NOT_DELETED && txn_snapshot_contains(snapshot, h->xmax)) {
            return false;
        }
        return true;
    }

    if (!txn_snapshot_contains(snapshot, h->xmin)) {
        return false;
    }

    if (h->xmax != MVCC_NOT_DELETED) {
        if (h->xmax == current_txn) return false;
        if (txn_snapshot_contains(snapshot, h->xmax)) return false;
    }

    return true;
}

bool mvcc_deleted(const mvcc_header_t *h,
                  const txn_snapshot_t *snapshot,
                  txn_id_t current_txn) {
    if (h->xmax == MVCC_NOT_DELETED) return false;
    if (h->xmax == current_txn) return true;
    if (txn_snapshot_contains(snapshot, h->xmax)) return true;
    return false;
}