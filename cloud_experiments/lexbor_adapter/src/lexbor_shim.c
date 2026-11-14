#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "lexbor/core/array_obj.h"
#include "lexbor/core/lexbor.h"
#include "lexbor/dom/interfaces/attr.h"
#include "lexbor/dom/interfaces/character_data.h"
#include "lexbor/dom/interfaces/element.h"
#include "lexbor/dom/interfaces/node.h"
#include "lexbor/encoding/encoding.h"
#include "lexbor/html/encoding.h"
#include "lexbor/html/interfaces/document.h"
#include "lexbor/html/parser.h"
#include "lexbor/html/tokenizer.h"
#include "lexbor/html/tokenizer/error.h"
#include "lexbor/html/tree.h"
#include "lexbor/html/tree/error.h"

enum {
    ATHENA_LEXBOR_EVENT_NODE = 0,
    ATHENA_LEXBOR_EVENT_ATTRIBUTE = 1
};

enum {
    ATHENA_LEXBOR_NODE_OTHER = 0,
    ATHENA_LEXBOR_NODE_ELEMENT = 1,
    ATHENA_LEXBOR_NODE_TEXT = 2
};

enum {
    ATHENA_LEXBOR_ATTR_MINIMAL = 0,
    ATHENA_LEXBOR_ATTR_STAGE_IR = 1
};

enum {
    ATHENA_TRANSCODE_OK = 0,
    ATHENA_TRANSCODE_ERR_ARGS = 1,
    ATHENA_TRANSCODE_ERR_OOM = 2,
    ATHENA_TRANSCODE_ERR_TOO_LARGE = 3,
    ATHENA_TRANSCODE_ERR_CODEC = 4
};

#define ATHENA_LXB_HEADER_MAGIC32 ((uint32_t) 0xA74E4A1EU)
#define ATHENA_LXB_MAX_TRACKED_THREADS 4096U

typedef struct __attribute__((aligned(16))) AthenaAllocHeader {
    size_t byte_size;
    uint32_t magic32;
    uint32_t owner_tid;
} AthenaAllocHeader;

typedef AthenaAllocHeader athena_lxb_alloc_header_t;

_Static_assert(sizeof(AthenaAllocHeader) == 16,
               "Lexbor allocation header must be 16 bytes");
_Static_assert(_Alignof(AthenaAllocHeader) == 16,
               "Lexbor allocation header must preserve 16-byte alignment");

static pthread_once_t g_lxb_mem_once = PTHREAD_ONCE_INIT;
static atomic_uint_fast32_t g_next_lxb_tid = 1U;
static atomic_size_t g_remote_freed_bytes[ATHENA_LXB_MAX_TRACKED_THREADS];

static _Thread_local uint32_t tl_lxb_tid = 0U;
static _Thread_local size_t tl_lxb_alloc_calls = 0U;
static _Thread_local size_t tl_lxb_live_bytes = 0U;
static _Thread_local size_t tl_lxb_peak_bytes = 0U;
static _Thread_local void (*tl_lxb_growth_hook)(size_t c_live) = NULL;

static uint32_t athena_lxb_get_tid(void) {
    if (tl_lxb_tid == 0U) {
        uint32_t assigned = (uint32_t) atomic_fetch_add_explicit(
            &g_next_lxb_tid, 1U, memory_order_relaxed);
        if (assigned == 0U) {
            assigned = 1U;
        }
        tl_lxb_tid = assigned;
    }
    return tl_lxb_tid;
}

static void athena_lxb_reconcile_remote_frees(uint32_t tid) {
    if (tid > 0U && tid < ATHENA_LXB_MAX_TRACKED_THREADS) {
        size_t pending = atomic_load_explicit(
            &g_remote_freed_bytes[tid], memory_order_relaxed);
        if (pending != 0U) {
            pending = atomic_exchange_explicit(
                &g_remote_freed_bytes[tid], 0U, memory_order_acq_rel);
            tl_lxb_live_bytes =
                (tl_lxb_live_bytes > pending) ? (tl_lxb_live_bytes - pending) : 0U;
        }
    }
}

static void athena_lxb_record_growth(uint32_t tid, size_t delta) {
    athena_lxb_reconcile_remote_frees(tid);
    tl_lxb_live_bytes += delta;
    if (tl_lxb_live_bytes > tl_lxb_peak_bytes) {
        tl_lxb_peak_bytes = tl_lxb_live_bytes;
    }
    if (delta > 0U && tl_lxb_growth_hook != NULL) {
        tl_lxb_growth_hook(tl_lxb_live_bytes);
    }
}

static void *athena_lxb_malloc(size_t size) {
    if (size > SIZE_MAX - sizeof(AthenaAllocHeader) - 1U) {
        return NULL;
    }
    uint32_t tid = athena_lxb_get_tid();
    size_t payload = size == 0U ? 1U : size;
    AthenaAllocHeader *hdr =
        (AthenaAllocHeader *) malloc(sizeof(AthenaAllocHeader) + payload);
    if (hdr == NULL) {
        return NULL;
    }
    hdr->byte_size = size;
    hdr->magic32 = ATHENA_LXB_HEADER_MAGIC32;
    hdr->owner_tid = tid;
    tl_lxb_alloc_calls += 1U;
    athena_lxb_record_growth(tid, size);
    return (void *) (hdr + 1);
}

static void *athena_lxb_calloc(size_t num, size_t size) {
    if (num != 0U && size > SIZE_MAX / num) {
        return NULL;
    }
    size_t user_bytes = num * size;
    if (user_bytes > SIZE_MAX - sizeof(AthenaAllocHeader) - 1U) {
        return NULL;
    }
    uint32_t tid = athena_lxb_get_tid();
    size_t payload = user_bytes == 0U ? 1U : user_bytes;
    AthenaAllocHeader *hdr =
        (AthenaAllocHeader *) calloc(1U, sizeof(AthenaAllocHeader) + payload);
    if (hdr == NULL) {
        return NULL;
    }
    hdr->byte_size = user_bytes;
    hdr->magic32 = ATHENA_LXB_HEADER_MAGIC32;
    hdr->owner_tid = tid;
    tl_lxb_alloc_calls += 1U;
    athena_lxb_record_growth(tid, user_bytes);
    return (void *) (hdr + 1);
}

