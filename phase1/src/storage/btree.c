#include "btree.h"
#include <stdlib.h>
#include <string.h>

struct btree {
    buffer_pool_t *bp;
    page_id_t root_pid;
};

struct btree_cursor {
    buffer_pool_t *bp;
    page_id_t leaf_pid;
    int idx;
    int num_keys;
    btree_key_t end;
    bool done;
};

/* ---------- 辅助：找叶子节点 ---------- */

static page_id_t find_leaf(buffer_pool_t *bp, page_id_t root, btree_key_t key) {
    page_id_t cur = root;
    for (;;) {
        page_t *node = bp_fetch_page(bp, cur);
        if (node_get_type(node) == BTREE_LEAF) {
            bp_unpin_page(bp, cur, false);
            return cur;
        }
        int idx = node_find_key(node, key);
        page_id_t child = node_get_child(node, idx);
        bp_unpin_page(bp, cur, false);
        cur = child;
    }
}

/* ---------- 辅助：叶子插入 ---------- */

static void leaf_insert_at(page_t *leaf, int idx, btree_key_t key, rid_t rid) {
    uint16_t n = node_get_num_keys(leaf);
    for (int i = n; i > idx; i--) {
        node_set_key(leaf, i, node_get_key(leaf, i - 1));
        node_set_rid(leaf, i, node_get_rid(leaf, i - 1));
    }
    node_set_key(leaf, idx, key);
    node_set_rid(leaf, idx, rid);
    node_set_num_keys(leaf, n + 1);
}

/* ---------- 辅助：内节点插入 ---------- */

static void internal_insert_at(page_t *node, int idx, btree_key_t key,
                               page_id_t left_child, page_id_t right_child) {
    uint16_t n = node_get_num_keys(node);
    for (int i = n; i > idx; i--) {
        node_set_key(node, i, node_get_key(node, i - 1));
        node_set_child(node, i + 1, node_get_child(node, i));
    }
    node_set_key(node, idx, key);
    node_set_child(node, idx, left_child);
    node_set_child(node, idx + 1, right_child);
    node_set_num_keys(node, n + 1);
}

/* ---------- 分裂：叶子 ---------- */

static btree_key_t split_leaf(buffer_pool_t *bp, page_id_t leaf_pid,
                              page_id_t *new_pid_out) {
    page_t *leaf = bp_fetch_page(bp, leaf_pid);
    uint16_t n = node_get_num_keys(leaf);
    int mid = n / 2;

    page_t *new_leaf;
    page_id_t new_pid = bp_new_page(bp, &new_leaf);
    node_init(new_leaf, BTREE_LEAF, node_get_parent(leaf));

    for (int i = mid; i < n; i++) {
        node_set_key(new_leaf, i - mid, node_get_key(leaf, i));
        node_set_rid(new_leaf, i - mid, node_get_rid(leaf, i));
    }
    node_set_num_keys(new_leaf, n - mid);
    node_set_num_keys(leaf, mid);

    node_set_next_leaf(new_leaf, node_get_next_leaf(leaf));
    node_set_next_leaf(leaf, new_pid);

    btree_key_t up_key = node_get_key(new_leaf, 0);

    bp_unpin_page(bp, leaf_pid, true);
    bp_unpin_page(bp, new_pid, true);

    *new_pid_out = new_pid;
    return up_key;
}

/* ---------- 分裂：内节点 ---------- */

static btree_key_t split_internal(buffer_pool_t *bp, page_id_t node_pid,
                                  page_id_t *new_pid_out) {
    page_t *node = bp_fetch_page(bp, node_pid);
    uint16_t n = node_get_num_keys(node);
    int mid = n / 2;

    page_t *new_node;
    page_id_t new_pid = bp_new_page(bp, &new_node);
    node_init(new_node, BTREE_INTERNAL, node_get_parent(node));

    btree_key_t up_key = node_get_key(node, mid);

    for (int i = mid + 1; i < n; i++) {
        node_set_key(new_node, i - mid - 1, node_get_key(node, i));
        node_set_child(new_node, i - mid - 1, node_get_child(node, i));
    }
    node_set_child(new_node, n - mid - 1, node_get_child(node, n));
    node_set_num_keys(new_node, n - mid - 1);
    node_set_num_keys(node, mid);

    for (int i = 0; i <= node_get_num_keys(new_node); i++) {
        page_id_t cpid = node_get_child(new_node, i);
        page_t *child = bp_fetch_page(bp, cpid);
        node_set_parent(child, new_pid);
        bp_unpin_page(bp, cpid, true);
    }

    bp_unpin_page(bp, node_pid, true);
    bp_unpin_page(bp, new_pid, true);

    *new_pid_out = new_pid;
    return up_key;
}

