#include "buffer_pool.h"
#include <stdlib.h>
#include <string.h>

typedef struct {
    page_id_t pid;
    frame_id_t fid;
    uint8_t state;  /* 0=空 1=占用 2=tombstone */
} pt_entry_t;

struct buffer_pool {
    pager_t *pager;
    int pool_size;

    page_t    *frames;
    int       *pin_count;
    bool      *dirty;
    page_id_t *frame_pid;

    pt_entry_t *pt;
    int pt_cap;

    replacer_t *repl;

    frame_id_t *free_list;
    int free_count;

    bp_stats_t stats;
};

/* ---------- page_table (开放寻址哈希) ---------- */

static frame_id_t pt_lookup(buffer_pool_t *bp, page_id_t pid) {
    int start = (int)(pid % (page_id_t)bp->pt_cap);
    for (int i = 0; i < bp->pt_cap; i++) {
        int j = (start + i) % bp->pt_cap;
        if (bp->pt[j].state == 0) return INVALID_FRAME_ID;
        if (bp->pt[j].state == 1 && bp->pt[j].pid == pid) return bp->pt[j].fid;
    }
    return INVALID_FRAME_ID;
}

static void pt_insert(buffer_pool_t *bp, page_id_t pid, frame_id_t fid) {
    int start = (int)(pid % (page_id_t)bp->pt_cap);
    int tomb = -1;
    for (int i = 0; i < bp->pt_cap; i++) {
        int j = (start + i) % bp->pt_cap;
        if (bp->pt[j].state == 0) {
            int at = (tomb >= 0) ? tomb : j;
            bp->pt[at].pid = pid;
            bp->pt[at].fid = fid;
            bp->pt[at].state = 1;
            return;
        }
        if (bp->pt[j].state == 2 && tomb < 0) tomb = j;
        if (bp->pt[j].state == 1 && bp->pt[j].pid == pid) {
            bp->pt[j].fid = fid;
            return;
        }
    }
    if (tomb >= 0) {
        bp->pt[tomb].pid = pid;
        bp->pt[tomb].fid = fid;
        bp->pt[tomb].state = 1;
    }
}

static void pt_delete(buffer_pool_t *bp, page_id_t pid) {
    int start = (int)(pid % (page_id_t)bp->pt_cap);
    for (int i = 0; i < bp->pt_cap; i++) {
        int j = (start + i) % bp->pt_cap;
        if (bp->pt[j].state == 0) return;
        if (bp->pt[j].state == 1 && bp->pt[j].pid == pid) {
            bp->pt[j].state = 2;
            return;
        }
    }
}

/* ---------- 内部辅助 ---------- */

static frame_id_t bp_evict(buffer_pool_t *bp) {
    frame_id_t fid = replacer_victim(bp->repl);
    if (fid == INVALID_FRAME_ID) return INVALID_FRAME_ID;

    bp->stats.evictions++;

    if (bp->dirty[fid]) {
        pager_write(bp->pager, bp->frame_pid[fid], &bp->frames[fid]);
        bp->dirty[fid] = false;
        bp->stats.dirty_writes++;
    }

    pt_delete(bp, bp->frame_pid[fid]);
    bp->frame_pid[fid] = INVALID_PAGE_ID;
    return fid;
}

static frame_id_t bp_get_frame(buffer_pool_t *bp) {
    if (bp->free_count > 0) {
        return bp->free_list[--bp->free_count];
    }
    return bp_evict(bp);
}

/* ---------- 公共接口 ---------- */