static void athena_lxb_free(void *dst) {
    if (dst == NULL) {
        return;
    }
    AthenaAllocHeader *hdr = ((AthenaAllocHeader *) dst) - 1;
    if (hdr->magic32 != ATHENA_LXB_HEADER_MAGIC32) {
        abort();
    }
    size_t sz = hdr->byte_size;
    uint32_t owner = hdr->owner_tid;
    hdr->magic32 = 0U;
    free(hdr);

    if (tl_lxb_tid != 0U && owner == tl_lxb_tid) {
        athena_lxb_reconcile_remote_frees(tl_lxb_tid);
        tl_lxb_live_bytes = (tl_lxb_live_bytes > sz) ? (tl_lxb_live_bytes - sz) : 0U;
    } else if (owner > 0U && owner < ATHENA_LXB_MAX_TRACKED_THREADS) {
        atomic_fetch_add_explicit(&g_remote_freed_bytes[owner], sz, memory_order_release);
    }
}

static void *athena_lxb_realloc(void *dst, size_t size) {
    if (dst == NULL) {
        return athena_lxb_malloc(size);
    }
    if (size > SIZE_MAX - sizeof(AthenaAllocHeader) - 1U) {
        return NULL;
    }
    AthenaAllocHeader *old_hdr = ((AthenaAllocHeader *) dst) - 1;
    if (old_hdr->magic32 != ATHENA_LXB_HEADER_MAGIC32) {
        abort();
    }
    size_t old_size = old_hdr->byte_size;
    uint32_t old_owner = old_hdr->owner_tid;
    uint32_t tid = athena_lxb_get_tid();
    size_t payload = size == 0U ? 1U : size;

    old_hdr->magic32 = 0U;
    AthenaAllocHeader *new_hdr = (AthenaAllocHeader *) realloc(
        old_hdr, sizeof(AthenaAllocHeader) + payload);
    if (new_hdr == NULL) {
        old_hdr->magic32 = ATHENA_LXB_HEADER_MAGIC32;
        return NULL;
    }
    new_hdr->byte_size = size;
    new_hdr->magic32 = ATHENA_LXB_HEADER_MAGIC32;
    new_hdr->owner_tid = tid;
    tl_lxb_alloc_calls += 1U;

    if (old_owner == tid) {
        athena_lxb_reconcile_remote_frees(tid);
        if (size >= old_size) {
            athena_lxb_record_growth(tid, size - old_size);
        } else {
            size_t shrink = old_size - size;
            tl_lxb_live_bytes =
                (tl_lxb_live_bytes > shrink) ? (tl_lxb_live_bytes - shrink) : 0U;
        }
    } else {
        if (old_owner > 0U && old_owner < ATHENA_LXB_MAX_TRACKED_THREADS) {
            atomic_fetch_add_explicit(
                &g_remote_freed_bytes[old_owner], old_size, memory_order_release);
        }
        athena_lxb_record_growth(tid, size);
    }
    return (void *) (new_hdr + 1);
}

static void athena_lxb_install_hooks_once(void) {
    lxb_status_t st = lexbor_memory_setup(
        athena_lxb_malloc,
        athena_lxb_realloc,
        athena_lxb_calloc,
        athena_lxb_free);
    if (st != LXB_STATUS_OK) {
        abort();
    }
}

size_t athena_lxb_current_live_bytes(void) {
    if (tl_lxb_tid != 0U) {
        athena_lxb_reconcile_remote_frees(tl_lxb_tid);
    }
    return tl_lxb_live_bytes;
}

void athena_lxb_set_growth_hook(void (*fn)(size_t c_live)) {
    tl_lxb_growth_hook = fn;
}

void athena_lexbor_reset_and_enable_metering(
    size_t *alloc_calls_out,
    size_t *live_bytes_out,
    size_t *peak_bytes_out)
{
    pthread_once(&g_lxb_mem_once, athena_lxb_install_hooks_once);
    uint32_t tid = athena_lxb_get_tid();
    athena_lxb_reconcile_remote_frees(tid);
    tl_lxb_alloc_calls = 0U;
    tl_lxb_peak_bytes = tl_lxb_live_bytes;
    if (alloc_calls_out != NULL) {
        *alloc_calls_out = 0U;
    }
    if (live_bytes_out != NULL) {
        *live_bytes_out = tl_lxb_live_bytes;
    }
    if (peak_bytes_out != NULL) {
        *peak_bytes_out = tl_lxb_peak_bytes;
    }
}

void athena_lexbor_read_heap_counters(
    size_t *alloc_calls_out,
    size_t *live_bytes_out,
    size_t *peak_bytes_out)
{
    pthread_once(&g_lxb_mem_once, athena_lxb_install_hooks_once);
    if (tl_lxb_tid != 0U) {
        athena_lxb_reconcile_remote_frees(tl_lxb_tid);
    }
    if (alloc_calls_out != NULL) {
        *alloc_calls_out = tl_lxb_alloc_calls;
    }
    if (live_bytes_out != NULL) {
        *live_bytes_out = tl_lxb_live_bytes;
    }
    if (peak_bytes_out != NULL) {
        *peak_bytes_out = tl_lxb_peak_bytes;
    }
}

typedef int (*athena_lexbor_node_callback)(
    void *context,
    const void *document_node,
    const void *node,
    const void *parent,
    uint8_t event_kind,
    uint8_t kind,
    const unsigned char *name,
    size_t name_len,
    const unsigned char *text,
    size_t text_len);

typedef int (*athena_lexbor_diagnostic_callback)(
    void *context,
    uint8_t source,
    size_t error_id,
    const unsigned char *description,
    size_t description_len,
    uintptr_t start_address,
    uintptr_t end_address);

typedef struct {
    const lxb_dom_node_t *document_node;
    athena_lexbor_node_callback callback;
    void *context;
    uint8_t attr_profile;
    int stopped;
} athena_walk_context_t;

static unsigned char athena_ascii_lower(unsigned char ch) {
    if (ch >= 'A' && ch <= 'Z') {
        return (unsigned char) (ch + ('a' - 'A'));
    }
    return ch;
}

static int athena_is_css_ws(unsigned char ch) {
    return ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r' || ch == '\f';
}

