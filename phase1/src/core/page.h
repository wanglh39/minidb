#ifndef MINIDB_PAGE_H
#define MINIDB_PAGE_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#define PAGE_SIZE 4096
#define PAGE_HEADER_SIZE 20
#define SLOT_SIZE 4

typedef uint32_t page_id_t;
typedef uint16_t slot_id_t;

#define INVALID_PAGE_ID ((page_id_t)0xFFFFFFFFu)
#define INVALID_SLOT_ID ((slot_id_t)0xFFFFu)

typedef enum {
    PAGE_TYPE_FREE = 0,
    PAGE_TYPE_HEAP = 1,
    PAGE_TYPE_INDEX = 2,
    PAGE_TYPE_OVERFLOW = 3,
} page_type_t;

/*
 * Slotted Page 布局（4096 字节）：
 *
 *   偏移 0                                              4096
 *   ┌────────────┬────────────┬────────────┬────────────┐
 *   │ Header(20) │ Slot Array │  空闲空间  │ Tuple Data │
 *   │            │ → 向右生长 │            │ ← 向左生长 │
 *   └────────────┴────────────┴────────────┴────────────┘
 *
 * Header 布局（20 字节，大端序）：
 *   0-3   page_id          (4B)
 *   4     page_type        (1B)
 *   5-6   num_slots        (2B)
 *   7-8   free_space_offset(2B)  tuple data 边界，初始 = PAGE_SIZE
 *   9-12  next_page_id     (4B)  链表，溢出页用
 *   13-16 checksum         (4B)
 *   17-19 保留             (3B)
 *
 * Slot 布局（4 字节，大端序）：
 *   0-1   offset           (2B)  tuple 在页内偏移
 *   2-3   length           (2B)  tuple 长度，0 = 已删除
 */
typedef struct {
    uint8_t data[PAGE_SIZE];
} page_t;

void page_init(page_t *page, page_id_t pid, page_type_t type);

page_id_t     page_get_id(const page_t *page);
page_type_t   page_get_type(const page_t *page);
uint16_t      page_get_num_slots(const page_t *page);
page_id_t     page_get_next_page(const page_t *page);
void          page_set_next_page(page_t *page, page_id_t pid);

slot_id_t     page_add_tuple(page_t *page, const void *data, uint16_t len);
const void   *page_get_tuple(const page_t *page, slot_id_t sid, uint16_t *len);
bool          page_delete_tuple(page_t *page, slot_id_t sid);
bool          page_is_slot_deleted(const page_t *page, slot_id_t sid);

uint16_t      page_free_space(const page_t *page);
bool          page_has_space(const page_t *page, uint16_t len);

bool          page_validate(const page_t *page, page_id_t expected_id);

#endif