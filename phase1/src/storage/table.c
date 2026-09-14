#include "table.h"
#include <stdlib.h>

struct table {
    heap_t *heap;
    btree_t *index;
    const schema_t *schema;
    int pk_col;
};

struct table_scan {
    btree_cursor_t *bcur;
    heap_t *heap;
};

table_t *table_create(buffer_pool_t *bp, const schema_t *schema, int pk_col) {
    table_t *table = malloc(sizeof(table_t));
    table->heap = heap_create(bp, schema);
    table->index = btree_create(bp);
    table->schema = schema;
    table->pk_col = pk_col;
    return table;
}

void table_destroy(table_t *table) {
    if (!table) return;
    heap_destroy(table->heap);
    btree_destroy(table->index);
    free(table);
}

rid_t table_insert(table_t *table, const tuple_t *tuple) {
    rid_t rid = heap_insert(table->heap, tuple);
    int32_t pk = tuple_get_int32(tuple, table->pk_col);
    btree_insert(table->index, pk, rid);
    return rid;
}

tuple_t *table_find(table_t *table, int32_t pk) {
    rid_t rid;
    if (!btree_find(table->index, pk, &rid)) return NULL;
    return heap_fetch(table->heap, rid);
}

bool table_delete(table_t *table, int32_t pk) {
    rid_t rid;
    if (!btree_find(table->index, pk, &rid)) return false;
    btree_delete(table->index, pk);
    heap_delete(table->heap, rid);
    return true;
}

table_scan_t *table_scan_open(table_t *table, int32_t start, int32_t end) {
    table_scan_t *scan = malloc(sizeof(table_scan_t));
    scan->bcur = btree_range_open(table->index, start, end);
    scan->heap = table->heap;
    return scan;
}

bool table_scan_next(table_scan_t *scan, tuple_t *tuple) {
    btree_key_t key;
    rid_t rid;
    if (!btree_range_next(scan->bcur, &key, &rid)) return false;

    tuple_t *fetched = heap_fetch(scan->heap, rid);
    if (!fetched) return false;

    tuple_deserialize(tuple, fetched->data, fetched->length);
    tuple_destroy(fetched);
    return true;
}

void table_scan_close(table_scan_t *scan) {
    btree_range_close(scan->bcur);
    free(scan);
}

heap_t *table_heap(table_t *table) {
    return table->heap;
}

btree_t *table_index(table_t *table) {
    return table->index;
}