static int athena_ascii_case_equal(
    const unsigned char *lhs,
    size_t lhs_len,
    const char *rhs,
    size_t rhs_len)
{
    if (lhs_len != rhs_len || lhs == NULL) {
        return 0;
    }
    for (size_t i = 0; i < lhs_len; i++) {
        if (athena_ascii_lower(lhs[i]) != (unsigned char) rhs[i]) {
            return 0;
        }
    }
    return 1;
}

#define ATHENA_STAGE_IR_CSS_PROPS 16
#define ATHENA_MAX_CSS_VAL_LEN 160

static const char *const k_stage_ir_css_props[ATHENA_STAGE_IR_CSS_PROPS] = {
    "color",
    "background-color",
    "background",
    "left",
    "top",
    "text-indent",
    "width",
    "height",
    "overflow",
    "opacity",
    "font-size",
    "visibility",
    "display",
    "clip",
    "clip-path",
    "transform"
};

static int athena_match_stage_ir_css_prop(const unsigned char *name, size_t len) {
    for (int i = 0; i < ATHENA_STAGE_IR_CSS_PROPS; i++) {
        size_t plen = strlen(k_stage_ir_css_props[i]);
        if (athena_ascii_case_equal(name, len, k_stage_ir_css_props[i], plen)) {
            return i;
        }
    }
    return -1;
}

static int athena_ascii_prefix_ci(
    const unsigned char *p,
    const unsigned char *end,
    const char *lit)
{
    size_t n = strlen(lit);
    if ((size_t) (end - p) < n) {
        return 0;
    }
    for (size_t i = 0; i < n; i++) {
        if (athena_ascii_lower(p[i]) != (unsigned char) lit[i]) {
            return 0;
        }
    }
    return 1;
}

static const unsigned char *athena_skip_balanced_parens(
    const unsigned char *p,
    const unsigned char *end)
{
    int depth = 0;
    unsigned char quote = 0;
    while (p < end) {
        unsigned char ch = *p++;
        if (quote != 0) {
            if (ch == '\\' && p < end) {
                p++;
            } else if (ch == quote) {
                quote = 0;
            }
            continue;
        }
        if (ch == '\'' || ch == '"') {
            quote = ch;
        } else if (ch == '(') {
            depth++;
        } else if (ch == ')') {
            depth--;
            if (depth <= 0) {
                break;
            }
        }
    }
    return p;
}

static size_t athena_normalize_css_decl_value(
    int prop_idx,
    const unsigned char *raw_start,
    const unsigned char *raw_end,
    int *out_important,
    unsigned char out_val[ATHENA_MAX_CSS_VAL_LEN + 1])
{
    *out_important = 0;
    unsigned char cleaned[ATHENA_MAX_CSS_VAL_LEN + 32];
    size_t clen = 0;
    const unsigned char *p = raw_start;
    unsigned char quote = 0;

    while (p < raw_end) {
        if (quote == 0 && (raw_end - p) >= 2 && p[0] == '/' && p[1] == '*') {
            p += 2;
            while ((raw_end - p) >= 2 && !(p[0] == '*' && p[1] == '/')) {
                p++;
            }
            p = ((raw_end - p) >= 2) ? (p + 2) : raw_end;
            if (clen > 0 && cleaned[clen - 1] != ' ') {
                if (clen >= sizeof(cleaned)) {
                    return 0;
                }
                cleaned[clen++] = ' ';
            }
            continue;
        }

        if (quote == 0) {
            int is_prev_ident = (p > raw_start)
                && ((p[-1] >= 'a' && p[-1] <= 'z')
                    || (p[-1] >= 'A' && p[-1] <= 'Z')
                    || (p[-1] >= '0' && p[-1] <= '9')
                    || p[-1] == '-' || p[-1] == '_');
            if (!is_prev_ident && athena_ascii_prefix_ci(p, raw_end, "url(")) {
                p = athena_skip_balanced_parens(p + 3, raw_end);
                continue;
            }
            if (!is_prev_ident && athena_ascii_prefix_ci(p, raw_end, "image-set(")) {
                p = athena_skip_balanced_parens(p + 9, raw_end);
                continue;
            }
            if (!is_prev_ident && athena_ascii_prefix_ci(p, raw_end, "-webkit-image-set(")) {
                p = athena_skip_balanced_parens(p + 17, raw_end);
                continue;
            }
        }

        unsigned char ch = *p++;
        if (quote != 0) {
            if (clen >= sizeof(cleaned)) {
                return 0;
            }
            cleaned[clen++] = ch;
            if (ch == '\\' && p < raw_end) {
                if (clen >= sizeof(cleaned)) {
                    return 0;
                }
                cleaned[clen++] = *p++;
            } else if (ch == quote) {
                quote = 0;
            }
            continue;
        }

        if (ch == '\'' || ch == '"') {
            quote = ch;
            if (clen >= sizeof(cleaned)) {
                return 0;
            }
            cleaned[clen++] = ch;
            continue;
        }

        if (athena_is_css_ws(ch)) {
            if (clen > 0 && cleaned[clen - 1] != ' '
                && cleaned[clen - 1] != '(' && cleaned[clen - 1] != ',')
            {
                if (clen >= sizeof(cleaned)) {
                    return 0;
                }
                cleaned[clen++] = ' ';
            }
            continue;
        }

        if ((ch == ')' || ch == ',') && clen > 0 && cleaned[clen - 1] == ' ') {
            clen--;
        }
        if (clen >= sizeof(cleaned)) {
            return 0;
        }
        cleaned[clen++] = athena_ascii_lower(ch);
    }

    while (clen > 0 && cleaned[clen - 1] == ' ') {
        clen--;
    }

    if (clen >= 10 && memcmp(cleaned + clen - 9, "important", 9) == 0) {
        size_t bang_pos = clen - 9;
        while (bang_pos > 0 && cleaned[bang_pos - 1] == ' ') {
            bang_pos--;
        }
        if (bang_pos > 0 && cleaned[bang_pos - 1] == '!') {
            *out_important = 1;
            clen = bang_pos - 1;
            while (clen > 0 && cleaned[clen - 1] == ' ') {
                clen--;
            }
        }
    }

    if (clen == 0 || clen > ATHENA_MAX_CSS_VAL_LEN) {
        return 0;
    }

    if (strcmp(k_stage_ir_css_props[prop_idx], "opacity") == 0) {
        int all_zero_frac = 0;
        if ((clen >= 2 && cleaned[0] == '.') || (clen >= 3 && cleaned[0] == '0' && cleaned[1] == '.')) {
            size_t start_z = (cleaned[0] == '.') ? 1U : 2U;
            all_zero_frac = (start_z < clen);
            for (size_t k = start_z; k < clen; k++) {
                if (cleaned[k] != '0') {
                    all_zero_frac = 0;
                    break;
                }
            }
        }
        if (all_zero_frac) {
            out_val[0] = '0';
            out_val[1] = '\0';
            return 1U;
        }
        if (clen >= 3 && cleaned[0] == '1' && cleaned[1] == '.') {
            int all_zero = 1;
            for (size_t k = 2U; k < clen; k++) {
                if (cleaned[k] != '0') {
                    all_zero = 0;
                    break;
                }
            }
            if (all_zero) {
                out_val[0] = '1';
                out_val[1] = '\0';
                return 1U;
            }
        }
    }

    memcpy(out_val, cleaned, clen);
    out_val[clen] = '\0';
    return clen;
}

