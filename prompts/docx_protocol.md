# DOCX protocol extension

A `source` attachment or active canonical work contains the working
`docx_blocks`. Return every working-document block ID exactly once and in order.
Do not add, remove, split, merge, or reorder paragraphs, list items, table rows,
or table cells. An `active_reference_document` and any attachment whose role is
`reference` are precedent only: never use their block set as the returned
replacement map, use them as the template, or make their text canonical source or
output. If a reference and working document have overlapping ID strings, every
returned ID still denotes the working document's block and must contain that
working block's replacement text.

Preserve every paired protected hyperlink wrapper and ID exactly once and in its
original order, such as
`{{OVEO_LINK_l000001}}display text{{/OVEO_LINK_l000001}}`. Only the display text
may change.

On `establish`, `replace`, or `full`, add a complete `docx_blocks` array:

`"docx_blocks":[{"id":"p000001","text":"complete replacement block"}]`

For a `source` DOCX attachment, omit `source` on `establish`: the application uses
the uploaded document. The visible deliverable is the output: the returned blocks'
clean text joined in order with blank lines, retaining hyperlink display text but
removing its wrappers. The server requires exact equality. DOCX-backed work
cannot use `append`. Omit `docx_blocks` for non-DOCX work.
