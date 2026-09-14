#include "btree_node.h"
#include <string.h>

static uint16_t rd_u16(const uint8_t *p) {
    return (uint16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}

static uint32_t rd_u32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8)  |  (uint32_t)p[3];
}

static void wr_u16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)((v >> 8) & 0xFF);
    p[1] = (uint8_t)(v & 0xFF);
}

static void wr_u32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)((v >> 24) & 0xFF);
    p[1] = (uint8_t)((v >> 16) & 0xFF);
    p[2] = (uint8_t)((v >> 8)  & 0xFF);
    p[3] = (uint8_t)(v & 0xFF);
}

void node_init(page_t *page, node_type_t type, page_id_t parent) {
    memset(page->data + PAGE_HEADER_SIZE, 0, PAGE_SIZE - PAGE_HEADER_SIZE);
    page->data[4] = (uint8_t)PAGE_TYPE_INDEX;
    page->data[NODE_OFF_TYPE] = (uint8_t)type;
    wr_u16(page->data + NODE_OFF_NUM_KEYS, 0);
    wr_u32(page->data + NODE_OFF_PARENT, parent);
    wr_u32(page->data + NODE_OFF_NEXT_LEAF, INVALID_PAGE_ID);
}

node_type_t node_get_type(const page_t *page) {
    return (node_type_t)page->data[NODE_OFF_TYPE];
}

uint16_t node_get_num_keys(const page_t *page) {
    return rd_u16(page->data + NODE_OFF_NUM_KEYS);
}

void node_set_num_keys(page_t *page, uint16_t n) {
    wr_u16(page->data + NODE_OFF_NUM_KEYS, n);
}

page_id_t node_get_parent(const page_t *page) {
    return rd_u32(page->data + NODE_OFF_PARENT);
}

void node_set_parent(page_t *page, page_id_t pid) {
    wr_u32(page->data + NODE_OFF_PARENT, pid);
}

page_id_t node_get_next_leaf(const page_t *page) {
    return rd_u32(page->data + NODE_OFF_NEXT_LEAF);
}

void node_set_next_leaf(page_t *page, page_id_t pid) {
    wr_u32(page->data + NODE_OFF_NEXT_LEAF, pid);
}

btree_key_t node_get_key(const page_t *page, int idx) {
    return (btree_key_t)rd_u32(page->data + NODE_OFF_KEYS + idx * KEY_SIZE);
}

void node_set_key(page_t *page, int idx, btree_key_t key) {
    wr_u32(page->data + NODE_OFF_KEYS + idx * KEY_SIZE, (uint32_t)key);
}

page_id_t node_get_child(const page_t *page, int idx) {
    return rd_u32(page->data + NODE_OFF_CHILDREN + idx * CHILD_SIZE);
}

void node_set_child(page_t *page, int idx, page_id_t pid) {
    wr_u32(page->data + NODE_OFF_CHILDREN + idx * CHILD_SIZE, pid);
}

rid_t node_get_rid(const page_t *page, int idx) {
    const uint8_t *p = page->data + NODE_OFF_RIDS + idx * RID_SIZE;
    rid_t rid;
    rid.page_id = rd_u32(p);
    rid.slot_id = rd_u16(p + 4);
    return rid;
}

void node_set_rid(page_t *page, int idx, rid_t rid) {
    uint8_t *p = page->data + NODE_OFF_RIDS + idx * RID_SIZE;
    wr_u32(p, rid.page_id);
    wr_u16(p + 4, rid.slot_id);
}

int node_find_key(const page_t *page, btree_key_t key) {
    uint16_t n = node_get_num_keys(page);
    int lo = 0, hi = n;
    while (lo < hi) {
        int mid = (lo + hi) / 2;
        if (node_get_key(page, mid) < key) lo = mid + 1;
        else hi = mid;
    }
    return lo;
}

bool node_is_full(const page_t *page) {
    return node_get_num_keys(page) >= BTREE_MAX_KEYS;
}