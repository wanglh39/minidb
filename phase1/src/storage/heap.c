#include "heap.h"
#include <stdlib.h>
#include <string.h>

struct heap {
    buffer_pool_t *bp;
    const schema_t *schema;
    page_id_t first_page;
    page_id_t last_page;
};

struct heap_scan {
    buffer_pool_t *bp;
    const schema_t *schema;
    page_id_t cur_page;
    slot_id_t cur_slot;
    bool done;
};

heap_t *heap_create(buffer_pool_t *bp, const schema_t *schema) {
    heap_t *heap = malloc(sizeof(heap_t));
    heap->bp = bp;
    heap->schema = schema;

    page_t *page;
    page_id_t pid = bp_new_page(bp, &page);
    heap->first_page = pid;
    heap->last_page = pid;
    bp_unpin_page(bp, pid, true);
    return heap;
}

void heap_destroy(heap_t *heap) {
    free(heap);
}

rid_t heap_insert(heap_t *heap, const tuple_t *tuple) {
    uint8_t buf[PAGE_SIZE];
    uint16_t len = tuple_serialize(tuple, buf);

    page_id_t target_pid = heap->last_page;
    page_t *page = bp_fetch_page(heap->bp, target_pid);

    if (!page_has_space(page, len)) {
        bp_unpin_page(heap->bp, target_pid, false);

        page_t *new_page;
        page_id_t new_pid = bp_new_page(heap->bp, &new_page);

        page_t *old_page = bp_fetch_page(heap->bp, target_pid);
        page_set_next_page(old_page, new_pid);
        bp_unpin_page(heap->bp, target_pid, true);

        heap->last_page = new_pid;
        target_pid = new_pid;
        page = new_page;
    }

    slot_id_t sid = page_add_tuple(page, buf, len);
    rid_t rid = { target_pid, sid };
    bp_unpin_page(heap->bp, target_pid, true);
    return rid;
}

tuple_t *heap_fetch(heap_t *heap, rid_t rid) {
    page_t *page = bp_fetch_page(heap->bp, rid.page_id);
    uint16_t len;
    const void *data = page_get_tuple(page, rid.slot_id, &len);

    tuple_t *tuple = NULL;
    if (data && len > 0) {
        tuple = tuple_create(heap->schema);
        tuple_deserialize(tuple, (const uint8_t *)data, len);
    }
    bp_unpin_page(heap->bp, rid.page_id, false);
    return tuple;
}

bool heap_delete(heap_t *heap, rid_t rid) {
    page_t *page = bp_fetch_page(heap->bp, rid.page_id);
    bool ok = page_delete_tuple(page, rid.slot_id);
    bp_unpin_page(heap->bp, rid.page_id, true);
    return ok;
}

heap_scan_t *heap_scan_open(heap_t *heap) {
    heap_scan_t *scan = malloc(sizeof(heap_scan_t));
    scan->bp = heap->bp;
    scan->schema = heap->schema;
    scan->cur_page = heap->first_page;
    scan->cur_slot = 0;
    scan->done = false;
    return scan;
}

bool heap_scan_next(heap_scan_t *scan, rid_t *rid, tuple_t *tuple) {
    if (scan->done) return false;

    for (;;) {
        page_t *page = bp_fetch_page(scan->bp, scan->cur_page);
        uint16_t num_slots = page_get_num_slots(page);

        while (scan->cur_slot < num_slots) {
            slot_id_t sid = scan->cur_slot;
            scan->cur_slot++;

            if (page_is_slot_deleted(page, sid)) continue;

            uint16_t len;
            const void *data = page_get_tuple(page, sid, &len);
            if (!data) continue;

            if (rid) {
                rid->page_id = scan->cur_page;
                rid->slot_id = sid;
            }
            if (tuple) {
                tuple_deserialize(tuple, (const uint8_t *)data, len);
            }
            bp_unpin_page(scan->bp, scan->cur_page, false);
            return true;
        }

        page_id_t next = page_get_next_page(page);
        bp_unpin_page(scan->bp, scan->cur_page, false);

        if (next == INVALID_PAGE_ID) {
            scan->done = true;
            return false;
        }
        scan->cur_page = next;
        scan->cur_slot = 0;
    }
}

void heap_scan_close(heap_scan_t *scan) {
    free(scan);
}

page_id_t heap_first_page(heap_t *heap) {
    return heap->first_page;
}