typedef struct {
    int prop_idx;
    int important;
    size_t val_len;
    unsigned char val[ATHENA_MAX_CSS_VAL_LEN + 1];
} athena_css_decl_entry_t;

static size_t athena_distill_style_tokens(
    const unsigned char *style,
    size_t style_len,
    unsigned char *out_buf,
    size_t out_cap)
{
    if (style == NULL || style_len == 0 || out_cap == 0) {
        return 0;
    }

    athena_css_decl_entry_t entries[ATHENA_STAGE_IR_CSS_PROPS];
    size_t entry_count = 0;

    const unsigned char *cur = style;
    const unsigned char *end = style + style_len;

    while (cur < end) {
        const unsigned char *decl_start = cur;
        int paren_depth = 0;
        unsigned char quote = 0;
        const unsigned char *colon = NULL;

        while (cur < end) {
            if (quote == 0 && (end - cur) >= 2 && cur[0] == '/' && cur[1] == '*') {
                cur += 2;
                while ((end - cur) >= 2 && !(cur[0] == '*' && cur[1] == '/')) {
                    cur++;
                }
                cur = ((end - cur) >= 2) ? (cur + 2) : end;
                continue;
            }
            unsigned char ch = *cur;
            if (quote != 0) {
                if (ch == '\\' && (cur + 1) < end) {
                    cur += 2;
                    continue;
                }
                if (ch == quote) {
                    quote = 0;
                }
                cur++;
                continue;
            }
            if (ch == '\'' || ch == '"') {
                quote = ch;
            } else if (ch == '(') {
                paren_depth++;
            } else if (ch == ')' && paren_depth > 0) {
                paren_depth--;
            } else if (ch == ':' && paren_depth == 0 && colon == NULL) {
                colon = cur;
            } else if (ch == ';' && paren_depth == 0) {
                break;
            }
            cur++;
        }

        const unsigned char *decl_end = cur;
        if (cur < end && *cur == ';') {
            cur++;
        }

        if (colon == NULL) {
            continue;
        }

        const unsigned char *p_start = decl_start;
        const unsigned char *p_end = colon;
        while (p_start < p_end) {
            if (athena_is_css_ws(*p_start)) {
                p_start++;
            } else if ((p_end - p_start) >= 2 && p_start[0] == '/' && p_start[1] == '*') {
                p_start += 2;
                while ((p_end - p_start) >= 2 && !(p_start[0] == '*' && p_start[1] == '/')) {
                    p_start++;
                }
                p_start = ((p_end - p_start) >= 2) ? (p_start + 2) : p_end;
            } else {
                break;
            }
        }
        while (p_end > p_start && athena_is_css_ws(p_end[-1])) {
            p_end--;
        }

        int prop_idx = athena_match_stage_ir_css_prop(p_start, (size_t) (p_end - p_start));
        if (prop_idx < 0) {
            continue;
        }

        unsigned char norm_val[ATHENA_MAX_CSS_VAL_LEN + 1];
        int is_important = 0;
        size_t norm_len = athena_normalize_css_decl_value(
            prop_idx, colon + 1, decl_end, &is_important, norm_val);
        if (norm_len == 0) {
            continue;
        }

        size_t existing_pos = entry_count;
        for (size_t i = 0; i < entry_count; i++) {
            if (entries[i].prop_idx == prop_idx) {
                existing_pos = i;
                break;
            }
        }
        if (existing_pos < entry_count) {
            if (entries[existing_pos].important && !is_important) {
                continue;
            }
            entries[existing_pos].important = is_important;
            entries[existing_pos].val_len = norm_len;
            memcpy(entries[existing_pos].val, norm_val, norm_len + 1U);
        } else if (entry_count < ATHENA_STAGE_IR_CSS_PROPS) {
            entries[entry_count].prop_idx = prop_idx;
            entries[entry_count].important = is_important;
            entries[entry_count].val_len = norm_len;
            memcpy(entries[entry_count].val, norm_val, norm_len + 1U);
            entry_count++;
        }
    }

    size_t written = 0;
    for (size_t i = 0; i < entry_count; i++) {
        const char *pname = k_stage_ir_css_props[entries[i].prop_idx];
        size_t plen = strlen(pname);
        size_t need = (written > 0 ? 1U : 0U) + plen + 1U + entries[i].val_len;
        if (written + need > out_cap) {
            break;
        }
        if (written > 0) {
            out_buf[written++] = ';';
        }
        memcpy(out_buf + written, pname, plen);
        written += plen;
        out_buf[written++] = ':';
        memcpy(out_buf + written, entries[i].val, entries[i].val_len);
        written += entries[i].val_len;
    }
    return written;
}

static int athena_minimal_attribute(const lxb_char_t *name, size_t name_len) {
    return athena_ascii_case_equal(name, name_len, "href", 4)
        || athena_ascii_case_equal(name, name_len, "id", 2)
        || athena_ascii_case_equal(name, name_len, "rowspan", 7)
        || athena_ascii_case_equal(name, name_len, "colspan", 7)
        || athena_ascii_case_equal(name, name_len, "headers", 7)
        || athena_ascii_case_equal(name, name_len, "scope", 5)
        || athena_ascii_case_equal(name, name_len, "data-unit", 9)
        || athena_ascii_case_equal(name, name_len, "abbr", 4);
}

