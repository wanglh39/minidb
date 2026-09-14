#include "file_manager.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct file_manager {
    FILE *fp;
    uint32_t num_pages;
};

file_manager_t *fm_open(const char *path) {
    FILE *fp = fopen(path, "r+b");
    if (!fp) {
        fp = fopen(path, "w+b");
        if (!fp) {
            return NULL;
        }
    }

    if (fseek(fp, 0, SEEK_END) != 0) {
        fclose(fp);
        return NULL;
    }
    long size = ftell(fp);
    if (size < 0) {
        fclose(fp);
        return NULL;
    }

    file_manager_t *fm = malloc(sizeof(file_manager_t));
    if (!fm) {
        fclose(fp);
        return NULL;
    }

    fm->fp = fp;
    fm->num_pages = (uint32_t)((size_t)size / PAGE_SIZE);
    return fm;
}

void fm_close(file_manager_t *fm) {
    if (!fm) return;
    if (fm->fp) {
        fclose(fm->fp);
    }
    free(fm);
}

bool fm_read_page(file_manager_t *fm, page_id_t pid, void *buf) {
    if (pid >= fm->num_pages) {
        return false;
    }
    long offset = (long)((size_t)pid * PAGE_SIZE);
    if (fseek(fm->fp, offset, SEEK_SET) != 0) {
        return false;
    }
    size_t n = fread(buf, 1, PAGE_SIZE, fm->fp);
    return n == PAGE_SIZE;
}

bool fm_write_page(file_manager_t *fm, page_id_t pid, const void *buf) {
    if (pid >= fm->num_pages) {
        return false;
    }
    long offset = (long)((size_t)pid * PAGE_SIZE);
    if (fseek(fm->fp, offset, SEEK_SET) != 0) {
        return false;
    }
    size_t n = fwrite(buf, 1, PAGE_SIZE, fm->fp);
    if (n != PAGE_SIZE) {
        return false;
    }
    return fflush(fm->fp) == 0;
}

page_id_t fm_allocate_page(file_manager_t *fm) {
    page_id_t pid = fm->num_pages;
    long offset = (long)((size_t)pid * PAGE_SIZE);
    if (fseek(fm->fp, offset, SEEK_SET) != 0) {
        return INVALID_PAGE_ID;
    }

    static uint8_t zeros[PAGE_SIZE];
    memset(zeros, 0, PAGE_SIZE);

    size_t n = fwrite(zeros, 1, PAGE_SIZE, fm->fp);
    if (n != PAGE_SIZE) {
        return INVALID_PAGE_ID;
    }
    if (fflush(fm->fp) != 0) {
        return INVALID_PAGE_ID;
    }

    fm->num_pages++;
    return pid;
}

uint32_t fm_num_pages(file_manager_t *fm) {
    return fm->num_pages;
}