buffer_pool_t *bp_create(pager_t *pager, int pool_size, replacer_type_t rtype) {
    buffer_pool_t *bp = calloc(1, sizeof(buffer_pool_t));
    if (!bp) return NULL;

    bp->pager = pager;
    bp->pool_size = pool_size;
    bp->pt_cap = pool_size * 2;

    bp->frames    = malloc(pool_size * sizeof(page_t));
    bp->pin_count = calloc(pool_size, sizeof(int));
    bp->dirty     = calloc(pool_size, sizeof(bool));
    bp->frame_pid = malloc(pool_size * sizeof(page_id_t));
    bp->pt        = calloc(bp->pt_cap, sizeof(pt_entry_t));
    bp->free_list = malloc(pool_size * sizeof(frame_id_t));

    for (int i = 0; i < pool_size; i++) {
        bp->frame_pid[i] = INVALID_PAGE_ID;
        bp->free_list[i] = i;
    }
    bp->free_count = pool_size;

    bp->repl = replacer_create(rtype, pool_size);
    return bp;
}

void bp_destroy(buffer_pool_t *bp) {
    if (!bp) return;
    bp_flush_all(bp);
    free(bp->frames);
    free(bp->pin_count);
    free(bp->dirty);
    free(bp->frame_pid);
    free(bp->pt);
    free(bp->free_list);
    replacer_destroy(bp->repl);
    free(bp);
}

page_t *bp_fetch_page(buffer_pool_t *bp, page_id_t pid) {
    frame_id_t fid = pt_lookup(bp, pid);
    if (fid != INVALID_FRAME_ID) {
        bp->pin_count[fid]++;
        replacer_pin(bp->repl, fid);
        bp->stats.hits++;
        return &bp->frames[fid];
    }

    bp->stats.misses++;

    fid = bp_get_frame(bp);
    if (fid == INVALID_FRAME_ID) return NULL;

    if (!pager_read(bp->pager, pid, &bp->frames[fid])) return NULL;

    bp->frame_pid[fid] = pid;
    bp->pin_count[fid] = 1;
    bp->dirty[fid] = false;
    pt_insert(bp, pid, fid);
    replacer_pin(bp->repl, fid);

    return &bp->frames[fid];
}

void bp_unpin_page(buffer_pool_t *bp, page_id_t pid, bool is_dirty) {
    frame_id_t fid = pt_lookup(bp, pid);
    if (fid == INVALID_FRAME_ID) return;

    if (is_dirty) bp->dirty[fid] = true;
    bp->pin_count[fid]--;

    if (bp->pin_count[fid] == 0) {
        replacer_unpin(bp->repl, fid);
    }
}

page_id_t bp_new_page(buffer_pool_t *bp, page_t **page) {
    page_id_t pid = pager_allocate(bp->pager);
    if (pid == INVALID_PAGE_ID) return INVALID_PAGE_ID;

    frame_id_t fid = bp_get_frame(bp);
    if (fid == INVALID_FRAME_ID) return INVALID_PAGE_ID;

    page_init(&bp->frames[fid], pid, PAGE_TYPE_HEAP);
    bp->frame_pid[fid] = pid;
    bp->pin_count[fid] = 1;
    bp->dirty[fid] = true;
    pt_insert(bp, pid, fid);
    replacer_pin(bp->repl, fid);

    if (page) *page = &bp->frames[fid];
    return pid;
}

bool bp_flush_page(buffer_pool_t *bp, page_id_t pid) {
    frame_id_t fid = pt_lookup(bp, pid);
    if (fid == INVALID_FRAME_ID) return false;

    if (bp->dirty[fid]) {
        if (!pager_write(bp->pager, pid, &bp->frames[fid])) return false;
        bp->dirty[fid] = false;
        bp->stats.dirty_writes++;
    }
    return true;
}

void bp_flush_all(buffer_pool_t *bp) {
    for (int fid = 0; fid < bp->pool_size; fid++) {
        if (bp->frame_pid[fid] != INVALID_PAGE_ID && bp->dirty[fid]) {
            pager_write(bp->pager, bp->frame_pid[fid], &bp->frames[fid]);
            bp->dirty[fid] = false;
            bp->stats.dirty_writes++;
        }
    }
}

void bp_get_stats(buffer_pool_t *bp, bp_stats_t *stats) {
    *stats = bp->stats;
}