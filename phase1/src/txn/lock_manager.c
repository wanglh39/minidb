#include "lock_manager.h"
#include <stdlib.h>
#include <string.h>

#define LM_TABLE_SIZE 2048

typedef struct {
    rid_t rid;
    txn_id_t txn;
    lock_mode_t mode;
    bool used;
} lock_entry_t;

struct lock_manager {
    lock_entry_t table[LM_TABLE_SIZE];
    int num_locks;
};

static uint32_t rid_hash(rid_t rid) {
    uint32_t h = rid.page_id * 31u + rid.slot_id;
    return h % LM_TABLE_SIZE;
}

static lock_entry_t *find_lock(lock_manager_t *lm, rid_t rid, txn_id_t txn) {
    uint32_t h = rid_hash(rid);
    for (int i = 0; i < LM_TABLE_SIZE; i++) {
        int idx = (h + i) % LM_TABLE_SIZE;
        if (!lm->table[idx].used) return NULL;
        if (lm->table[idx].rid.page_id == rid.page_id &&
            lm->table[idx].rid.slot_id == rid.slot_id &&
            lm->table[idx].txn == txn) {
            return &lm->table[idx];
        }
    }
    return NULL;
}

static bool has_conflict(lock_manager_t *lm, rid_t rid, txn_id_t txn, lock_mode_t mode) {
    uint32_t h = rid_hash(rid);
    for (int i = 0; i < LM_TABLE_SIZE; i++) {
        int idx = (h + i) % LM_TABLE_SIZE;
        if (!lm->table[idx].used) return false;

        lock_entry_t *e = &lm->table[idx];
        if (e->rid.page_id != rid.page_id || e->rid.slot_id != rid.slot_id) continue;
        if (e->txn == txn) continue;

        if (e->mode == LOCK_EXCLUSIVE || mode == LOCK_EXCLUSIVE) {
            return true;
        }
    }
    return false;
}

lock_manager_t *lm_create(void) {
    lock_manager_t *lm = calloc(1, sizeof(lock_manager_t));
    return lm;
}

void lm_destroy(lock_manager_t *lm) {
    free(lm);
}

bool lm_lock(lock_manager_t *lm, txn_id_t txn, rid_t rid, lock_mode_t mode) {
    lock_entry_t *existing = find_lock(lm, rid, txn);
    if (existing) {
        if (mode == LOCK_EXCLUSIVE) existing->mode = LOCK_EXCLUSIVE;
        return true;
    }

    if (has_conflict(lm, rid, txn, mode)) {
        return false;
    }

    uint32_t h = rid_hash(rid);
    for (int i = 0; i < LM_TABLE_SIZE; i++) {
        int idx = (h + i) % LM_TABLE_SIZE;
        if (!lm->table[idx].used) {
            lm->table[idx].rid = rid;
            lm->table[idx].txn = txn;
            lm->table[idx].mode = mode;
            lm->table[idx].used = true;
            lm->num_locks++;
            return true;
        }
    }
    return false;
}

bool lm_unlock(lock_manager_t *lm, txn_id_t txn, rid_t rid) {
    lock_entry_t *e = find_lock(lm, rid, txn);
    if (!e) return false;
    e->used = false;
    lm->num_locks--;
    return true;
}

void lm_unlock_all(lock_manager_t *lm, txn_id_t txn) {
    for (int i = 0; i < LM_TABLE_SIZE; i++) {
        if (lm->table[i].used && lm->table[i].txn == txn) {
            lm->table[i].used = false;
            lm->num_locks--;
        }
    }
}

bool lm_has_deadlock(lock_manager_t *lm) {
    (void)lm;
    return false;
}

int lm_num_locks(lock_manager_t *lm) {
    return lm->num_locks;
}