static int athena_stage_ir_attribute(const lxb_char_t *name, size_t name_len) {
    return athena_minimal_attribute(name, name_len)
        || athena_ascii_case_equal(name, name_len, "style", 5)
        || athena_ascii_case_equal(name, name_len, "alttext", 7)
        || athena_ascii_case_equal(name, name_len, "display", 7)
        || athena_ascii_case_equal(name, name_len, "notation", 8)
        || athena_ascii_case_equal(name, name_len, "data-mw", 7)
        || athena_ascii_case_equal(name, name_len, "data-latex", 10)
        || athena_ascii_case_equal(name, name_len, "data-tex", 8)
        || athena_ascii_case_equal(name, name_len, "data-lang", 9)
        || athena_ascii_case_equal(name, name_len, "data-language", 13)
        || athena_ascii_case_equal(name, name_len, "rel", 3)
        || athena_ascii_case_equal(name, name_len, "src", 3)
        || athena_ascii_case_equal(name, name_len, "alt", 3)
        || athena_ascii_case_equal(name, name_len, "class", 5)
        || athena_ascii_case_equal(name, name_len, "title", 5)
        || athena_ascii_case_equal(name, name_len, "lang", 4)
        || athena_ascii_case_equal(name, name_len, "dir", 3)
        || athena_ascii_case_equal(name, name_len, "role", 4)
        || athena_ascii_case_equal(name, name_len, "aria-hidden", 11)
        || athena_ascii_case_equal(name, name_len, "aria-label", 10)
        || athena_ascii_case_equal(name, name_len, "hidden", 6)
        || athena_ascii_case_equal(name, name_len, "type", 4)
        || athena_ascii_case_equal(name, name_len, "encoding", 8);
}

static lexbor_action_t athena_walk_node(lxb_dom_node_t *node, void *opaque) {
    athena_walk_context_t *context = (athena_walk_context_t *) opaque;
    uint8_t kind = ATHENA_LEXBOR_NODE_OTHER;
    const lxb_char_t *name = NULL;
    size_t name_len = 0;
    const lxb_char_t *text = NULL;
    size_t text_len = 0;
    lxb_dom_element_t *element = NULL;

    if (node->type == LXB_DOM_NODE_TYPE_ELEMENT) {
        kind = ATHENA_LEXBOR_NODE_ELEMENT;
        name = lxb_dom_node_name(node, &name_len);
        element = lxb_dom_interface_element(node);
    } else if (node->type == LXB_DOM_NODE_TYPE_TEXT) {
        const lxb_dom_character_data_t *character_data =
            (const lxb_dom_character_data_t *) node;
        kind = ATHENA_LEXBOR_NODE_TEXT;
        text = character_data->data.data;
        text_len = character_data->data.length;
    }

    if (!context->callback(
            context->context,
            context->document_node,
            node,
            node->parent,
            ATHENA_LEXBOR_EVENT_NODE,
            kind,
            name,
            name_len,
            text,
            text_len))
    {
        context->stopped = 1;
        return LEXBOR_ACTION_STOP;
    }

    if (element != NULL) {
        for (lxb_dom_attr_t *attribute = element->first_attr;
             attribute != NULL;
             attribute = attribute->next)
        {
            size_t attribute_name_len = 0;
            size_t attribute_value_len = 0;
            const lxb_char_t *attribute_name =
                lxb_dom_attr_qualified_name(attribute, &attribute_name_len);
            const lxb_char_t *attribute_value =
                lxb_dom_attr_value(attribute, &attribute_value_len);

            unsigned char distilled_style[3072];
            if (context->attr_profile == ATHENA_LEXBOR_ATTR_STAGE_IR) {
                if (!athena_stage_ir_attribute(attribute_name, attribute_name_len)) {
                    continue;
                }
                if (athena_ascii_case_equal(attribute_name, attribute_name_len, "style", 5)) {
                    size_t distilled_len = athena_distill_style_tokens(
                        attribute_value,
                        attribute_value_len,
                        distilled_style,
                        sizeof(distilled_style));
                    if (distilled_len == 0) {
                        continue;
                    }
                    attribute_value = distilled_style;
                    attribute_value_len = distilled_len;
                }
            } else {
                if (!athena_minimal_attribute(attribute_name, attribute_name_len)) {
                    continue;
                }
            }

            if (!context->callback(
                    context->context,
                    context->document_node,
                    node,
                    node->parent,
                    ATHENA_LEXBOR_EVENT_ATTRIBUTE,
                    ATHENA_LEXBOR_NODE_ELEMENT,
                    attribute_name,
                    attribute_name_len,
                    attribute_value,
                    attribute_value_len))
            {
                context->stopped = 1;
                return LEXBOR_ACTION_STOP;
            }
        }
    }
    return LEXBOR_ACTION_OK;
}

static int athena_emit_tree_diagnostics(
    lexbor_array_obj_t *errors,
    athena_lexbor_diagnostic_callback callback,
    void *context)
{
    if (errors == NULL) {
        return 0;
    }

    for (size_t index = 0; index < errors->length; index++) {
        const lxb_html_tree_error_t *error =
            (const lxb_html_tree_error_t *) lexbor_array_obj_get(errors, index);
        if (error == NULL) {
            return -1;
        }

        size_t description_len = 0;
        const lxb_char_t *description =
            lxb_html_tree_error_to_string(error->id, &description_len);
        if (!callback(
                context,
                0,
                (size_t) error->id,
                description,
                description_len,
                (uintptr_t) error->begin,
                (uintptr_t) error->end))
        {
            return 1;
        }
    }
    return 0;
}

static int athena_emit_tokenizer_diagnostics(
    lexbor_array_obj_t *errors,
    athena_lexbor_diagnostic_callback callback,
    void *context)
{
    if (errors == NULL) {
        return 0;
    }

    for (size_t index = 0; index < errors->length; index++) {
        const lxb_html_tokenizer_error_t *error =
            (const lxb_html_tokenizer_error_t *) lexbor_array_obj_get(errors, index);
        if (error == NULL) {
            return -1;
        }

        size_t description_len = 0;
        const lxb_char_t *description =
            lxb_html_tokenizer_error_to_string(error->id, &description_len);
        if (!callback(
                context,
                1,
                (size_t) error->id,
                description,
                description_len,
                (uintptr_t) error->pos,
                (uintptr_t) error->pos))
        {
            return 1;
        }
    }
    return 0;
}

