#ifndef MINIDB_BUFFER_POOL_H
#define MINIDB_BUFFER_POOL_H

#include "page.h"
#include "pager.h"
#include "replacer.h"

typedef struct buffer_pool buffer_pool_t;

typedef struct {
    int hits;
    int misses;
    int evictions;
    int dirty_writes;
} bp_stats_t;

buffer_pool_t *bp_create(pager_t *pager, int pool_size, replacer_type_t rtype);
void           bp_destroy(buffer_pool_t *bp);

page_t   *bp_fetch_page(buffer_pool_t *bp, page_id_t pid);
void      bp_unpin_page(buffer_pool_t *bp, page_id_t pid, bool is_dirty);
page_id_t bp_new_page(buffer_pool_t *bp, page_t **page);
bool      bp_flush_page(buffer_pool_t *bp, page_id_t pid);
void      bp_flush_all(buffer_pool_t *bp);

void      bp_get_stats(buffer_pool_t *bp, bp_stats_t *stats);

#endif