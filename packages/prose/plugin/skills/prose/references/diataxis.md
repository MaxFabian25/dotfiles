# Diátaxis documentation architecture

Apply this branch to durable technical or product documentation that serves learning, task,
information, or understanding needs. A software mention alone does not select this branch.

The Diátaxis authorities in [source provenance](source-manifest.md) identify the official guidance.
This module contains independently worded adaptations. It uses the four types as purposes, not
mandatory navigation labels.

## Classify the user need

Use the smallest stable unit that a reader can reach independently. This unit is usually a page or
named section. Determine what the reader needs from that unit before selecting its structure.

Use this mapping to select the unit's primary type:

| The reader needs to | The reader is | Type |
| --- | --- | --- |
| learn through practical activity | acquiring skill | tutorial |
| complete a real task or solve a problem | applying existing skill | how-to guide |
| find accurate facts while working | applying existing skill | reference |
| understand a defined subject | acquiring knowledge or reflecting | explanation |

Give each unit one primary purpose. When architecture is in scope, place another purpose in a
distinct unit or subsection. Link that material from the point where the reader needs it.

Keep navigation suited to the product and readers. Create a type when its reader need exists, not
only to make the set symmetrical.

## Tutorials

A tutorial gives a learner a practical learning experience.

- State a meaningful and achievable outcome.
- Define a safe starting state and use one reliable path.
- Give concrete steps in a logical order.
- Produce visible results early and often. State the expected result after each meaningful action.
- Prompt the learner to notice important effects.
- Keep choices, exhaustive reference, and extended explanation in linked material.
- Test the complete tutorial from its stated starting state.

Complete this type when a learner can follow the path and obtain every promised result in the
stated environment.

## How-to guides

A how-to guide helps a competent reader complete a real task or solve a real problem.

- Name the goal, applicability conditions, prerequisites, and material hazards.
- Organise the guide around the reader's goal, not a product feature.
- Give an executable sequence. Add conditional branches only when real-world variation requires
  them.
- Include the context needed for judgement. Put teaching, exhaustive reference, and extended
  explanation in linked material.
- Start and finish at points that let the reader connect the guide to their work.

Complete this type when a competent reader can recognise its applicability and complete the stated
goal, including each supported branch.

## Reference

Reference gives facts that a reader consults while working.

- Define the scope and mirror the documented system's structure.
- State facts neutrally and authoritatively.
- Use consistent patterns for comparable items.
- Include the parameters, options, types, limitations, errors, defaults, and examples in scope.
- Keep task sequences and discursive context in linked how-to guides and explanations.
- Verify each description against the documented system or another authoritative source.

Complete this type when every item in scope is findable, complete, and consistent with its
authoritative source.

## Explanation

Explanation develops understanding of a defined subject.

- Use a real or plausible *why* question to set the boundary.
- Connect the subject to its context, causes, relationships, alternatives, and implications.
- Use examples or analogies only when they clarify those connections.
- Label perspectives and opinions explicitly.
- Keep procedures and exhaustive inventories in linked how-to guides and reference.

Complete this type when the reader can relate the subject to its context, reasons, and alternatives
without performing a task.

## Terminology

Use these source designations within the Diátaxis scope:

| Scope | Preferred designation |
| --- | --- |
| Systematic approach to technical documentation content, architecture, and form based on user needs | Diátaxis |
| Learning-oriented practical documentation in which a learner acquires skill by doing | tutorial |
| Goal-oriented directions for a competent reader completing a real task or solving a problem | how-to guide |
| Information-oriented technical description that a reader consults while working | reference |
| Understanding-oriented treatment that adds context and supports reflection | explanation |

Complete this branch when every unit has one recorded primary type and meets its type criterion.
Place secondary material in a distinct unit and link it from the point of need.
