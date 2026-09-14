#ifndef MINIDB_BTREE_NODE_H
#define MINIDB_BTREE_NODE_H

#include "page.h"
#include <stdint.h>

#define BTREE_MAX_KEYS 32
#define BTREE_MIN_KEYS 16

typedef int32_t btree_key_t;

typedef struct {
    page_id_t page_id;
    slot_id_t slot_id;
} rid_t;

typedef enum {
    BTREE_INTERNAL = 0,
    BTREE_LEAF = 1,
} node_type_t;

/*
 * B+Tree 节点布局（复用 page_t，page_type = PAGE_TYPE_INDEX）：
 *
 *   [0..19]   page header (章1)
 *   [20]      node_type     (1B)
 *   [21..22]  num_keys      (2B, 大端)
 *   [23..26]  parent_pid    (4B)
 *   [27..30]  next_leaf_pid (4B, 叶子节点链表)
 *   [31..]    keys[MAX_KEYS] (每个 4B)
 *             内节点: children[MAX_KEYS+1] (每个 4B)
 *             叶子:   rids[MAX_KEYS]      (每个 8B)
 */
#define NODE_OFF_TYPE      PAGE_HEADER_SIZE
#define NODE_OFF_NUM_KEYS  (PAGE_HEADER_SIZE + 1)
#define NODE_OFF_PARENT    (PAGE_HEADER_SIZE + 3)
#define NODE_OFF_NEXT_LEAF (PAGE_HEADER_SIZE + 7)
#define NODE_OFF_KEYS      (PAGE_HEADER_SIZE + 11)

#define KEY_SIZE   4
#define CHILD_SIZE 4
#define RID_SIZE   8

#define NODE_OFF_CHILDREN (NODE_OFF_KEYS + BTREE_MAX_KEYS * KEY_SIZE)
#define NODE_OFF_RIDS     (NODE_OFF_KEYS + BTREE_MAX_KEYS * KEY_SIZE)

void       node_init(page_t *page, node_type_t type, page_id_t parent);

node_type_t node_get_type(const page_t *page);
uint16_t    node_get_num_keys(const page_t *page);
void        node_set_num_keys(page_t *page, uint16_t n);
page_id_t   node_get_parent(const page_t *page);
void        node_set_parent(page_t *page, page_id_t pid);
page_id_t   node_get_next_leaf(const page_t *page);
void        node_set_next_leaf(page_t *page, page_id_t pid);

btree_key_t node_get_key(const page_t *page, int idx);
void        node_set_key(page_t *page, int idx, btree_key_t key);

page_id_t   node_get_child(const page_t *page, int idx);
void        node_set_child(page_t *page, int idx, page_id_t pid);

rid_t       node_get_rid(const page_t *page, int idx);
void        node_set_rid(page_t *page, int idx, rid_t rid);

int         node_find_key(const page_t *page, btree_key_t key);
bool        node_is_full(const page_t *page);

#endif