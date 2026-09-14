#include "page.h"
#include <string.h>

#define HDR_OFFSET_ID        0
#define HDR_OFFSET_TYPE      4
#define HDR_OFFSET_NUM_SLOTS 5
#define HDR_OFFSET_FREE      7
#define HDR_OFFSET_NEXT      9
#define HDR_OFFSET_CHECKSUM  13

static uint16_t read_u16(const uint8_t *p) {
    return (uint16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}

static uint32_t read_u32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8)  |  (uint32_t)p[3];
}

static void write_u16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)((v >> 8) & 0xFF);
    p[1] = (uint8_t)(v & 0xFF);
}

static void write_u32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)((v >> 24) & 0xFF);
    p[1] = (uint8_t)((v >> 16) & 0xFF);
    p[2] = (uint8_t)((v >> 8)  & 0xFF);
    p[3] = (uint8_t)(v & 0xFF);
}

void page_init(page_t *page, page_id_t pid, page_type_t type) {
    memset(page->data, 0, PAGE_SIZE);
    write_u32(page->data + HDR_OFFSET_ID, pid);
    page->data[HDR_OFFSET_TYPE] = (uint8_t)type;
    write_u16(page->data + HDR_OFFSET_NUM_SLOTS, 0);
    write_u16(page->data + HDR_OFFSET_FREE, PAGE_SIZE);
    write_u32(page->data + HDR_OFFSET_NEXT, INVALID_PAGE_ID);
}

page_id_t page_get_id(const page_t *page) {
    return read_u32(page->data + HDR_OFFSET_ID);
}

page_type_t page_get_type(const page_t *page) {
    return (page_type_t)page->data[HDR_OFFSET_TYPE];
}

uint16_t page_get_num_slots(const page_t *page) {
    return read_u16(page->data + HDR_OFFSET_NUM_SLOTS);
}

page_id_t page_get_next_page(const page_t *page) {
    return read_u32(page->data + HDR_OFFSET_NEXT);
}

void page_set_next_page(page_t *page, page_id_t pid) {
    write_u32(page->data + HDR_OFFSET_NEXT, pid);
}

static uint16_t slot_get_offset(const page_t *page, slot_id_t sid) {
    const uint8_t *slot = page->data + PAGE_HEADER_SIZE + (size_t)sid * SLOT_SIZE;
    return read_u16(slot);
}

static uint16_t slot_get_length(const page_t *page, slot_id_t sid) {
    const uint8_t *slot = page->data + PAGE_HEADER_SIZE + (size_t)sid * SLOT_SIZE;
    return read_u16(slot + 2);
}

static void slot_set(page_t *page, slot_id_t sid, uint16_t offset, uint16_t length) {
    uint8_t *slot = page->data + PAGE_HEADER_SIZE + (size_t)sid * SLOT_SIZE;
    write_u16(slot, offset);
    write_u16(slot + 2, length);
}

uint16_t page_free_space(const page_t *page) {
    uint16_t num_slots = page_get_num_slots(page);
    uint16_t free_offset = read_u16(page->data + HDR_OFFSET_FREE);
    uint16_t slot_area_end = (uint16_t)(PAGE_HEADER_SIZE + (size_t)num_slots * SLOT_SIZE);
    return (uint16_t)(free_offset - slot_area_end);
}

bool page_has_space(const page_t *page, uint16_t len) {
    return page_free_space(page) >= (uint16_t)(len + SLOT_SIZE);
}

slot_id_t page_add_tuple(page_t *page, const void *data, uint16_t len) {
    if (len == 0 || !page_has_space(page, len)) {
        return INVALID_SLOT_ID;
    }

    uint16_t num_slots = page_get_num_slots(page);
    uint16_t free_offset = read_u16(page->data + HDR_OFFSET_FREE);

    free_offset = (uint16_t)(free_offset - len);
    memcpy(page->data + free_offset, data, len);

    slot_set(page, num_slots, free_offset, len);

    write_u16(page->data + HDR_OFFSET_NUM_SLOTS, (uint16_t)(num_slots + 1));
    write_u16(page->data + HDR_OFFSET_FREE, free_offset);

    return num_slots;
}

const void *page_get_tuple(const page_t *page, slot_id_t sid, uint16_t *len) {
    uint16_t num_slots = page_get_num_slots(page);
    if (sid >= num_slots) {
        if (len) *len = 0;
        return NULL;
    }

    uint16_t offset = slot_get_offset(page, sid);
    uint16_t length = slot_get_length(page, sid);

    if (length == 0) {
        if (len) *len = 0;
        return NULL;
    }

    if (len) *len = length;
    return page->data + offset;
}

bool page_delete_tuple(page_t *page, slot_id_t sid) {
    uint16_t num_slots = page_get_num_slots(page);
    if (sid >= num_slots) {
        return false;
    }
    uint8_t *slot = page->data + PAGE_HEADER_SIZE + (size_t)sid * SLOT_SIZE;
    write_u16(slot + 2, 0);
    return true;
}

bool page_is_slot_deleted(const page_t *page, slot_id_t sid) {
    uint16_t num_slots = page_get_num_slots(page);
    if (sid >= num_slots) {
        return true;
    }
    return slot_get_length(page, sid) == 0;
}

bool page_validate(const page_t *page, page_id_t expected_id) {
    if (page_get_id(page) != expected_id) {
        return false;
    }
    uint16_t num_slots = page_get_num_slots(page);
    uint16_t free_offset = read_u16(page->data + HDR_OFFSET_FREE);
    uint16_t slot_area_end = (uint16_t)(PAGE_HEADER_SIZE + (size_t)num_slots * SLOT_SIZE);
    if (free_offset < slot_area_end || free_offset > PAGE_SIZE) {
        return false;
    }
    return true;
}