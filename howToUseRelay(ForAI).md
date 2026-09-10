# Aeowun Local Relay — AI Agent Guide

This document describes how AI assistants should use the Aeowun Local Relay when collaborating with the AEOWUN engineering environment.

Relay is a communication bridge around ChatGPT.

Its fundamental topology is:

```text
SOURCE → RELAY → CHATGPT → RELAY → DESTINATION
```

**ChatGPT remains the reasoning middleman.**

Relay does not replace the reasoning layer and does not directly route a source to a destination.

---

## 1. What Relay Does

Relay allows local engineering systems and external AI assistants to communicate with a live authenticated ChatGPT session without requiring a direct cloud API integration.

Possible sources and destinations include:

* Gemini
* MAGY
* AEOWUN
* human operators
* development tools
* tool calls
* tool results
* future automation workflows

Examples:

```text
Gemini → Relay → ChatGPT → Relay → Gemini

MAGY → Relay → ChatGPT → Relay → MAGY

AEOWUN → Relay → ChatGPT → Relay → AEOWUN

Tool Result → Relay → ChatGPT → Relay → Next Action
```

Relay transports communication. The application using Relay remains responsible for its own reasoning and behavior.

---

## 2. Give ChatGPT Ground Truth

ChatGPT does not automatically have access to the local AEOWUN filesystem, source tree, running processes, or local development environment.

When asking ChatGPT about local engineering work, provide enough context for the question to be answerable.

Useful context includes:

* project name
* repository or workspace
* relevant file paths
* relevant source excerpts
* observed error messages
* actual logs
* expected behavior
* actual behavior

Prefer evidence over descriptions.

For example:

```text
We are working on the AEOWUN VS Code fork.

File:
src/vs/workbench/contrib/chat/...

Observed:
...

Intended:
...

Difference:
...

What should we investigate?
```

Do not assume ChatGPT can inspect a local file merely because its path is mentioned.

---

## 3. Use Relay for Engineering Reasoning

Good Relay requests provide a specific engineering problem and its evidence.

Examples:

```text
We observed Turn 5 failing because Relay selected a code block
as the composer. Here is the relevant log and selector code.
Determine why that happened and identify the smallest justified fix.
```

```text
This transaction succeeded:

baseline → composer verified → submit accepted → response stable

The next transaction failed at submit.
Compare the two execution paths and identify the first divergence.
```

Avoid vague requests such as:

```text
Fix this.
Make the architecture better.
What should we do?
```

Provide the actual problem and evidence whenever possible.

---

## 4. Do Not Treat Hypotheses as Facts

This is an important engineering rule.

Distinguish:

**PROVEN**

Supported by code, logs, tests, or reproducible execution.

**PLAUSIBLE**

A reasonable explanation that has not yet been demonstrated.

**UNVERIFIED**

A claim for which sufficient evidence is not available.

Example:

```text
Long conversations might cause DOM virtualization.
```

This is a hypothesis.

Do not report:

```text
DOM virtualization caused the failure.
```

until evidence demonstrates that relationship.

When debugging Relay or AEOWUN, prefer:

```text
Observed behavior
→ evidence
→ hypothesis
→ reproduction
→ fix
→ validation
```

---

## 5. Keep One Engineering Goal Per Request

Relay can carry complex requests, but debugging is usually clearer when one request has one primary goal.

Prefer:

```text
Investigate why Turn 5 selected the wrong composer.
```

Then:

```text
Fix the confirmed Turn 5 composer-selection bug.
```

Then:

```text
Run the Turn 5 regression test.
```

Do not combine unrelated architectural redesign, UI work, performance analysis, and bug fixing into one request unless there is a real dependency between them.

---

## 6. Persistent STDIO Mode

Relay supports a persistent JSON Lines interface:

```powershell
python relay.py --stdio
```

Persistent mode is intended for systems that exchange multiple messages through one long-running Relay process.

The implementation should follow the protocol actually defined by the current Relay code.

Do not assume a message type exists merely because an older document mentioned it.

When integrating with Relay, inspect the current protocol definitions and examples first.

---

## 7. Oversized Messages

Relay may need to split large messages before sending them through ChatGPT.

The critical invariant is:

