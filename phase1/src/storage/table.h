#ifndef MINIDB_TABLE_H
#define MINIDB_TABLE_H

#include "heap.h"
#include "btree.h"

typedef struct table table_t;

table_t *table_create(buffer_pool_t *bp, const schema_t *schema, int pk_col);
void     table_destroy(table_t *table);

rid_t    table_insert(table_t *table, const tuple_t *tuple);
tuple_t *table_find(table_t *table, int32_t pk);
bool     table_delete(table_t *table, int32_t pk);

typedef struct table_scan table_scan_t;
table_scan_t *table_scan_open(table_t *table, int32_t start, int32_t end);
bool          table_scan_next(table_scan_t *scan, tuple_t *tuple);
void          table_scan_close(table_scan_t *scan);

heap_t *table_heap(table_t *table);
btree_t *table_index(table_t *table);

#endif