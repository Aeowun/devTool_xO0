# Aeowun Local Relay (`devTool_xO0`)

**A local communication bridge between engineering tools and ChatGPT.**

Aeowun Local Relay (`Relay`) connects local applications and engineering tools to an authenticated ChatGPT session through Chrome DevTools Protocol (CDP).

Its fundamental topology is:

```text
SOURCE → RELAY → CHATGPT → RELAY → DESTINATION
```

**ChatGPT remains the reasoning middleman.**

Relay is the communication layer around that interaction. It transports messages, context, requests, and responses; it does not contain the reasoning logic of the applications using it.

## What Relay Is For

Relay is intended to provide a common communication path for systems such as:

* Human interaction
* MAGY
* AEOWUN
* Gemini
* engineering tools
* tool calls and tool results
* future local automation workflows

The core abstraction is deliberately simple:

```text
Message
  ↓
Relay
  ↓
ChatGPT
  ↓
Relay
  ↓
Response / Destination
```

The implementation may change as the system evolves, but this communication contract is fundamental.

## Current Capabilities

### CDP ChatGPT Transport

Relay can communicate with a live authenticated ChatGPT session through Chrome DevTools Protocol.

It can:

* discover a usable ChatGPT page
* locate the composer
* submit a message
* observe the resulting response
* correlate the response with the active transaction
* verify response stability before returning it

### Guarded Transactions

Relay treats each interaction as a controlled transaction.

The current flow includes:

```text
observe
→ prepare
→ submit
→ verify acceptance
→ observe response
→ verify stability
→ return response
```

Only one transaction is permitted at a time through the guarded path, reducing prompt stacking and turn desynchronization.

### Composer Targeting

The ChatGPT page contains other editable/content-bearing elements, including code blocks inside conversation history.

Relay therefore explicitly avoids selecting editable elements belonging to message history and prioritizes the actual ChatGPT composer.

This prevents a previously observed failure where a code block from an earlier response was incorrectly selected as the input field.

### Oversized Messages

Messages larger than the effective ChatGPT input limit are split into controlled chunks.

Chunk construction accounts for the metadata and footer attached to each transmitted part so that the complete transmitted chunk remains within the configured limit.

Chunked messages can be reassembled without losing ordering or payload data.

### Persistent STDIO Mode

Relay supports a persistent JSON Lines interface:

```powershell
python relay.py --stdio
```

This allows a long-running process to handle multiple transactions without spawning a new Relay process for every request.

### Single Transactions

A single transaction can be executed directly:

```powershell
python relay.py --desktop-cdp 127.0.0.1:9222 "Analyze this codebase..."
```

### Chrome / CDP Discovery

Relay can use an existing Chrome CDP endpoint and can provision its expected local browser environment when required.

The goal is to avoid unnecessary browser duplication while preserving the authenticated session used by Relay.

## Protocol and Safety Properties

Relay is designed around a few important invariants:

**ChatGPT remains in the path**

```text
SOURCE → RELAY → CHATGPT → RELAY → DESTINATION
```

Relay does not directly transform the source into the destination.

**One guarded transaction at a time**

A transaction is completed before another guarded transaction is accepted.

**Explicit response completion**

Relay does not treat the first changing response text as final. It waits for the response observation to satisfy its stability criteria.

**Bounded transmitted chunks**

When chunking is required, the complete transmitted chunk—including required metadata—is constrained by the configured prompt limit.

**Observable failure**

Failures are surfaced through structured transaction state, diagnostics, and logging rather than silently ignored.

## Validation

Relay is actively tested against real ChatGPT sessions as well as local test/certification paths.

Recent real-world validation has included:

* repeated transactions against a long-lived ChatGPT conversation
* long-response generation
* repeated multi-turn transactions
* large generated responses
* Unicode and emoji-containing content
* regression testing for composer misidentification
* chunked-message testing

One observed production-style failure involved a `contenteditable` code block in conversation history being mistaken for the ChatGPT composer. That failure was corrected by tightening composer targeting and excluding message-history content.

Validation should be understood as evidence for specific behaviors, not as a guarantee that every possible browser or ChatGPT UI state is supported.

## Architecture

Relay is being maintained as a modular system rather than a single God Script.

The implementation is organized around meaningful responsibilities such as:

```text
relay.py
    ↓
core / protocol
chunking
CDP / ChatGPT interaction
ADB
UI automation
diagnostics / logging
CLI
testing
```

The exact module boundaries may evolve as implementation requirements change.

The architectural goal is:

**high cohesion, low coupling, simple boundaries, and observable behavior.**

## Runtime Interface

### Persistent mode

```powershell
python relay.py --stdio
```

JSON Lines are exchanged through `stdin` / `stdout`.

### Single transaction

```powershell
python relay.py --desktop-cdp 127.0.0.1:9222 "Your message"
```

### Environment

Relay currently relies primarily on Python's standard library, with optional platform-specific components where required by a transport.

## Design Principles

Relay follows several principles:

**Communication, not reasoning**

Relay carries communication between participants and ChatGPT. It does not become the application's reasoning engine.

**Evidence over assumptions**

Observed behavior and test results take precedence over assumptions about the browser or ChatGPT UI.

**Preserve the contract, improve the implementation**

Existing behavior is preserved where it is correct. Bugs, unsafe assumptions, coupling, and fragile logic should be corrected when evidence justifies doing so.

**Simple architecture**

Modules exist to separate meaningful responsibilities, not merely to increase the number of files.

**Fail visibly**

A failed transaction should be diagnosable rather than silently producing an incorrect result.

## Roadmap

Planned work may include:

* improved recovery when the CDP/browser session disappears
* broader attachment/file handling
* additional transport capabilities
* continued hardening against ChatGPT UI changes
* additional automated integration testing

Relay is an active engineering component of the AEOWUN ecosystem and its implementation will continue to evolve with the systems that use it.
