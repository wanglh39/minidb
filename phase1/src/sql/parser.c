#include "parser.h"
#include "lexer.h"
#include <stdlib.h>
#include <string.h>

struct parser {
    lexer_t *lex;
    token_t cur;
};

static void advance(parser_t *p) { p->cur = lexer_next(p->lex); }
static bool is_kw(parser_t *p, keyword_t kw) { return p->cur.type == TOK_KEYWORD && p->cur.keyword == kw; }
static bool accept_kw(parser_t *p, keyword_t kw) { if (is_kw(p, kw)) { advance(p); return true; } return false; }
static bool expect_kw(parser_t *p, keyword_t kw) { if (!accept_kw(p, kw)) return false; return true; }

parser_t *parser_create(const char *sql) {
    parser_t *p = malloc(sizeof(parser_t));
    p->lex = lexer_create(sql);
    p->cur = lexer_next(p->lex);
    return p;
}

void parser_destroy(parser_t *p) {
    lexer_destroy(p->lex);
    free(p);
}

static bool parse_value(parser_t *p, ast_value_t *val) {
    if (p->cur.type == TOK_NUMBER) {
        if (strchr(p->cur.text, '.')) {
            val->type = VAL_FLOAT;
            val->float_val = p->cur.float_val;
        } else {
            val->type = VAL_INT;
            val->int_val = p->cur.int_val;
        }
        advance(p);
        return true;
    }
    if (p->cur.type == TOK_STRING) {
        val->type = VAL_STRING;
        strncpy(val->str_val, p->cur.text, 63);
        advance(p);
        return true;
    }
    if (accept_kw(p, KW_NULL)) {
        val->type = VAL_NULL;
        return true;
    }
    return false;
}

static bool parse_expr(parser_t *p, ast_expr_t *expr) {
    if (p->cur.type != TOK_IDENT) return false;
    expr->type = EXPR_COMPARE;
    strncpy(expr->column, p->cur.text, AST_MAX_NAME - 1);
    advance(p);
    if (p->cur.type != TOK_OP) return false;
    strncpy(expr->op, p->cur.text, 3);
    advance(p);
    return parse_value(p, &expr->value);
}

static void parse_where(parser_t *p, ast_stmt_t *stmt) {
    if (!accept_kw(p, KW_WHERE)) return;
    if (!parse_expr(p, &stmt->where[stmt->num_where])) return;
    stmt->num_where++;
    while (accept_kw(p, KW_AND) && stmt->num_where < AST_MAX_WHERE) {
        if (!parse_expr(p, &stmt->where[stmt->num_where])) break;
        stmt->num_where++;
    }
}

static ast_stmt_t *parse_select(parser_t *p) {
    advance(p);
    ast_stmt_t *s = calloc(1, sizeof(ast_stmt_t));
    s->type = AST_SELECT;

    if (p->cur.type == TOK_OP && p->cur.text[0] == '*') {
        s->select_all = true;
        advance(p);
    } else {
        while (p->cur.type == TOK_IDENT) {
            strncpy(s->columns[s->num_cols++], p->cur.text, AST_MAX_NAME - 1);
            advance(p);
            if (!accept_kw(p, KW_AND) && p->cur.type != TOK_COMMA) break;
            if (p->cur.type == TOK_COMMA) advance(p);
        }
    }

    if (!expect_kw(p, KW_FROM)) { free(s); return NULL; }
    strncpy(s->table, p->cur.text, AST_MAX_NAME - 1);
    advance(p);
    parse_where(p, s);
    return s;
}

static ast_stmt_t *parse_insert(parser_t *p) {
    advance(p);
    expect_kw(p, KW_INTO);
    ast_stmt_t *s = calloc(1, sizeof(ast_stmt_t));
    s->type = AST_INSERT;

    strncpy(s->table, p->cur.text, AST_MAX_NAME - 1);
    advance(p);

    if (p->cur.type == TOK_LPAREN) {
        advance(p);
        while (p->cur.type == TOK_IDENT) {
            strncpy(s->columns[s->num_cols++], p->cur.text, AST_MAX_NAME - 1);
            advance(p);
            if (p->cur.type != TOK_COMMA) break;
            advance(p);
        }
        if (p->cur.type == TOK_RPAREN) advance(p);
    }

    expect_kw(p, KW_VALUES);
    if (p->cur.type == TOK_LPAREN) {
        advance(p);
        while (parse_value(p, &s->values[s->num_values])) {
            s->num_values++;
            if (p->cur.type != TOK_COMMA) break;
            advance(p);
        }
        if (p->cur.type == TOK_RPAREN) advance(p);
    }
    return s;
}

static ast_stmt_t *parse_delete(parser_t *p) {
    advance(p);
    expect_kw(p, KW_FROM);
    ast_stmt_t *s = calloc(1, sizeof(ast_stmt_t));
    s->type = AST_DELETE;
    strncpy(s->table, p->cur.text, AST_MAX_NAME - 1);
    advance(p);
    parse_where(p, s);
    return s;
}

static ast_stmt_t *parse_create(parser_t *p) {
    advance(p);
    expect_kw(p, KW_TABLE);
    ast_stmt_t *s = calloc(1, sizeof(ast_stmt_t));
    s->type = AST_CREATE;

    strncpy(s->table, p->cur.text, AST_MAX_NAME - 1);
    advance(p);

    if (p->cur.type == TOK_LPAREN) {
        advance(p);
        while (p->cur.type == TOK_IDENT) {
            ast_col_def_t *c = &s->col_defs[s->num_col_defs];
            strncpy(c->name, p->cur.text, AST_MAX_NAME - 1);
            advance(p);
            if (accept_kw(p, KW_INT)) c->type = AST_COL_INT32;
            else if (accept_kw(p, KW_FLOAT)) c->type = AST_COL_FLOAT;
            c->nullable = true;
            if (accept_kw(p, KW_PRIMARY)) {
                expect_kw(p, KW_KEY);
                c->primary_key = true;
                c->nullable = false;
            }
            s->num_col_defs++;
            if (p->cur.type != TOK_COMMA) break;
            advance(p);
        }
        if (p->cur.type == TOK_RPAREN) advance(p);
    }
    return s;
}

ast_stmt_t *parser_parse(parser_t *p) {
    if (p->cur.type != TOK_KEYWORD) return NULL;
    switch (p->cur.keyword) {
        case KW_SELECT: return parse_select(p);
        case KW_INSERT: return parse_insert(p);
        case KW_DELETE: return parse_delete(p);
        case KW_CREATE: return parse_create(p);
        default: return NULL;
    }
}