static int athena_is_strict_utf8(const unsigned char *s, size_t len) {
    size_t i = 0;
    while (i < len) {
        unsigned char b0 = s[i];
        if (b0 < 0x80U) {
            i += 1U;
        } else if (b0 >= 0xC2U && b0 <= 0xDFU) {
            if (i + 1U >= len || (s[i + 1U] & 0xC0U) != 0x80U) {
                return 0;
            }
            i += 2U;
        } else if (b0 == 0xE0U) {
            if (i + 2U >= len || s[i + 1U] < 0xA0U || s[i + 1U] > 0xBFU
                || (s[i + 2U] & 0xC0U) != 0x80U)
            {
                return 0;
            }
            i += 3U;
        } else if ((b0 >= 0xE1U && b0 <= 0xECU) || (b0 >= 0xEEU && b0 <= 0xEFU)) {
            if (i + 2U >= len || (s[i + 1U] & 0xC0U) != 0x80U
                || (s[i + 2U] & 0xC0U) != 0x80U)
            {
                return 0;
            }
            i += 3U;
        } else if (b0 == 0xEDU) {
            if (i + 2U >= len || s[i + 1U] < 0x80U || s[i + 1U] > 0x9FU
                || (s[i + 2U] & 0xC0U) != 0x80U)
            {
                return 0;
            }
            i += 3U;
        } else if (b0 == 0xF0U) {
            if (i + 3U >= len || s[i + 1U] < 0x90U || s[i + 1U] > 0xBFU
                || (s[i + 2U] & 0xC0U) != 0x80U || (s[i + 3U] & 0xC0U) != 0x80U)
            {
                return 0;
            }
            i += 4U;
        } else if (b0 >= 0xF1U && b0 <= 0xF3U) {
            if (i + 3U >= len || (s[i + 1U] & 0xC0U) != 0x80U
                || (s[i + 2U] & 0xC0U) != 0x80U || (s[i + 3U] & 0xC0U) != 0x80U)
            {
                return 0;
            }
            i += 4U;
        } else if (b0 == 0xF4U) {
            if (i + 3U >= len || s[i + 1U] < 0x80U || s[i + 1U] > 0x8FU
                || (s[i + 2U] & 0xC0U) != 0x80U || (s[i + 3U] & 0xC0U) != 0x80U)
            {
                return 0;
            }
            i += 4U;
        } else {
            return 0;
        }
    }
    return 1;
}

static const lxb_encoding_data_t *athena_sniff_http_header_encoding(
    const unsigned char *headers,
    size_t headers_len)
{
    if (headers == NULL || headers_len == 0) {
        return NULL;
    }
    const unsigned char *cur = headers;
    const unsigned char *end = headers + headers_len;

    while (cur < end) {
        const unsigned char *line_end = cur;
        while (line_end < end && *line_end != '\n') {
            line_end++;
        }
        const unsigned char *trimmed_end = line_end;
        if (trimmed_end > cur && trimmed_end[-1] == '\r') {
            trimmed_end--;
        }
        const unsigned char *colon = cur;
        while (colon < trimmed_end && *colon != ':') {
            colon++;
        }
        if (colon < trimmed_end) {
            const unsigned char *name_start = cur;
            const unsigned char *name_end = colon;
            while (name_start < name_end && athena_is_css_ws(*name_start)) {
                name_start++;
            }
            while (name_end > name_start && athena_is_css_ws(name_end[-1])) {
                name_end--;
            }
            if (athena_ascii_case_equal(
                    name_start, (size_t) (name_end - name_start), "content-type", 12))
            {
                const lxb_char_t *cs_end = NULL;
                const lxb_char_t *cs_name = lxb_html_encoding_content(
                    (const lxb_char_t *) (colon + 1),
                    (const lxb_char_t *) trimmed_end,
                    &cs_end);
                if (cs_name != NULL && cs_end > cs_name) {
                    const lxb_encoding_data_t *enc = lxb_encoding_data_by_pre_name(
                        cs_name, (size_t) (cs_end - cs_name));
                    if (enc != NULL && enc->encoding != LXB_ENCODING_UNDEFINED) {
                        return enc;
                    }
                }
            }
        }
        cur = (line_end < end) ? (line_end + 1) : end;
    }
    return NULL;
}

static int athena_append_codepoints_utf8(
    const lxb_encoding_data_t *utf8_enc,
    const lxb_codepoint_t *cps,
    size_t cp_count,
    size_t max_output_bytes,
    unsigned char **out_buf,
    size_t *out_len,
    size_t *out_cap)
{
    const lxb_codepoint_t *cp_ptr = cps;
    const lxb_codepoint_t *cp_end = cps + cp_count;

    while (cp_ptr < cp_end) {
        size_t remaining_cps = (size_t) (cp_end - cp_ptr);
        if (*out_cap - *out_len < 16U) {
            if (*out_len >= max_output_bytes) {
                return ATHENA_TRANSCODE_ERR_TOO_LARGE;
            }
            size_t next_cap = (*out_cap) * 2U;
            size_t needed = *out_len + remaining_cps * 4U + 16U;
            if (next_cap < needed) {
                next_cap = needed;
            }
            size_t ceiling = (max_output_bytes <= SIZE_MAX - 32U)
                ? (max_output_bytes + 32U)
                : SIZE_MAX;
            if (next_cap > ceiling) {
                next_cap = ceiling;
            }
            if (next_cap <= *out_cap) {
                return ATHENA_TRANSCODE_ERR_TOO_LARGE;
            }
            unsigned char *grown = (unsigned char *) athena_lxb_realloc(*out_buf, next_cap);
            if (grown == NULL) {
                return ATHENA_TRANSCODE_ERR_OOM;
            }
            *out_buf = grown;
            *out_cap = next_cap;
        }

        lxb_encoding_encode_t enc_ctx;
        if (lxb_encoding_encode_init(
                &enc_ctx,
                utf8_enc,
                (lxb_char_t *) (*out_buf + *out_len),
                *out_cap - *out_len) != LXB_STATUS_OK)
        {
            return ATHENA_TRANSCODE_ERR_CODEC;
        }
        if (lxb_encoding_encode_replace_set(
                &enc_ctx,
                LXB_ENCODING_REPLACEMENT_BYTES,
                LXB_ENCODING_REPLACEMENT_SIZE) != LXB_STATUS_OK)
        {
            return ATHENA_TRANSCODE_ERR_CODEC;
        }

        const lxb_codepoint_t *before = cp_ptr;
        lxb_status_t es = utf8_enc->encode(&enc_ctx, &cp_ptr, cp_end);
        size_t written = lxb_encoding_encode_buf_used(&enc_ctx);
        *out_len += written;
        if (*out_len > max_output_bytes) {
            return ATHENA_TRANSCODE_ERR_TOO_LARGE;
        }
        if (es == LXB_STATUS_SMALL_BUFFER) {
            if (cp_ptr == before && written == 0U && (*out_len + 4U > max_output_bytes)) {
                return ATHENA_TRANSCODE_ERR_TOO_LARGE;
            }
            size_t next_cap = (*out_cap) * 2U;
            size_t ceiling = (max_output_bytes <= SIZE_MAX - 32U)
                ? (max_output_bytes + 32U)
                : SIZE_MAX;
            if (next_cap > ceiling) {
                next_cap = ceiling;
            }
            if (next_cap <= *out_cap) {
                return ATHENA_TRANSCODE_ERR_TOO_LARGE;
            }
            unsigned char *grown = (unsigned char *) athena_lxb_realloc(*out_buf, next_cap);
            if (grown == NULL) {
                return ATHENA_TRANSCODE_ERR_OOM;
            }
            *out_buf = grown;
            *out_cap = next_cap;
            continue;
        }
        if (es != LXB_STATUS_OK) {
            return ATHENA_TRANSCODE_ERR_CODEC;
        }
    }
    return ATHENA_TRANSCODE_OK;
}

