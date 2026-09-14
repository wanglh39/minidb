#include "recovery.h"
#include <string.h>

#define MAX_TXN_IDS 1024

void recovery_redo(buffer_pool_t *bp, wal_t *wal) {
    bool committed[MAX_TXN_IDS] = {false};

    wal_iter_t *it = wal_iter_open(wal);
    log_record_t rec;
    while (wal_iter_next(it, &rec)) {
        if (rec.type == LOG_COMMIT && rec.txn_id < MAX_TXN_IDS) {
            committed[rec.txn_id] = true;
        }
    }
    wal_iter_close(it);

    it = wal_iter_open(wal);
    while (wal_iter_next(it, &rec)) {
        if (rec.type == LOG_UPDATE &&
            rec.txn_id < MAX_TXN_IDS &&
            committed[rec.txn_id]) {
            page_t *page = bp_fetch_page(bp, rec.page_id);
            if (page) {
                memcpy(page->data + rec.offset, rec.new_data, rec.length);
                bp_unpin_page(bp, rec.page_id, true);
            }
        }
    }
    wal_iter_close(it);
}