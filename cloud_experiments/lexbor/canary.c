#include <stdio.h>
#include <string.h>

#include "lexbor/core/array_obj.h"
#include "lexbor/dom/interfaces/attr.h"
#include "lexbor/dom/interfaces/character_data.h"
#include "lexbor/dom/interfaces/element.h"
#include "lexbor/dom/interfaces/node.h"
#include "lexbor/html/interfaces/document.h"
#include "lexbor/html/parser.h"
#include "lexbor/html/tokenizer.h"
#include "lexbor/html/tree.h"

typedef struct {
    size_t paragraphs;
    size_t tables;
    size_t bodies;
    size_t rows;
    size_t cells;
    size_t anchors;
    size_t scripts;
    size_t text_nodes;
    lxb_dom_node_t *paragraph;
    lxb_dom_node_t *cell;
    lxb_dom_node_t *anchor;
} observed_t;

static lexbor_action_t observe_node(lxb_dom_node_t *node, void *context) {
    observed_t *observed = (observed_t *) context;
    if (node->type == LXB_DOM_NODE_TYPE_TEXT) {
        observed->text_nodes++;
        return LEXBOR_ACTION_OK;
    }
    if (node->type != LXB_DOM_NODE_TYPE_ELEMENT) {
        return LEXBOR_ACTION_OK;
    }

    size_t name_len = 0;
    const lxb_char_t *name = lxb_dom_node_name(node, &name_len);
    if (name == NULL) {
        return LEXBOR_ACTION_OK;
    }

    /* Lexbor 3.0.0's public node-name accessor returns uppercase HTML names. */
#define IS_TAG(tag) (name_len == sizeof(tag) - 1 && memcmp(name, tag, sizeof(tag) - 1) == 0)
    if (IS_TAG("P")) {
        observed->paragraphs++;
        observed->paragraph = node;
    } else if (IS_TAG("TABLE")) {
        observed->tables++;
    } else if (IS_TAG("TBODY")) {
        observed->bodies++;
    } else if (IS_TAG("TR")) {
        observed->rows++;
    } else if (IS_TAG("TD")) {
        observed->cells++;
        observed->cell = node;
    } else if (IS_TAG("A")) {
        observed->anchors++;
        observed->anchor = node;
    } else if (IS_TAG("SCRIPT")) {
        observed->scripts++;
    }
#undef IS_TAG
    return LEXBOR_ACTION_OK;
}

static int require(int condition, const char *contract) {
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", contract);
        return 0;
    }
    return 1;
}

static int text_equals(lxb_dom_node_t *node, const char *expected, size_t expected_len) {
    if (node == NULL) {
        return 0;
    }
    size_t actual_len = 0;
    lxb_char_t *actual = lxb_dom_node_text_content(node, &actual_len);
    int equal = actual != NULL && actual_len == expected_len
        && memcmp(actual, expected, expected_len) == 0;
    if (actual != NULL) {
        lxb_dom_document_destroy_text(node->owner_document, actual);
    }
    return equal;
}

int main(void) {
    static const lxb_char_t html[] =
        "<!doctype html><table><tr><td>table &amp; \xE5\x86\x85\xE5\xBB\x89</td></tr></table>"
        "<p>A &amp; B <a href='/x'>link</a></p><script>ignored()</script>";
    static const char expected_paragraph[] = "A & B link";
    static const char expected_cell[] = "table & \xE5\x86\x85\xE5\xBB\x89";

    lxb_html_document_t *document = lxb_html_document_create();
    if (!require(document != NULL, "document allocation")) {
        return 1;
    }

    lxb_status_t status = lxb_html_document_parse(document, html, sizeof(html) - 1);
    if (!require(status == LXB_STATUS_OK, "parse the supplied UTF-8 HTML bytes")) {
        lxb_html_document_destroy(document);
        return 1;
    }

    observed_t observed = {0};
    lxb_dom_node_simple_walk(&document->dom_document.node, observe_node, &observed);

    int ok = 1;
    ok &= require(observed.paragraphs == 1 && observed.tables == 1 && observed.rows == 1
        && observed.cells == 1, "retain the expected paragraph and table descendants");
    ok &= require(observed.bodies == 1, "insert the HTML5 implicit tbody");
    ok &= require(observed.scripts == 1, "retain script in the parser tree for projection policy");
    ok &= require(observed.anchors == 1 && observed.anchor != NULL, "retain the anchor element");
    ok &= require(text_equals(observed.paragraph, expected_paragraph, sizeof(expected_paragraph) - 1),
        "decode entities and preserve descendant text order");
    ok &= require(text_equals(observed.cell, expected_cell, sizeof(expected_cell) - 1),
        "preserve UTF-8 bytes and decode the table-cell entity");

    if (observed.anchor != NULL) {
        size_t href_len = 0;
        const lxb_char_t *href = lxb_dom_element_get_attribute(
            lxb_dom_interface_element(observed.anchor), (const lxb_char_t *) "href", 4, &href_len);
        ok &= require(href != NULL && href_len == 2 && memcmp(href, "/x", 2) == 0,
            "expose the decoded href for Athena's HTTPS link policy");
    }

    lxb_html_parser_t *parser = (lxb_html_parser_t *) document->dom_document.parser;
    size_t tree_errors = parser != NULL && parser->tree != NULL && parser->tree->parse_errors != NULL
        ? parser->tree->parse_errors->length : 0;
    size_t tokenizer_errors = parser != NULL && parser->tkz != NULL && parser->tkz->parse_errors != NULL
        ? parser->tkz->parse_errors->length : 0;
    lxb_html_document_destroy(document);

    static const lxb_char_t malformed[] = "<table><tr><td>x</table>";
    lxb_html_document_t *malformed_document = lxb_html_document_create();
    if (!require(malformed_document != NULL, "malformed-input document allocation")) {
        return 1;
    }
    status = lxb_html_document_parse(malformed_document, malformed, sizeof(malformed) - 1);
    ok &= require(status == LXB_STATUS_OK, "HTML5 repair returns a document for malformed markup");
    lxb_html_parser_t *malformed_parser = (lxb_html_parser_t *) malformed_document->dom_document.parser;
    size_t malformed_tree_errors = malformed_parser != NULL && malformed_parser->tree != NULL
        && malformed_parser->tree->parse_errors != NULL
        ? malformed_parser->tree->parse_errors->length : 0;
    ok &= require(malformed_tree_errors > 0, "expose tree-builder diagnostics for malformed HTML");

    printf("{\"status\":\"%s\",\"text_nodes\":%zu,\"tree_errors\":%zu,\"tokenizer_errors\":%zu,\"malformed_tree_errors\":%zu}\n",
        ok ? "PASS" : "FAIL", observed.text_nodes, tree_errors, tokenizer_errors, malformed_tree_errors);
    lxb_html_document_destroy(malformed_document);
    return ok ? 0 : 1;
}