void athena_lexbor_free_buffer(unsigned char *buf) {
    athena_lxb_free(buf);
}

int athena_lexbor_transcode_whatwg_utf8(
    const unsigned char *raw,
    size_t raw_len,
    const unsigned char *headers,
    size_t headers_len,
    size_t max_output_bytes,
    unsigned char **utf8_out,
    size_t *utf8_len_out)
{
    pthread_once(&g_lxb_mem_once, athena_lxb_install_hooks_once);

    if (utf8_out == NULL || utf8_len_out == NULL || (raw == NULL && raw_len != 0U)) {
        return ATHENA_TRANSCODE_ERR_ARGS;
    }
    *utf8_out = NULL;
    *utf8_len_out = 0U;

    if (raw_len > max_output_bytes) {
        return ATHENA_TRANSCODE_ERR_TOO_LARGE;
    }
    if (raw_len == 0U) {
        unsigned char *empty = (unsigned char *) athena_lxb_malloc(1U);
        if (empty == NULL) {
            return ATHENA_TRANSCODE_ERR_OOM;
        }
        empty[0] = '\0';
        *utf8_out = empty;
        *utf8_len_out = 0U;
        return ATHENA_TRANSCODE_OK;
    }

    const unsigned char *data = raw;
    const unsigned char *end = raw + raw_len;
    const lxb_encoding_data_t *enc_data = NULL;

    if (raw_len >= 3U && raw[0] == 0xEFU && raw[1] == 0xBBU && raw[2] == 0xBFU) {
        enc_data = lxb_encoding_data(LXB_ENCODING_UTF_8);
        data += 3U;
    } else if (raw_len >= 2U && raw[0] == 0xFEU && raw[1] == 0xFFU) {
        enc_data = lxb_encoding_data(LXB_ENCODING_UTF_16BE);
        data += 2U;
    } else if (raw_len >= 2U && raw[0] == 0xFFU && raw[1] == 0xFEU) {
        enc_data = lxb_encoding_data(LXB_ENCODING_UTF_16LE);
        data += 2U;
    }

    if (enc_data == NULL) {
        enc_data = athena_sniff_http_header_encoding(headers, headers_len);
    }

    if (enc_data == NULL) {
        lxb_html_encoding_t em;
        if (lxb_html_encoding_init(&em) == LXB_STATUS_OK) {
            size_t rem = (size_t) (end - data);
            size_t scan_len = rem < 4096U ? rem : 4096U;
            if (lxb_html_encoding_determine(
                    &em,
                    (const lxb_char_t *) data,
                    (const lxb_char_t *) (data + scan_len)) == LXB_STATUS_OK)
            {
                size_t meta_len = lxb_html_encoding_meta_length(&em);
                for (size_t i = 0; i < meta_len; i++) {
                    lxb_html_encoding_entry_t *entry = lxb_html_encoding_meta_entry(&em, i);
                    if (entry != NULL && entry->name != NULL && entry->end > entry->name) {
                        const lxb_encoding_data_t *cand = lxb_encoding_data_prescan_validate(
                            entry->name, (size_t) (entry->end - entry->name));
                        if (cand != NULL && cand->encoding != LXB_ENCODING_UNDEFINED) {
                            enc_data = cand;
                            break;
                        }
                    }
                }
            }
            lxb_html_encoding_destroy(&em, false);
        }
    }

    if (enc_data == NULL) {
        if (athena_is_strict_utf8(data, (size_t) (end - data))) {
            size_t copy_len = (size_t) (end - data);
            unsigned char *buf = (unsigned char *) athena_lxb_malloc(copy_len == 0U ? 1U : copy_len);
            if (buf == NULL) {
                return ATHENA_TRANSCODE_ERR_OOM;
            }
            if (copy_len > 0U) {
                memcpy(buf, data, copy_len);
            }
            *utf8_out = buf;
            *utf8_len_out = copy_len;
            return ATHENA_TRANSCODE_OK;
        }
        enc_data = lxb_encoding_data(LXB_ENCODING_WINDOWS_1252);
    } else if (enc_data->encoding == LXB_ENCODING_UTF_8
               && athena_is_strict_utf8(data, (size_t) (end - data)))
    {
        size_t copy_len = (size_t) (end - data);
        unsigned char *buf = (unsigned char *) athena_lxb_malloc(copy_len == 0U ? 1U : copy_len);
        if (buf == NULL) {
            return ATHENA_TRANSCODE_ERR_OOM;
        }
        if (copy_len > 0U) {
            memcpy(buf, data, copy_len);
        }
        *utf8_out = buf;
        *utf8_len_out = copy_len;
        return ATHENA_TRANSCODE_OK;
    }

    const lxb_encoding_data_t *utf8_enc = lxb_encoding_data(LXB_ENCODING_UTF_8);
    if (enc_data == NULL || utf8_enc == NULL) {
        return ATHENA_TRANSCODE_ERR_CODEC;
    }

    size_t out_cap = (size_t) (end - data) + 32U;
    size_t ceiling = (max_output_bytes <= SIZE_MAX - 32U)
        ? (max_output_bytes + 32U)
        : SIZE_MAX;
    if (out_cap > ceiling) {
        out_cap = ceiling;
    }
    if (out_cap < 32U) {
        out_cap = 32U;
    }
    unsigned char *out_buf = (unsigned char *) athena_lxb_malloc(out_cap);
    if (out_buf == NULL) {
        return ATHENA_TRANSCODE_ERR_OOM;
    }
    size_t out_len = 0U;

    lxb_codepoint_t cp_buf[1024];
    lxb_encoding_decode_t dec;
    if (lxb_encoding_decode_init(
            &dec, enc_data, cp_buf, sizeof(cp_buf) / sizeof(cp_buf[0])) != LXB_STATUS_OK)
    {
        athena_lxb_free(out_buf);
        return ATHENA_TRANSCODE_ERR_CODEC;
    }
    if (lxb_encoding_decode_replace_set(
            &dec,
            LXB_ENCODING_REPLACEMENT_BUFFER,
            LXB_ENCODING_REPLACEMENT_BUFFER_LEN) != LXB_STATUS_OK)
    {
        athena_lxb_free(out_buf);
        return ATHENA_TRANSCODE_ERR_CODEC;
    }

    const lxb_char_t *p = (const lxb_char_t *) data;
    const lxb_char_t *p_end = (const lxb_char_t *) end;

    while (p < p_end) {
        const lxb_char_t *before_p = p;
        lxb_encoding_decode_buf_used_set(&dec, 0U);
        lxb_status_t ds = enc_data->decode(&dec, &p, p_end);
        size_t used = lxb_encoding_decode_buf_used(&dec);
        if (used > 0U) {
            int rc = athena_append_codepoints_utf8(
                utf8_enc, cp_buf, used, max_output_bytes, &out_buf, &out_len, &out_cap);
            if (rc != ATHENA_TRANSCODE_OK) {
                athena_lxb_free(out_buf);
                return rc;
            }
        }
        if (ds != LXB_STATUS_OK && ds != LXB_STATUS_SMALL_BUFFER && ds != LXB_STATUS_CONTINUE) {
            athena_lxb_free(out_buf);
            return ATHENA_TRANSCODE_ERR_CODEC;
        }
        if (p == before_p && used == 0U) {
            p++;
        }
    }

    lxb_encoding_decode_buf_used_set(&dec, 0U);
    lxb_status_t fs = lxb_encoding_decode_finish(&dec);
    if (fs == LXB_STATUS_OK) {
        size_t tail_used = lxb_encoding_decode_buf_used(&dec);
        if (tail_used > 0U) {
            int rc = athena_append_codepoints_utf8(
                utf8_enc, cp_buf, tail_used, max_output_bytes, &out_buf, &out_len, &out_cap);
            if (rc != ATHENA_TRANSCODE_OK) {
                athena_lxb_free(out_buf);
                return rc;
            }
        }
    }

    *utf8_out = out_buf;
    *utf8_len_out = out_len;
    return ATHENA_TRANSCODE_OK;
}