```text
complete transmitted chunk
(including required metadata and footer)
<= configured prompt limit
```

Do not assume:

```text
raw payload <= limit
```

is sufficient.

Chunked data must preserve:

* ordering
* part numbering
* total-part information
* payload contents
* required completion metadata

Do not silently truncate a message.

Do not silently discard a chunk.

If chunking or reassembly fails, report the failure.

---

## 8. Response Handling

Relay's intended behavior is to return the relevant assistant response produced by ChatGPT.

Do not assume that the first changing DOM value is the final response.

Do not assume that a response has completed merely because one particular button or selector exists.

The implementation uses browser/CDP observation and response validation to determine when a response can be returned.

When investigating response failures, identify the actual transaction phase involved:

```text
baseline
recovery/state observation
composer
submit
response observation
correlation
stability
```

Use execution evidence to determine where the failure occurred.

---

## 9. Current Composer Considerations

ChatGPT conversation content may contain editable or content-bearing DOM elements such as code blocks.

Relay must not accidentally select message-history content as the composer.

A previously observed failure occurred when a `contenteditable` code block inside conversation history was mistaken for the input area.

That was a confirmed bug and was addressed by tightening composer targeting and excluding message-history content.

When modifying composer discovery:

* preserve the actual composer behavior
* avoid selecting conversation history
* prefer reliable positive identifiers
* validate the selected element
* test against actual conversation content containing code blocks

Do not broaden selectors merely because a broader selector appears more compatible.

---

## 10. Validate Changes

Do not tell Zack that a change is working unless it has been tested.

Use this distinction:

```text
Implemented
    ≠
Tested
    ≠
Validated
```

A useful engineering report should say:

```text
Implemented: yes
Tested: yes
Validated: yes
Evidence: ...
```

or:

```text
Implemented: yes
Tested: no
Validation: pending
```

Never invent successful test results.

---

## 11. Preserve the Relay Contract

Regardless of which application uses Relay, preserve:

```text
SOURCE
  ↓
RELAY
  ↓
CHATGPT
  ↓
RELAY
  ↓
DESTINATION
```

Do not turn Relay into:

```text
SOURCE → RELAY → DESTINATION
```

unless the architecture is explicitly changed and the change is deliberate.

Relay is not the reasoning engine.

ChatGPT remains the middleman.

---

## 12. Efficient Investigation

When tracing a large codebase, start with direct evidence.

Useful techniques include:

```powershell
grep -n "symbol" ...
grep -r "symbol" ...
```

and equivalent repository search tools.

Search for:

* definitions
* callers
* protocol types
* error messages
* transaction states
* relevant selectors
* test cases

Prefer targeted searches over repeatedly reading large unrelated portions of the repository.

When the repository already contains logs or tests, use those as the starting point.

---

## 13. When Communicating With ChatGPT

A good request normally contains:

```text
CONTEXT
What system are we working on?

OBSERVED
What actually happened?

EXPECTED
What was supposed to happen?

EVIDENCE
Logs, code, tests, or reproduction steps.

QUESTION
What specific engineering decision or diagnosis is needed?
```

Example:

```text
Context:
Relay drives an authenticated ChatGPT browser session.

Observed:
Turn 5 failed with "Send control was not unique."

Evidence:
The previous assistant response contained a code block.
The composer selector also matches contenteditable elements.

Expected:
The real ChatGPT composer should be selected.

Question:
Identify the failure path and determine the smallest robust fix.
```

---

## 14. Do Not Let Relay Become Application-Specific

Relay should understand:

* messages
* communication
* transport
* ChatGPT interaction
* protocol state
* response handling
* diagnostics

Relay should NOT need to understand:

* MAGY's reasoning
* AEOWUN business logic
* Gemini's internal behavior
* what a tool actually does
* application-specific task planning

Those responsibilities belong to their respective systems.

---

## 15. Engineering Standard

When working with Relay, prefer:

**Evidence over assumption.**

**Small justified changes over speculative redesign.**

**Observable behavior over claims.**

**Explicit failures over silent recovery.**

**Simple interfaces over unnecessary abstractions.**

**Tests over confidence.**

The objective is not to make Relay appear sophisticated.

The objective is to make it reliable, understandable, and useful as communication infrastructure around ChatGPT.
