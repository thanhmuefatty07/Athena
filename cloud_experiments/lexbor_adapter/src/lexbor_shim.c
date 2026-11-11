#include <pthread.h>
#include <stdatomic.h>
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

/*
 * 16-byte-aligned allocation header for exact Lexbor C heap accounting (T2a).
 */
typedef struct __attribute__((aligned(16))) {
    size_t byte_size;
    size_t magic;
} athena_lxb_alloc_header_t;

_Static_assert(sizeof(athena_lxb_alloc_header_t) == 16,
               "Lexbor allocation header must be 16 bytes");
_Static_assert(_Alignof(athena_lxb_alloc_header_t) == 16,
               "Lexbor allocation header must preserve 16-byte alignment");

#define ATHENA_LXB_HEADER_MAGIC ((size_t) 0xA74E4A1EB0C30001ULL)

static pthread_once_t g_lxb_mem_once = PTHREAD_ONCE_INIT;
static atomic_size_t g_lxb_alloc_calls = 0;
static atomic_size_t g_lxb_live_bytes = 0;
static atomic_size_t g_lxb_peak_bytes = 0;

static void athena_lxb_record_growth(size_t delta) {
    size_t prev = atomic_fetch_add_explicit(&g_lxb_live_bytes, delta, memory_order_relaxed);
    size_t current = prev + delta;
    size_t observed_peak = atomic_load_explicit(&g_lxb_peak_bytes, memory_order_relaxed);
    while (current > observed_peak) {
        if (atomic_compare_exchange_weak_explicit(
                &g_lxb_peak_bytes,
                &observed_peak,
                current,
                memory_order_relaxed,
                memory_order_relaxed))
        {
            break;
        }
    }
}

static void *athena_lxb_malloc(size_t size) {
    if (size > SIZE_MAX - sizeof(athena_lxb_alloc_header_t) - 1) {
        return NULL;
    }
    size_t payload = size == 0 ? 1 : size;
    athena_lxb_alloc_header_t *hdr =
        (athena_lxb_alloc_header_t *) malloc(sizeof(athena_lxb_alloc_header_t) + payload);
    if (hdr == NULL) {
        return NULL;
    }
    hdr->byte_size = size;
    hdr->magic = ATHENA_LXB_HEADER_MAGIC;
    atomic_fetch_add_explicit(&g_lxb_alloc_calls, 1, memory_order_relaxed);
    athena_lxb_record_growth(size);
    return (void *) (hdr + 1);
}

static void *athena_lxb_calloc(size_t num, size_t size) {
    if (num != 0 && size > SIZE_MAX / num) {
        return NULL;
    }
    size_t user_bytes = num * size;
    if (user_bytes > SIZE_MAX - sizeof(athena_lxb_alloc_header_t) - 1) {
        return NULL;
    }
    size_t payload = user_bytes == 0 ? 1 : user_bytes;
    athena_lxb_alloc_header_t *hdr =
        (athena_lxb_alloc_header_t *) calloc(1, sizeof(athena_lxb_alloc_header_t) + payload);
    if (hdr == NULL) {
        return NULL;
    }
    hdr->byte_size = user_bytes;
    hdr->magic = ATHENA_LXB_HEADER_MAGIC;
    atomic_fetch_add_explicit(&g_lxb_alloc_calls, 1, memory_order_relaxed);
    athena_lxb_record_growth(user_bytes);
    return (void *) (hdr + 1);
}

static void athena_lxb_free(void *dst) {
    if (dst == NULL) {
        return;
    }
    athena_lxb_alloc_header_t *hdr = ((athena_lxb_alloc_header_t *) dst) - 1;
    if (hdr->magic != ATHENA_LXB_HEADER_MAGIC) {
        abort();
    }
    size_t sz = hdr->byte_size;
    hdr->magic = 0;
    atomic_fetch_sub_explicit(&g_lxb_live_bytes, sz, memory_order_relaxed);
    free(hdr);
}

