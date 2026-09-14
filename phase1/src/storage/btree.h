#ifndef MINIDB_BTREE_H
#define MINIDB_BTREE_H

#include "buffer_pool.h"
#include "btree_node.h"

typedef struct btree btree_t;

btree_t *btree_create(buffer_pool_t *bp);
btree_t *btree_open(buffer_pool_t *bp, page_id_t root_pid);
void     btree_destroy(btree_t *tree);

bool btree_insert(btree_t *tree, btree_key_t key, rid_t rid);
bool btree_find(btree_t *tree, btree_key_t key, rid_t *rid);
bool btree_delete(btree_t *tree, btree_key_t key);

typedef struct btree_cursor btree_cursor_t;
btree_cursor_t *btree_range_open(btree_t *tree, btree_key_t start, btree_key_t end);
bool            btree_range_next(btree_cursor_t *cur, btree_key_t *key, rid_t *rid);
void            btree_range_close(btree_cursor_t *cur);

page_id_t btree_root_pid(btree_t *tree);

#endif