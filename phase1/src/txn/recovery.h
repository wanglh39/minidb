#ifndef MINIDB_RECOVERY_H
#define MINIDB_RECOVERY_H

#include "buffer_pool.h"
#include "wal.h"

void recovery_redo(buffer_pool_t *bp, wal_t *wal);

#endif