#ifndef MINIDB_MVCC_H
#define MINIDB_MVCC_H

#include "transaction.h"
#include <stdint.h>
#include <stdbool.h>

typedef struct {
    txn_id_t xmin;
    txn_id_t xmax;
} mvcc_header_t;

#define MVCC_HEADER_SIZE 8
#define MVCC_NOT_DELETED ((txn_id_t)0)

bool mvcc_visible(const mvcc_header_t *h,
                  const txn_snapshot_t *snapshot,
                  txn_id_t current_txn);

bool mvcc_deleted(const mvcc_header_t *h,
                  const txn_snapshot_t *snapshot,
                  txn_id_t current_txn);

void mvcc_init(mvcc_header_t *h, txn_id_t xmin);

#endif