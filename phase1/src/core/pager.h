#ifndef MINIDB_PAGER_H
#define MINIDB_PAGER_H

#include "page.h"
#include "file_manager.h"

typedef struct pager pager_t;

pager_t *pager_open(const char *path);
void     pager_close(pager_t *pager);

bool      pager_read(pager_t *pager, page_id_t pid, page_t *page);
bool      pager_write(pager_t *pager, page_id_t pid, const page_t *page);
page_id_t pager_allocate(pager_t *pager);
uint32_t  pager_num_pages(pager_t *pager);

#endif