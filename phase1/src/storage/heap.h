#ifndef MINIDB_HEAP_H
#define MINIDB_HEAP_H

#include "buffer_pool.h"
#include "schema.h"
#include "tuple.h"
#include "btree_node.h"

typedef struct heap heap_t;

heap_t *heap_create(buffer_pool_t *bp, const schema_t *schema);
void    heap_destroy(heap_t *heap);

rid_t   heap_insert(heap_t *heap, const tuple_t *tuple);
tuple_t *heap_fetch(heap_t *heap, rid_t rid);
bool    heap_delete(heap_t *heap, rid_t rid);

typedef struct heap_scan heap_scan_t;
heap_scan_t *heap_scan_open(heap_t *heap);
bool         heap_scan_next(heap_scan_t *scan, rid_t *rid, tuple_t *tuple);
void         heap_scan_close(heap_scan_t *scan);

page_id_t heap_first_page(heap_t *heap);

#endif