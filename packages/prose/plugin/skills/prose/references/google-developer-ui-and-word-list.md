# Google UI terminology and word-list lookup

Apply the matching section alongside [Google developer documentation style
guide](google-developer-documentation-style.md) when Google developer documentation names UI
elements or requires checking a Google word-list entry.

## Google UI terminology adapter

Apply [Interface instructions](interface-instructions.md) to the task, labels, input methods, and
interaction verbs. Apply this adapter only to Google developer documentation.

- Use *command* for an item in a desktop menu. Keep *menu item* only for documentation about
  interface construction.
- Express a menu path in prose or by using angle brackets (`>`). Format the entire sequence
  together. Reserve this notation for a menu path rather than a combination of different UI
  elements. Use the host's method for making a screen reader interpret each angle bracket as *and
  then*.
- Use *window* for an entire desktop application or a modular element that readers can open and
  close.
- Use *page* for a web page or a subpage of a console. Use *dialog* for a smaller window that appears
  in front of the main application window.
- Use *pane* or *panel* for a distinct region inside a larger window. Use *section* for a labelled
  grouping of options and controls.
- Use *navigation menu* for a control that contains a list of items. The items take readers to pages
  in an application or website. Use *toolbar* for a set of buttons for common user actions.
- Use *menu button* for a button that opens a menu. Use *button* for a control that starts an action.
- Use *icon* for a symbol or image representing an object or function. Use *tab* for a navigation
  element that looks like a file tab.
- Use *box* for a text box, combo box, or spin box. Use *list* for a list box when clearer.
- Use *checkbox* for a small box that indicates whether an option is on or off. Use *radio button*
  for one option in a mutually exclusive group.
- Use *expander arrow* for the UI element used to expand or collapse a section. Use *toggle* for
  the UI element that switches between on and off states.
- In Google Cloud and Google Workspace documentation, use *field* instead of *box*. Elsewhere,
  follow host terminology.

## Word-list evidence

Use [Google developer documentation style guide](google-developer-documentation-style.md) for
ordinary drafting. When project terminology and that guidance leave a material word choice
unresolved, look up the entry in this order:

1. Run a fixed-string search on the [entry
   register](google-developer-documentation-word-list-entries.csv) to find the exact anchor, then
   read only the matching `decision_ids` rows in the [decision
   register](google-developer-documentation-word-list-decisions.csv) rather than loading either
   register end-to-end.
2. Treat `source_status` as evidence about Google's heading, not as a local adoption decision:
   `unmarked` records no source icon, `caution` and `do-not-use` record the corresponding source
   markers, and `product_markers` apply only inside each named product scope.
3. Read the matching decision treatments before following their destinations. Select only decisions
   whose conditions match the word's use in the current passage. Use the first destination for the
   shared rule and each additional destination only under its stated condition; reuse sections
   already read. Apply unique adoption treatments within the entry's scope and the authority order.
4. Consult the current official [word-list entry](https://developers.google.com/style/word-list)
   when its exact clause is needed to resolve that choice. Report a discrepancy that changes the
   affected recommendation or its scope, and continue authorised work that does not depend on it.
   Distinguish recorded guidance from what the live source verifies. If no anchor applies, use
   [Terminology selection](terminology-selection.md); keep an established conventional term instead
   of inventing a paraphrase.

Preserve a term covered by *Don't use* or *Use with caution* guidance when code or an interface
requires it. Format it as exact text, and apply the entry's guidance about alternative terms in
prose thereafter. Keep Google product terminology inside its product scope.

For a requested rule audit, record each recommendation's controlling rule, scope, and material
source conflicts. When updating adopted guidance or recorded evidence, apply the [source
maintenance procedure](source-manifest.md#source-drift).

Complete this branch when every UI element in Google developer documentation uses the adapter's
designations and any unresolved word-list choice is settled against the matching register rows,
product scope, and exact-text boundary.
