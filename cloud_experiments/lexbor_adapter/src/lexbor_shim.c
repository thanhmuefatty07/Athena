#include <stddef.h>
#include <stdint.h>

#include "lexbor/core/array_obj.h"
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
    int stopped;
} athena_walk_context_t;

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
        unsigned char actual = value[index];
        if (actual >= 'A' && actual <= 'Z') {
            actual = (unsigned char) (actual + ('a' - 'A'));
        }
        if (actual != (unsigned char) expected[index]) {
            return 0;
        }
    }
    return 1;
}

static int athena_research_attribute(const lxb_char_t *name, size_t name_len) {
    return athena_ascii_case_equal(name, name_len, "href", 4)
        || athena_ascii_case_equal(name, name_len, "id", 2)
        || athena_ascii_case_equal(name, name_len, "rowspan", 7)
        || athena_ascii_case_equal(name, name_len, "colspan", 7)
        || athena_ascii_case_equal(name, name_len, "headers", 7)
        || athena_ascii_case_equal(name, name_len, "scope", 5)
        || athena_ascii_case_equal(name, name_len, "data-unit", 9)
        || athena_ascii_case_equal(name, name_len, "abbr", 4);
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
            if (!athena_research_attribute(attribute_name, attribute_name_len)) {
                continue;
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

int athena_lexbor_parse(
    const unsigned char *html,
    size_t html_len,
    athena_lexbor_node_callback callback,
    void *context,
    athena_lexbor_diagnostic_callback diagnostic_callback,
    size_t *tree_errors,
    size_t *tokenizer_errors)
{
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
        0
    };
    lxb_dom_node_simple_walk(&document->dom_document.node, athena_walk_node, &walk);
    lxb_html_document_destroy(document);
    return walk.stopped ? -3 : 0;
}
