#ifndef MINIDB_FILE_MANAGER_H
#define MINIDB_FILE_MANAGER_H

#include "page.h"
#include <stdint.h>

typedef struct file_manager file_manager_t;

file_manager_t *fm_open(const char *path);
void            fm_close(file_manager_t *fm);

bool      fm_read_page(file_manager_t *fm, page_id_t pid, void *buf);
bool      fm_write_page(file_manager_t *fm, page_id_t pid, const void *buf);

page_id_t fm_allocate_page(file_manager_t *fm);
uint32_t  fm_num_pages(file_manager_t *fm);

#endif