static void *athena_lxb_realloc(void *dst, size_t size) {
    if (dst == NULL) {
        return athena_lxb_malloc(size);
    }
    if (size > SIZE_MAX - sizeof(athena_lxb_alloc_header_t) - 1) {
        return NULL;
    }
    athena_lxb_alloc_header_t *old_hdr = ((athena_lxb_alloc_header_t *) dst) - 1;
    if (old_hdr->magic != ATHENA_LXB_HEADER_MAGIC) {
        abort();
    }
    size_t old_size = old_hdr->byte_size;
    size_t payload = size == 0 ? 1 : size;
    athena_lxb_alloc_header_t *new_hdr = (athena_lxb_alloc_header_t *) realloc(
        old_hdr, sizeof(athena_lxb_alloc_header_t) + payload);
    if (new_hdr == NULL) {
        return NULL;
    }
    new_hdr->byte_size = size;
    new_hdr->magic = ATHENA_LXB_HEADER_MAGIC;
    atomic_fetch_add_explicit(&g_lxb_alloc_calls, 1, memory_order_relaxed);
    if (size >= old_size) {
        athena_lxb_record_growth(size - old_size);
    } else {
        atomic_fetch_sub_explicit(&g_lxb_live_bytes, old_size - size, memory_order_relaxed);
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

void athena_lexbor_reset_and_enable_metering(
    size_t *alloc_calls_out,
    size_t *live_bytes_out,
    size_t *peak_bytes_out)
{
    pthread_once(&g_lxb_mem_once, athena_lxb_install_hooks_once);
    atomic_store_explicit(&g_lxb_alloc_calls, 0, memory_order_relaxed);
    size_t current_live = atomic_load_explicit(&g_lxb_live_bytes, memory_order_relaxed);
    atomic_store_explicit(&g_lxb_peak_bytes, current_live, memory_order_relaxed);
    if (alloc_calls_out != NULL) {
        *alloc_calls_out = 0;
    }
    if (live_bytes_out != NULL) {
        *live_bytes_out = current_live;
    }
    if (peak_bytes_out != NULL) {
        *peak_bytes_out = current_live;
    }
}

void athena_lexbor_read_heap_counters(
    size_t *alloc_calls_out,
    size_t *live_bytes_out,
    size_t *peak_bytes_out)
{
    pthread_once(&g_lxb_mem_once, athena_lxb_install_hooks_once);
    if (alloc_calls_out != NULL) {
        *alloc_calls_out = atomic_load_explicit(&g_lxb_alloc_calls, memory_order_relaxed);
    }
    if (live_bytes_out != NULL) {
        *live_bytes_out = atomic_load_explicit(&g_lxb_live_bytes, memory_order_relaxed);
    }
    if (peak_bytes_out != NULL) {
        *peak_bytes_out = atomic_load_explicit(&g_lxb_peak_bytes, memory_order_relaxed);
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
    lxb_dom_node_t *document_node;
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

static int athena_ascii_space(unsigned char ch) {
    return ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r' || ch == '\f';
}

static int athena_ascii_case_equal(
    const lxb_char_t *value,
    size_t value_len,
    const char *expected,
    size_t expected_len)
{
    if (value == NULL || value_len != expected_len) {
        return 0;
    }
    for (size_t index = 0; index < expected_len; index++) {
        if (athena_ascii_lower(value[index]) != (unsigned char) expected[index]) {
            return 0;
        }
    }
    return 1;
}

static int athena_ascii_starts_with(
    const lxb_char_t *value,
    size_t value_len,
    const char *prefix,
    size_t prefix_len)
{
    if (value == NULL || value_len < prefix_len) {
        return 0;
    }
    return athena_ascii_case_equal(value, prefix_len, prefix, prefix_len);
}

static int athena_ascii_contains(
    const lxb_char_t *value,
    size_t value_len,
    const char *needle,
    size_t needle_len)
{
    if (value == NULL || needle_len == 0 || value_len < needle_len) {
        return 0;
    }
    for (size_t i = 0; i + needle_len <= value_len; i++) {
        if (athena_ascii_case_equal(value + i, needle_len, needle, needle_len)) {
            return 1;
        }
    }
    return 0;
}

static int athena_is_zero_css_number(const lxb_char_t *val, size_t len) {
    size_t i = 0;
    while (i < len && athena_ascii_space(val[i])) {
        i++;
    }
    if (i < len && (val[i] == '+' || val[i] == '-')) {
        i++;
    }
    int saw_zero = 0;
    while (i < len && val[i] == '0') {
        saw_zero = 1;
        i++;
    }
    if (i < len && val[i] == '.') {
        i++;
        while (i < len && val[i] == '0') {
            saw_zero = 1;
            i++;
        }
    }
    if (!saw_zero) {
        return 0;
    }
    if (i < len && val[i] >= '1' && val[i] <= '9') {
        return 0;
    }
    return 1;
}

static int athena_is_large_negative_coord(const lxb_char_t *val, size_t len) {
    size_t i = 0;
    while (i < len && athena_ascii_space(val[i])) {
        i++;
    }
    if (i >= len || val[i] != '-') {
        return 0;
    }
    i++;
    size_t digits = 0;
    unsigned long acc = 0;
    while (i < len && val[i] >= '0' && val[i] <= '9') {
        acc = acc * 10UL + (unsigned long) (val[i] - '0');
        digits++;
        if (acc >= 500UL) {
            return 1;
        }
        i++;
    }
    return digits >= 3 && acc >= 500UL;
}

static size_t athena_distill_style_tokens(
    const lxb_char_t *style,
    size_t style_len,
    unsigned char out_buf[128])
{
    if (style == NULL || style_len == 0) {
        return 0;
    }
    enum {
        TOK_DISPLAY_NONE = 1 << 0,
        TOK_VIS_HIDDEN = 1 << 1,
        TOK_OPACITY_ZERO = 1 << 2,
        TOK_FONT_ZERO = 1 << 3,
        TOK_CLIP_HIDDEN = 1 << 4,
        TOK_OFFSCREEN = 1 << 5,
        TOK_TRANSPARENT = 1 << 6,
        TOK_OVERFLOW_HIDDEN = 1 << 7,
        TOK_ZERO_DIM = 1 << 8
    };
    unsigned int flags = 0;

    size_t pos = 0;
    while (pos < style_len) {
        size_t decl_end = pos;
        int in_parens = 0;
        while (decl_end < style_len) {
            unsigned char ch = style[decl_end];
            if (ch == '(') {
                in_parens++;
            } else if (ch == ')' && in_parens > 0) {
                in_parens--;
            } else if (ch == ';' && in_parens == 0) {
                break;
            }
            decl_end++;
        }

        size_t p_start = pos;
        while (p_start < decl_end && athena_ascii_space(style[p_start])) {
            p_start++;
        }
        size_t colon = p_start;
        while (colon < decl_end && style[colon] != ':') {
            colon++;
        }
        if (colon < decl_end) {
            size_t p_end = colon;
            while (p_end > p_start && athena_ascii_space(style[p_end - 1])) {
                p_end--;
            }
            size_t v_start = colon + 1;
            while (v_start < decl_end && athena_ascii_space(style[v_start])) {
                v_start++;
            }
            size_t v_end = decl_end;
            while (v_end > v_start && athena_ascii_space(style[v_end - 1])) {
                v_end--;
            }

            const lxb_char_t *prop = style + p_start;
            size_t prop_len = p_end - p_start;
            const lxb_char_t *val = style + v_start;
            size_t val_len = v_end - v_start;

            if (val_len >= 10) {
                size_t bang = val_len;
                while (bang > 0 && val[bang - 1] != '!') {
                    bang--;
                }
                if (bang > 0
                    && athena_ascii_starts_with(val + bang - 1, val_len - (bang - 1), "!important", 10))
                {
                    val_len = bang - 1;
                    while (val_len > 0 && athena_ascii_space(val[val_len - 1])) {
                        val_len--;
                    }
                }
            }

            if (athena_ascii_case_equal(prop, prop_len, "display", 7)
                && athena_ascii_case_equal(val, val_len, "none", 4))
            {
                flags |= TOK_DISPLAY_NONE;
            }
            else if (athena_ascii_case_equal(prop, prop_len, "visibility", 10)
                     && (athena_ascii_case_equal(val, val_len, "hidden", 6)
                         || athena_ascii_case_equal(val, val_len, "collapse", 8)))
            {
                flags |= TOK_VIS_HIDDEN;
            }
            else if (athena_ascii_case_equal(prop, prop_len, "opacity", 7)
                     && athena_is_zero_css_number(val, val_len))
            {
                flags |= TOK_OPACITY_ZERO;
            }
            else if (athena_ascii_case_equal(prop, prop_len, "font-size", 9)
                     && athena_is_zero_css_number(val, val_len))
            {
                flags |= TOK_FONT_ZERO;
            }
            else if ((athena_ascii_case_equal(prop, prop_len, "clip", 4)
                      && athena_ascii_contains(val, val_len, "rect(0", 6))
                     || (athena_ascii_case_equal(prop, prop_len, "clip-path", 9)
                         && (athena_ascii_contains(val, val_len, "inset(50%", 9)
                             || athena_ascii_contains(val, val_len, "inset(100%", 10)
                             || athena_ascii_contains(val, val_len, "circle(0", 8)
                             || athena_ascii_contains(val, val_len, "polygon(0", 9))))
            {
                flags |= TOK_CLIP_HIDDEN;
            }
            else if ((athena_ascii_case_equal(prop, prop_len, "left", 4)
                      || athena_ascii_case_equal(prop, prop_len, "top", 3)
                      || athena_ascii_case_equal(prop, prop_len, "text-indent", 11))
                     && athena_is_large_negative_coord(val, val_len))
            {
                flags |= TOK_OFFSCREEN;
            }
            else if (athena_ascii_case_equal(prop, prop_len, "color", 5)
                     && (athena_ascii_case_equal(val, val_len, "transparent", 11)
                         || athena_ascii_contains(val, val_len, ",0)", 3)
                         || athena_ascii_contains(val, val_len, ", 0)", 4)))
            {
                flags |= TOK_TRANSPARENT;
            }
            else if ((athena_ascii_case_equal(prop, prop_len, "overflow", 8)
                      || athena_ascii_case_equal(prop, prop_len, "overflow-y", 10)
                      || athena_ascii_case_equal(prop, prop_len, "overflow-x", 10))
                     && athena_ascii_contains(val, val_len, "hidden", 6))
            {
                flags |= TOK_OVERFLOW_HIDDEN;
            }
            else if ((athena_ascii_case_equal(prop, prop_len, "height", 6)
                      || athena_ascii_case_equal(prop, prop_len, "max-height", 10)
                      || athena_ascii_case_equal(prop, prop_len, "width", 5)
                      || athena_ascii_case_equal(prop, prop_len, "max-width", 9))
                     && athena_is_zero_css_number(val, val_len))
            {
                flags |= TOK_ZERO_DIM;
            }
        }
        pos = decl_end < style_len ? decl_end + 1 : style_len;
    }

    static const struct {
        unsigned int bit;
        const char *token;
    } table[] = {
        { TOK_DISPLAY_NONE, "display:none" },
        { TOK_VIS_HIDDEN, "visibility:hidden" },
        { TOK_OPACITY_ZERO, "opacity:0" },
        { TOK_FONT_ZERO, "font-size:0" },
        { TOK_CLIP_HIDDEN, "clip-hidden" },
        { TOK_OFFSCREEN, "offscreen" },
        { TOK_TRANSPARENT, "color:transparent" }
    };

    size_t written = 0;
    for (size_t k = 0; k < sizeof(table) / sizeof(table[0]); k++) {
        if ((flags & table[k].bit) != 0) {
            size_t tlen = strlen(table[k].token);
            if (written > 0 && written + 1 < 128) {
                out_buf[written++] = ';';
            }
            if (written + tlen < 128) {
                memcpy(out_buf + written, table[k].token, tlen);
                written += tlen;
            }
        }
    }
    if ((flags & TOK_OVERFLOW_HIDDEN) != 0 && (flags & TOK_ZERO_DIM) != 0) {
        const char *ztok = "zero-box-hidden";
        size_t tlen = strlen(ztok);
        if (written > 0 && written + 1 < 128) {
            out_buf[written++] = ';';
        }
        if (written + tlen < 128) {
            memcpy(out_buf + written, ztok, tlen);
            written += tlen;
        }
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
        || athena_ascii_case_equal(name, name_len, "class", 5)
        || athena_ascii_case_equal(name, name_len, "role", 4)
        || athena_ascii_case_equal(name, name_len, "aria-hidden", 11)
        || athena_ascii_case_equal(name, name_len, "aria-label", 10)
        || athena_ascii_case_equal(name, name_len, "hidden", 6)
        || athena_ascii_case_equal(name, name_len, "style", 5)
        || athena_ascii_case_equal(name, name_len, "lang", 4)
        || athena_ascii_case_equal(name, name_len, "dir", 3)
        || athena_ascii_case_equal(name, name_len, "type", 4)
        || athena_ascii_case_equal(name, name_len, "alt", 3)
        || athena_ascii_case_equal(name, name_len, "title", 5)
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
    }
    else if (node->type == LXB_DOM_NODE_TYPE_TEXT) {
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

            if (context->attr_profile == ATHENA_LEXBOR_ATTR_STAGE_IR) {
                if (!athena_stage_ir_attribute(attribute_name, attribute_name_len)) {
                    continue;
                }
                unsigned char distilled_style[128];
                if (athena_ascii_case_equal(attribute_name, attribute_name_len, "style", 5)) {
                    size_t distilled_len = athena_distill_style_tokens(
                        attribute_value, attribute_value_len, distilled_style);
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
