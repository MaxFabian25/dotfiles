# Interface instructions

Apply this branch when an instruction tells a reader to interact with software, a device, or a user
interface.

Apply a selected host or product terminology adapter with these rules. For Google developer
documentation, apply the [Google UI terminology adapter](google-developer-documentation-style.md#google-ui-terminology-adapter).
For user documentation on an Apple platform, apply [Apple style](apple-style.md).
Apply [Interface copy](interface-copy.md) only when user-visible product text is written or reviewed.
Apply the [authority and preservation rules](../SKILL.md#authority-and-preservation) to every
label, state, message, command, and product term below.

## Tasks, labels, and controls

- In user materials, describe the task or visible result before naming a control. Name the control
  only when this helps readers locate or distinguish it.
- Give only the steps, controls, and labels that the supplied material states or the product shows,
  and keep every supplied step and note. When the task needs a step the material lacks, mark it for
  the owner to confirm instead of supplying it from memory.
- Distinguish a visible label from text the reader must enter. Name the element by its exact label or
  accessible name. Format a visible label in bold unless the host requires another treatment.
  Format entered text as the exact literal.
- Follow the capitalisation shown in the interface. Use sentence case when labels are all uppercase
  or inconsistently capitalised.
- Name interface elements as the readers' interface language shows them, such as *Control Centre*
  and *Mobile Data* on a British English device, and keep one interface language throughout, even
  when the supplied text mixes them. When the readers' interface language is not stated, use the
  one that matches the document's English variety.
- Omit a trailing interface ellipsis from a generic command reference.
- Give context when naming a control outside a procedure. State its purpose or action in the
  preceding prose.
- Document each specialised accessibility feature that the product provides. State its purpose,
  explain how readers use or configure it, and say where they get support for it.
- Use a control's technical name in developer materials or when readers must understand a
  distinction. For novice readers, use a familiar description only when it improves understanding.
  Use the same name throughout the document.
- Treat an interface element's name as a label, not as a substitute for the reader's action or an
  ordinary noun.
- Use location only to distinguish controls that share a name. Replace directional language with a
  named target. For a difficult-to-find element, add context, name its icon and tooltip, or use a
  screenshot. Use the accessible name when no visible text exists.

## Input methods and interaction verbs

- Use a verb that applies to every supported input method when the methods produce the same result.
  Name an input method or gesture only when it matters.
- In product documentation, give readers a complete procedure for each supported input method.
  Always document keyboard procedures, even when the interface indicates keyboard shortcuts.
- Keep each alternative method complete. Put alternatives after the primary method or in parallel
  table columns.
- Use *select* for choosing an interface object, text, or option. Use *highlighted* for a visual
  state or its change. Do not use *highlight* as a synonym for *select*. Use *choose* for a menu item
  only when the platform establishes that distinction.
- Use *selected* for a chosen option. Use *inactive* or *turned off* for a feature state when that
  wording matches the platform.
- Describe a command or option as *available* or *not available*. State the action that makes it
  available when known. Reserve *unavailable* for outages or services not offered to the reader.
- Use *appears dimmed* only for appearance. State availability separately.
- Use *pointer* for the general pointing indicator and *insertion point* for the position where text
  or data is inserted. Use *caret* only when a technical audience needs that term. Use *cursor* only
  for a defined interface, accessibility, or developer concept.
- Use *enter* when a reader supplies a value through typing, speech, selection, or another input
  method. Use *type* only when producing characters through keys. Use *press* for pressing and
  quickly releasing a keyboard key or mechanical button.
- Use *press and hold* for a keyboard key, mechanical button, or the physical mouse or trackpad. Use
  *click and hold* for an onscreen item operated with a mouse or trackpad. Use *touch and hold* for a
  touchscreen.
- In HTML, use the `kbd` element for a keyboard key or key combination. In non-HTML markup, use
  monospace formatting. Use an uppercase letter for a letter key. Spell out modifier-key names.
  Join keys in a combination with `+`, such as `Control+S`. Use code formatting when a key's value
  is entered as text.
- Describe the user's goal when a gesture is incidental. Use the host's gesture verb and complement
  when the gesture matters. Name fingers or device parts only when readers need them to perform or
  distinguish the gesture. A first definition may name them.
- Use *click* for positioning the pointer over an onscreen element and briefly pressing and
  releasing the mouse or trackpad. Do not add *on* before its direct object. When the mouse or
  trackpad itself is the subject, say *press and release*.
- Describe a drag by naming the moved object, control, destination, or visible result. Do not add
  *click and*, *tap and*, or *drag your finger* unless the input sequence matters. Use *drag*, not
  *put*. Treat dropping as part of the drag when the host defines it that way.
- For a service that sends content to devices automatically, say that the content appears
  automatically or is updated. In developer materials, use *push notification* for a notification
  generated by a server and delivered to a device. In user materials, use *notification*.
- In user materials, describe what haptic feedback feels like when possible. Define *haptics* on
  first use when the term is necessary.
- Use *connect* for joining devices, components, networks, or systems. Use *plug in* for inserting a
  plug or connector into a port, socket, or outlet. For novice readers, identify an unfamiliar
  connector by its function or a distinguishing physical feature.
- For a checkbox action, use *select* or *clear*. Describe its state as *selected* or *not selected*.
- Use *toggle* only as a control noun when the host or accessibility API defines it. State the
  required *on* or *off* position. In user materials, use *turn on*, *turn off*, *switch between*, or
  wording that names the intended state.
- In user materials, use *progress indicator*. Name the operation when context does not make it
  clear. Report a percentage, count, or time estimate only when the system measures it reliably.
  In developer materials, use a narrower technical term only when the distinction matters.

## Software and device actions

- In user materials, use *turn on* and *turn off* for power to a computer or peripheral device. Use
  *shut down* for a platform's named shutdown operation. Use *print*, not *print out*.
- Use *app* for a graphical application in general software prose. Use *program* for software
  without a graphical interface when the distinction matters. Use *application* when a broader or
  formal term is useful. Use one term consistently.
- Use *appear*, not *display*, for items becoming visible on a screen. In most cases, use *display*,
  not *monitor*, for a computer's primary or built-in display.
- In user instructions, state the requested action directly instead of using *prompt* as a verb.
  When a prompt must be named, describe the visible request or use passive voice.
- Use *open* for an app, document, folder, or resource. Reserve *launch* for a defined startup or
  process distinction in developer materials. Use *run* for a command, process, test, or other
  executing operation. Describe a software version as *installed* or *in use* unless execution is
  the subject.
- Use *quit* only when an operation stops an app completely. Use *close* for a window or document
  without implying that the app stops. Use an exact operation such as *stop* or *terminate* for a
  process or service.
- Use *login* only as a noun or adjective. Use *log in* as the verb and *log in to* before its
  object. Use *into* for movement to the inside. Write *in to* when *in* completes the preceding verb.
- Use the product's exact transfer term when it names a distinct operation. Otherwise, select the
  verb by direction and whether the source item remains. Use *download* from remote to local and
  *upload* from local to remote. Use *copy* when the source remains. Use *move* when it does not.
  Name the transferred item and destination for a download or upload.
- Use *homepage* only for a webpage that serves as a website's entry or directory page. Use
  *website* for the site as a whole. Name a more specific destination when it has a distinct role.
- Describe the reader's actual link action. Use *click*, *select*, *open*, *activate*, or another
  verb established by the host. Apply [Link selection and behaviour](references-and-citations.md#link-selection-and-behaviour).

Complete this branch when each instruction names the task, necessary control, exact label or input,
target state, and applicable input method. Every action verb and host term matches the operation.
Each supported keyboard path is documented.
