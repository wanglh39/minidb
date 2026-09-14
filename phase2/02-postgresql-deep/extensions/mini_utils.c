/*
 * PostgreSQL C 扩展：mini_utils
 *
 * 编译安装:
 *   gcc -shared -fPIC -I$(pg_config --includedir-server) \
 *       -o mini_utils.so mini_utils.c
 *
 * 加载:
 *   CREATE FUNCTION text_reverse(text) RETURNS text
 *       AS '/path/to/mini_utils' LANGUAGE C STRICT;
 *   SELECT text_reverse('hello');
 */
#include "postgres.h"
#include "fmgr.h"
#include "utils/builtins.h"

PG_MODULE_MAGIC;

/* text_reverse(text) -> text: 反转字符串 */
PG_FUNCTION_INFO_V1(text_reverse);
Datum
text_reverse(PG_FUNCTION_ARGS)
{
    text *t = PG_GETARG_TEXT_PP(0);
    int len = VARSIZE_ANY_EXHDR(t);
    text *result = palloc(VARHDRSZ + len);

    char *src = VARDATA_ANY(t);
    char *dst = VARDATA(result);
    for (int i = 0; i < len; i++)
        dst[i] = src[len - 1 - i];

    SET_VARSIZE(result, VARHDRSZ + len);
    PG_RETURN_TEXT_P(result);
}

/* int_array_sum(int[]) -> int: 数组求和 */
PG_FUNCTION_INFO_V1(int_array_sum);
Datum
int_array_sum(PG_FUNCTION_ARGS)
{
    ArrayType *arr = PG_GETARG_ARRAYTYPE_P(0);
    int32 *values = (int32 *) ARR_DATA_PTR(arr);
    int n = ARR_DIMS(arr)[0];
    int64 sum = 0;

    for (int i = 0; i < n; i++)
        sum += values[i];

    PG_RETURN_INT64(sum);
}

/* is_palindrome(text) -> bool: 判断回文 */
PG_FUNCTION_INFO_V1(is_palindrome);
Datum
is_palindrome(PG_FUNCTION_ARGS)
{
    text *t = PG_GETARG_TEXT_PP(0);
    int len = VARSIZE_ANY_EXHDR(t);
    char *s = VARDATA_ANY(t);

    for (int i = 0; i < len / 2; i++)
        if (s[i] != s[len - 1 - i])
            PG_RETURN_BOOL(false);

    PG_RETURN_BOOL(true);
}