#ifndef MINIDB_LOCK_MANAGER_H
#define MINIDB_LOCK_MANAGER_H

#include "btree_node.h"
#include "transaction.h"
#include <stdbool.h>

typedef enum {
    LOCK_SHARED = 0,
    LOCK_EXCLUSIVE = 1,
} lock_mode_t;

typedef struct lock_manager lock_manager_t;

lock_manager_t *lm_create(void);
void lm_destroy(lock_manager_t *lm);

bool lm_lock(lock_manager_t *lm, txn_id_t txn, rid_t rid, lock_mode_t mode);
bool lm_unlock(lock_manager_t *lm, txn_id_t txn, rid_t rid);
void lm_unlock_all(lock_manager_t *lm, txn_id_t txn);

bool lm_has_deadlock(lock_manager_t *lm);
int  lm_num_locks(lock_manager_t *lm);

#endif