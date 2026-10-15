# Interface copy

Apply this branch to visible labels, controls, messages, forms, tooltips, statuses, and prompts
or responses from bots and virtual agents.

Use the registered Apple button, alert, and progress authorities only within Apple-platform scope.
Use W3C accessibility authorities across web interfaces. Apply `AUTH-ISO-IEC-23859` only when its
easy-to-read and easy-to-understand interface-text scope is selected.

Apply these rules to user-visible interface text:

- **Problems.** State what failed and its visible consequence. State a cause only when it is known.
  Give the next safe action when recovery is possible. Use neutral language that identifies the
  problem without assigning blame.
- **Message terms.** Use *error message* for text about a failure. Use the WCAG term *status
  message* only within its defined scope. Describe a non-safety risk directly instead of calling
  it a warning.
- **Web-form input errors.** Write each user-visible error message to identify the invalid input and give a realistic correction. Apply the W3C term only to this web-form scope.
- **Action controls.** Label each action control with a concise verb or verb phrase that states its result. Use **Yes** or **No** only as direct answers to a nearby question. Avoid generic labels such as **OK** and **Submit** when a specific action fits.
- **Destructive confirmations.** Before an irreversible or unexpected destructive action, name the affected object and the consequence. Label the confirming control with the action. Give a clear cancel path. Ask a confirmation question that names the action and consequence.
- **Empty states.** Write each empty state to identify why content is absent when known. When the reader can resolve the empty state, give one action that can make the content available. Use guidance instead of humour or decorative filler.
- **Success messages.** Show a success message only when the result is important or not otherwise visible. Name the completed action and affected object.
- **Persistent form labels.** Give each input a persistent visible label that names the requested value. Use placeholder text only for a non-essential example or format hint. Pair placeholder text with the persistent label and every essential instruction.
- **Advance input constraints.** State every known input constraint before entry, including required status, accepted format, range, and length.
- **Option groups.** Give each radio-button or checkbox group one short visible question or group label. Make each option label self-explanatory without repeating the group label.
- **Apologies.** Use one brief apology only when a serious failure or material disruption warrants
  it. Follow with the impact and recovery path. For an external condition, state it directly and
  give the next action.
- **Tooltips.** Use a tooltip only for brief, supplemental information about one control. Keep required instructions, warnings, errors, and statuses visible outside it.
- **Toggle labels.** Label a toggle with the setting, view, or content whose state it controls, not
  with an imperative command. Use explicit paired state words when opposing states are unclear.
  Prefer specific pairs such as **Show/Hide**, **Allow/Block**, **Add/Remove**, or **On/Off** to
  **Enable/Disable**.

## Chatbots and virtual agents

- **Bot identity.** Tell readers when they are interacting with a bot.
  State its purpose, limits, and useful starting tasks.
- **Supported tasks.** State the bot's specific role and offer supported tasks instead of an
  unrestricted invitation. Keep each turn focused on one question or next action.
- **Unanswered questions.** State when the bot cannot answer. Point readers to the next
  useful resource or available human support path.
- **Human support.** At defined failure or sensitivity thresholds, offer help from a human when
  available. Explain how to get it.
- **Intent clarification.** Before acting, clarify or disambiguate the reader's input without blame.
  Ask one specific question or offer supported options.
- **Tone.** Match tone to consequence. Use brief, direct language for serious,
  financial, security, or recovery contexts.
- **Progress.** Respond promptly. Show a truthful typing or progress indicator when a response is
  delayed. Name the operation without presenting the bot as thinking or feeling.
- **Selectable replies.** Write each selectable reply from the user's perspective, using *I*, *me*,
  and *my*. Make its consequence clear without surrounding context.
- **Command labels.** Use familiar words such as *help*, *settings*, *start over*, and *stop*
  to label supported bot commands.

| Scope | Preferred designation |
| --- | --- |
| Visible text or another component with a text alternative that identifies a component | label |
| Name of a user interface element as exposed by a platform accessibility API | accessible name |
| A content change, not a change of context, that informs the user of success or results, waiting state, progress, or errors | status message |
| Component that lets people choose between a pair of opposing states, such as on and off | toggle |
| Directions needed to complete an input | instruction |
| Message about an input error in a web form | error message |
| Small, transient view that briefly describes how to use a component in the interface | tooltip |

Complete this branch when every visible label, message, control, prompt, and response in scope
follows each applicable interface rule.