/* ---------- 分裂后插入到父节点 ---------- */

static void insert_in_parent(buffer_pool_t *bp, page_id_t left_pid,
                             btree_key_t key, page_id_t right_pid,
                             page_id_t *root_ptr) {
    page_t *left = bp_fetch_page(bp, left_pid);
    page_id_t parent_pid = node_get_parent(left);
    bp_unpin_page(bp, left_pid, false);

    if (parent_pid == INVALID_PAGE_ID) {
        page_t *new_root;
        page_id_t new_root_pid = bp_new_page(bp, &new_root);
        node_init(new_root, BTREE_INTERNAL, INVALID_PAGE_ID);
        node_set_num_keys(new_root, 1);
        node_set_key(new_root, 0, key);
        node_set_child(new_root, 0, left_pid);
        node_set_child(new_root, 1, right_pid);

        page_t *l = bp_fetch_page(bp, left_pid);
        node_set_parent(l, new_root_pid);
        bp_unpin_page(bp, left_pid, true);

        page_t *r = bp_fetch_page(bp, right_pid);
        node_set_parent(r, new_root_pid);
        bp_unpin_page(bp, right_pid, true);

        bp_unpin_page(bp, new_root_pid, true);
        *root_ptr = new_root_pid;
        return;
    }

    page_t *parent = bp_fetch_page(bp, parent_pid);
    int idx = node_find_key(parent, key);
    internal_insert_at(parent, idx, key, left_pid, right_pid);

    bool need_split = node_is_full(parent);
    bp_unpin_page(bp, parent_pid, true);

    if (need_split) {
        page_id_t new_internal_pid;
        btree_key_t up_key = split_internal(bp, parent_pid, &new_internal_pid);
        insert_in_parent(bp, parent_pid, up_key, new_internal_pid, root_ptr);
    }
}

/* ---------- 公共接口 ---------- */

btree_t *btree_create(buffer_pool_t *bp) {
    page_t *root;
    page_id_t root_pid = bp_new_page(bp, &root);
    node_init(root, BTREE_LEAF, INVALID_PAGE_ID);
    bp_unpin_page(bp, root_pid, true);

    btree_t *tree = malloc(sizeof(btree_t));
    tree->bp = bp;
    tree->root_pid = root_pid;
    return tree;
}

void btree_destroy(btree_t *tree) {
    if (!tree) return;
    free(tree);
}

btree_t *btree_open(buffer_pool_t *bp, page_id_t root_pid) {
    btree_t *tree = malloc(sizeof(btree_t));
    tree->bp = bp;
    tree->root_pid = root_pid;
    return tree;
}

bool btree_insert(btree_t *tree, btree_key_t key, rid_t rid) {
    page_id_t leaf_pid = find_leaf(tree->bp, tree->root_pid, key);
    page_t *leaf = bp_fetch_page(tree->bp, leaf_pid);

    int idx = node_find_key(leaf, key);
    uint16_t n = node_get_num_keys(leaf);
    if (idx < n && node_get_key(leaf, idx) == key) {
        bp_unpin_page(tree->bp, leaf_pid, false);
        return false;
    }

    if (!node_is_full(leaf)) {
        leaf_insert_at(leaf, idx, key, rid);
        bp_unpin_page(tree->bp, leaf_pid, true);
        return true;
    }

    bp_unpin_page(tree->bp, leaf_pid, false);

    page_t *leaf2 = bp_fetch_page(tree->bp, leaf_pid);
    leaf_insert_at(leaf2, idx, key, rid);
    bp_unpin_page(tree->bp, leaf_pid, true);

    page_id_t new_leaf_pid;
    btree_key_t up_key = split_leaf(tree->bp, leaf_pid, &new_leaf_pid);
    insert_in_parent(tree->bp, leaf_pid, up_key, new_leaf_pid, &tree->root_pid);
    return true;
}

