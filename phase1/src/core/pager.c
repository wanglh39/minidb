#include "pager.h"
#include <stdlib.h>

struct pager {
    file_manager_t *fm;
};

pager_t *pager_open(const char *path) {
    file_manager_t *fm = fm_open(path);
    if (!fm) {
        return NULL;
    }

    pager_t *pager = malloc(sizeof(pager_t));
    if (!pager) {
        fm_close(fm);
        return NULL;
    }

    pager->fm = fm;
    return pager;
}

void pager_close(pager_t *pager) {
    if (!pager) return;
    fm_close(pager->fm);
    free(pager);
}

bool pager_read(pager_t *pager, page_id_t pid, page_t *page) {
    return fm_read_page(pager->fm, pid, page->data);
}

bool pager_write(pager_t *pager, page_id_t pid, const page_t *page) {
    return fm_write_page(pager->fm, pid, page->data);
}

page_id_t pager_allocate(pager_t *pager) {
    return fm_allocate_page(pager->fm);
}

uint32_t pager_num_pages(pager_t *pager) {
    return fm_num_pages(pager->fm);
}