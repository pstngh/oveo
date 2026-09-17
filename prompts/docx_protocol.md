# DOCX protocol extension

The attachment or active canonical work contains `docx_blocks`. Return every
block ID exactly once and in order. Do not add, remove, split, merge, or reorder
paragraphs, list items, table rows, or table cells.

Preserve every paired protected hyperlink wrapper and ID exactly once and in its
original order, such as
`{{OVEO_LINK_l000001}}display text{{/OVEO_LINK_l000001}}`. Only the display text
may change.

On `establish`, `replace`, or `full`, add a complete `docx_blocks` array:

`"docx_blocks":[{"id":"p000001","text":"complete replacement block"}]`

For a DOCX attachment, `source` is the uploaded blocks' clean text. `output` and
the visible deliverable are the returned blocks' clean text: block text joined
in order with blank lines, retaining hyperlink display text but removing its
wrappers. The server requires exact equality. DOCX-backed work cannot use
`append`. Omit `docx_blocks` for non-DOCX work.