bool btree_find(btree_t *tree, btree_key_t key, rid_t *rid) {
    page_id_t leaf_pid = find_leaf(tree->bp, tree->root_pid, key);
    page_t *leaf = bp_fetch_page(tree->bp, leaf_pid);

    int idx = node_find_key(leaf, key);
    uint16_t n = node_get_num_keys(leaf);
    bool found = false;
    if (idx < n && node_get_key(leaf, idx) == key) {
        if (rid) *rid = node_get_rid(leaf, idx);
        found = true;
    }
    bp_unpin_page(tree->bp, leaf_pid, false);
    return found;
}

bool btree_delete(btree_t *tree, btree_key_t key) {
    page_id_t leaf_pid = find_leaf(tree->bp, tree->root_pid, key);
    page_t *leaf = bp_fetch_page(tree->bp, leaf_pid);

    int idx = node_find_key(leaf, key);
    uint16_t n = node_get_num_keys(leaf);
    if (idx >= n || node_get_key(leaf, idx) != key) {
        bp_unpin_page(tree->bp, leaf_pid, false);
        return false;
    }

    for (int i = idx; i < n - 1; i++) {
        node_set_key(leaf, i, node_get_key(leaf, i + 1));
        node_set_rid(leaf, i, node_get_rid(leaf, i + 1));
    }
    node_set_num_keys(leaf, n - 1);
    bp_unpin_page(tree->bp, leaf_pid, true);
    return true;
}

/* ---------- 范围查询 ---------- */

btree_cursor_t *btree_range_open(btree_t *tree, btree_key_t start, btree_key_t end) {
    page_id_t leaf_pid = find_leaf(tree->bp, tree->root_pid, start);
    page_t *leaf = bp_fetch_page(tree->bp, leaf_pid);
    int idx = node_find_key(leaf, start);
    int n = node_get_num_keys(leaf);
    bp_unpin_page(tree->bp, leaf_pid, false);

    btree_cursor_t *cur = malloc(sizeof(btree_cursor_t));
    cur->bp = tree->bp;
    cur->leaf_pid = leaf_pid;
    cur->idx = idx;
    cur->num_keys = n;
    cur->end = end;
    cur->done = false;
    return cur;
}

bool btree_range_next(btree_cursor_t *cur, btree_key_t *key, rid_t *rid) {
    if (cur->done) return false;

    for (;;) {
        if (cur->idx >= cur->num_keys) {
            page_t *leaf = bp_fetch_page(cur->bp, cur->leaf_pid);
            page_id_t next = node_get_next_leaf(leaf);
            bp_unpin_page(cur->bp, cur->leaf_pid, false);

            if (next == INVALID_PAGE_ID) {
                cur->done = true;
                return false;
            }
            cur->leaf_pid = next;
            leaf = bp_fetch_page(cur->bp, next);
            cur->idx = 0;
            cur->num_keys = node_get_num_keys(leaf);
            bp_unpin_page(cur->bp, next, false);
        }

        page_t *leaf = bp_fetch_page(cur->bp, cur->leaf_pid);
        btree_key_t k = node_get_key(leaf, cur->idx);
        rid_t r = node_get_rid(leaf, cur->idx);
        bp_unpin_page(cur->bp, cur->leaf_pid, false);

        if (k > cur->end) {
            cur->done = true;
            return false;
        }

        cur->idx++;
        if (key) *key = k;
        if (rid) *rid = r;
        return true;
    }
}

void btree_range_close(btree_cursor_t *cur) {
    free(cur);
}

page_id_t btree_root_pid(btree_t *tree) {
    return tree->root_pid;
}