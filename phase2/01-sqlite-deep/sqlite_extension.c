/*
 * SQLite C 扩展：自定义 SQL 函数
 *
 * 编译: gcc -shared -fPIC -o sqlite_extension.so sqlite_extension.c -lsqlite3
 * 加载: SELECT load_extension('./sqlite_extension.so');
 * 使用: SELECT sha256_hex('hello');
 *       SELECT regex_match('^user_', 'user_123');
 */
#include <sqlite3ext.h>
SQLITE_EXTENSION_INIT1

#include <string.h>
#include <ctype.h>

/* 简化版 SHA-256 (仅演示，生产应用 OpenSSL) */
static void simple_hash(sqlite3_context *ctx, int argc, sqlite3_value **argv) {
    const unsigned char *text = sqlite3_value_text(argv[0]);
    int len = sqlite3_value_bytes(argv[0]);

    unsigned int hash = 5381;
    for (int i = 0; i < len; i++)
        hash = ((hash << 5) + hash) + text[i];

    char result[16];
    snprintf(result, sizeof(result), "%08x", hash);
    sqlite3_result_text(ctx, result, -1, SQLITE_TRANSIENT);
}

/* 字符串反转函数: reverse(text) -> reversed text */
static void reverse_text(sqlite3_context *ctx, int argc, sqlite3_value **argv) {
    const unsigned char *text = sqlite3_value_text(argv[0]);
    int len = sqlite3_value_bytes(argv[0]);

    char *result = sqlite3_malloc(len + 1);
    if (!result) {
        sqlite3_result_error_nomem(ctx);
        return;
    }

    for (int i = 0; i < len; i++)
        result[i] = text[len - 1 - i];
    result[len] = '\0';

    sqlite3_result_text(ctx, result, len, sqlite3_free);
}

/* 简单前缀匹配: prefix_match(prefix, text) -> 1 or 0 */
static void prefix_match(sqlite3_context *ctx, int argc, sqlite3_value **argv) {
    const unsigned char *prefix = sqlite3_value_text(argv[0]);
    const unsigned char *text = sqlite3_value_text(argv[1]);
    int prefix_len = sqlite3_value_bytes(argv[0]);
    int text_len = sqlite3_value_bytes(argv[1]);

    if (prefix_len > text_len) {
        sqlite3_result_int(ctx, 0);
        return;
    }

    sqlite3_result_int(ctx, memcmp(prefix, text, prefix_len) == 0);
}

/* 聚合函数: moving_average(value, window_size) */
typedef struct {
    double *values;
    int count;
    int capacity;
    int window;
    double sum;
} MovingAvg;

static void moving_avg_step(sqlite3_context *ctx, int argc, sqlite3_value **argv) {
    MovingAvg *ma = sqlite3_aggregate_context(ctx, sizeof(MovingAvg));
    if (!ma) return;

    if (ma->capacity == 0) {
        ma->window = sqlite3_value_int(argv[1]);
        ma->capacity = ma->window;
        ma->values = sqlite3_malloc(ma->capacity * sizeof(double));
    }

    double val = sqlite3_value_double(argv[0]);
    if (ma->count >= ma->window) {
        ma->sum -= ma->values[ma->count % ma->window];
    }
    ma->values[ma->count % ma->window] = val;
    ma->sum += val;
    ma->count++;
}

static void moving_avg_final(sqlite3_context *ctx) {
    MovingAvg *ma = sqlite3_aggregate_context(ctx, 0);
    if (!ma || ma->count == 0) {
        sqlite3_result_null(ctx);
        return;
    }

    int n = ma->count < ma->window ? ma->count : ma->window;
    sqlite3_result_double(ctx, ma->sum / n);

    if (ma->values)
        sqlite3_free(ma->values);
}

int sqlite3_extension_init(sqlite3 *db, char **err, const char *api) {
    SQLITE_EXTENSION_INIT2(api);
    int rc = SQLITE_OK;

    rc = sqlite3_create_function(db, "simple_hash", 1,
                                 SQLITE_UTF8, NULL, simple_hash, NULL, NULL);
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(db, "reverse_text", 1,
                                 SQLITE_UTF8, NULL, reverse_text, NULL, NULL);
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(db, "prefix_match", 2,
                                 SQLITE_UTF8, NULL, prefix_match, NULL, NULL);
    if (rc != SQLITE_OK) return rc;

    rc = sqlite3_create_function(db, "moving_average", 2,
                                 SQLITE_UTF8, NULL, NULL,
                                 moving_avg_step, moving_avg_final);
    return rc;
}