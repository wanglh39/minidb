#include "replacer.h"
#include <stdlib.h>
#include <string.h>

#define LRU_K 2

struct replacer {
    replacer_type_t type;
    int capacity;
    bool *in_set;
    int size;

    frame_id_t *lru_order;

    bool *clock_ref;
    int clock_hand;

    int *lruk_count;
    uint64_t *lruk_first;
    uint64_t *lruk_last;
    uint64_t lruk_time;
};

replacer_t *replacer_create(replacer_type_t type, int capacity) {
    replacer_t *r = calloc(1, sizeof(replacer_t));
    if (!r) return NULL;

    r->type = type;
    r->capacity = capacity;
    r->in_set = calloc(capacity, sizeof(bool));
    r->size = 0;

    switch (type) {
        case REPLACER_LRU:
            r->lru_order = malloc(capacity * sizeof(frame_id_t));
            break;
        case REPLACER_CLOCK:
            r->clock_ref = calloc(capacity, sizeof(bool));
            r->clock_hand = 0;
            break;
        case REPLACER_LRU_K:
            r->lruk_count = calloc(capacity, sizeof(int));
            r->lruk_first = calloc(capacity, sizeof(uint64_t));
            r->lruk_last = calloc(capacity, sizeof(uint64_t));
            r->lruk_time = 0;
            break;
    }
    return r;
}

void replacer_destroy(replacer_t *r) {
    if (!r) return;
    free(r->in_set);
    free(r->lru_order);
    free(r->clock_ref);
    free(r->lruk_count);
    free(r->lruk_first);
    free(r->lruk_last);
    free(r);
}

/* ---------- LRU ---------- */

static void lru_remove(replacer_t *r, frame_id_t fid) {
    for (int i = 0; i < r->size; i++) {
        if (r->lru_order[i] == fid) {
            memmove(&r->lru_order[i], &r->lru_order[i + 1],
                    (r->size - i - 1) * sizeof(frame_id_t));
            r->size--;
            return;
        }
    }
}

static frame_id_t lru_victim(replacer_t *r) {
    if (r->size == 0) return INVALID_FRAME_ID;
    frame_id_t victim = r->lru_order[0];
    lru_remove(r, victim);
    r->in_set[victim] = false;
    return victim;
}

/* ---------- Clock ---------- */

static frame_id_t clock_victim(replacer_t *r) {
    if (r->size == 0) return INVALID_FRAME_ID;

    for (int attempts = 0; attempts < r->capacity * 2; attempts++) {
        frame_id_t fid = r->clock_hand;
        r->clock_hand = (r->clock_hand + 1) % r->capacity;

        if (!r->in_set[fid]) continue;

        if (r->clock_ref[fid]) {
            r->clock_ref[fid] = false;
        } else {
            r->in_set[fid] = false;
            r->size--;
            return fid;
        }
    }
    return INVALID_FRAME_ID;
}

/* ---------- LRU-K (K=2) ---------- */

static frame_id_t lruk_victim(replacer_t *r) {
    if (r->size == 0) return INVALID_FRAME_ID;

    frame_id_t victim = INVALID_FRAME_ID;
    uint64_t best_key = UINT64_MAX;

    for (frame_id_t fid = 0; fid < r->capacity; fid++) {
        if (!r->in_set[fid]) continue;

        uint64_t key;
        if (r->lruk_count[fid] < LRU_K) {
            key = r->lruk_first[fid];
        } else {
            key = r->lruk_first[fid];
        }

        if (key < best_key) {
            best_key = key;
            victim = fid;
        }
    }

    if (victim != INVALID_FRAME_ID) {
        r->in_set[victim] = false;
        r->size--;
    }
    return victim;
}

/* ---------- 公共接口 ---------- */

void replacer_pin(replacer_t *r, frame_id_t fid) {
    if (fid < 0 || fid >= r->capacity) return;
    if (!r->in_set[fid]) return;

    if (r->type == REPLACER_LRU) {
        lru_remove(r, fid);
    }
    r->in_set[fid] = false;
    r->size--;
}

void replacer_unpin(replacer_t *r, frame_id_t fid) {
    if (fid < 0 || fid >= r->capacity) return;
    if (r->in_set[fid]) return;

    r->in_set[fid] = true;
    r->size++;

    switch (r->type) {
        case REPLACER_LRU:
            r->lru_order[r->size - 1] = fid;
            break;
        case REPLACER_CLOCK:
            r->clock_ref[fid] = true;
            break;
        case REPLACER_LRU_K:
            r->lruk_count[fid]++;
            if (r->lruk_count[fid] == 1) {
                r->lruk_first[fid] = r->lruk_time;
                r->lruk_last[fid] = r->lruk_time;
            } else {
                r->lruk_first[fid] = r->lruk_last[fid];
                r->lruk_last[fid] = r->lruk_time;
            }
            r->lruk_time++;
            break;
    }
}

frame_id_t replacer_victim(replacer_t *r) {
    switch (r->type) {
        case REPLACER_LRU:    return lru_victim(r);
        case REPLACER_CLOCK:  return clock_victim(r);
        case REPLACER_LRU_K:  return lruk_victim(r);
        default:              return INVALID_FRAME_ID;
    }
}

int replacer_size(replacer_t *r) {
    return r->size;
}