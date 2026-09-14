#ifndef MINIDB_WAL_H
#define MINIDB_WAL_H

#include "page.h"
#include <stdint.h>
#include <stdbool.h>

typedef uint64_t lsn_t;
typedef uint32_t txn_id_t;

#define INVALID_LSN ((lsn_t)0)

typedef enum {
    LOG_BEGIN = 0,
    LOG_COMMIT = 1,
    LOG_UPDATE = 2,
    LOG_ABORT = 3,
    LOG_CHECKPOINT = 4,
} log_type_t;

typedef struct {
    lsn_t lsn;
    txn_id_t txn_id;
    log_type_t type;
    page_id_t page_id;
    uint16_t offset;
    uint16_t length;
    const uint8_t *old_data;
    const uint8_t *new_data;
} log_record_t;

typedef struct wal wal_t;

wal_t *wal_open(const char *path);
void   wal_close(wal_t *wal);

lsn_t  wal_begin(wal_t *wal, txn_id_t txn_id);
lsn_t  wal_commit(wal_t *wal, txn_id_t txn_id);
lsn_t  wal_abort(wal_t *wal, txn_id_t txn_id);
lsn_t  wal_update(wal_t *wal, txn_id_t txn_id,
                  page_id_t pid, uint16_t offset, uint16_t len,
                  const void *old_data, const void *new_data);
lsn_t  wal_checkpoint(wal_t *wal, lsn_t max_lsn);

void   wal_flush(wal_t *wal);
lsn_t  wal_last_lsn(wal_t *wal);

typedef struct wal_iter wal_iter_t;
wal_iter_t *wal_iter_open(wal_t *wal);
bool        wal_iter_next(wal_iter_t *it, log_record_t *rec);
void        wal_iter_close(wal_iter_t *it);

#endif