int athena_lexbor_parse_with_profile(
    const unsigned char *html,
    size_t html_len,
    uint8_t attr_profile,
    athena_lexbor_node_callback callback,
    void *context,
    athena_lexbor_diagnostic_callback diagnostic_callback,
    size_t *tree_errors,
    size_t *tokenizer_errors)
{
    pthread_once(&g_lxb_mem_once, athena_lxb_install_hooks_once);

    if (callback == NULL || tree_errors == NULL || tokenizer_errors == NULL
        || (html == NULL && html_len != 0))
    {
        return -2;
    }

    lxb_html_document_t *document = lxb_html_document_create();
    if (document == NULL) {
        return -1;
    }

    lxb_status_t status = lxb_html_document_parse(
        document, (const lxb_char_t *) html, html_len);
    if (status != LXB_STATUS_OK) {
        lxb_html_document_destroy(document);
        return (int) status;
    }

    lxb_html_parser_t *parser =
        (lxb_html_parser_t *) document->dom_document.parser;
    *tree_errors = parser != NULL && parser->tree != NULL
        && parser->tree->parse_errors != NULL
        ? parser->tree->parse_errors->length : 0;
    *tokenizer_errors = parser != NULL && parser->tkz != NULL
        && parser->tkz->parse_errors != NULL
        ? parser->tkz->parse_errors->length : 0;

    if (diagnostic_callback != NULL && parser != NULL) {
        int diagnostic_status = athena_emit_tree_diagnostics(
            parser->tree == NULL ? NULL : parser->tree->parse_errors,
            diagnostic_callback,
            context);
        if (diagnostic_status == 0) {
            diagnostic_status = athena_emit_tokenizer_diagnostics(
                parser->tkz == NULL ? NULL : parser->tkz->parse_errors,
                diagnostic_callback,
                context);
        }
        if (diagnostic_status < 0) {
            lxb_html_document_destroy(document);
            return -4;
        }
    }

    athena_walk_context_t walk = {
        &document->dom_document.node,
        callback,
        context,
        attr_profile,
        0
    };
    lxb_dom_node_simple_walk(&document->dom_document.node, athena_walk_node, &walk);
    lxb_html_document_destroy(document);
    return walk.stopped ? -3 : 0;
}

int athena_lexbor_parse(
    const unsigned char *html,
    size_t html_len,
    athena_lexbor_node_callback callback,
    void *context,
    athena_lexbor_diagnostic_callback diagnostic_callback,
    size_t *tree_errors,
    size_t *tokenizer_errors)
{
    return athena_lexbor_parse_with_profile(
        html,
        html_len,
        ATHENA_LEXBOR_ATTR_MINIMAL,
        callback,
        context,
        diagnostic_callback,
        tree_errors,
        tokenizer_errors);
}
