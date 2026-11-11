# thinking-tags

A [ga-harness](https://github.com/Fwind43/ga-harness) extension plugin that brings [GenericAgent PR #861](https://github.com/lsdefine/GenericAgent/pull/861) (*feat(llmcore): wrap native reasoning in `<thinking>` tags and extract inline thinking*) into `ga-harness` **without modifying GenericAgent's source code**.

## What It Does

1. **Wrap Native Reasoning in `<thinking>` Tags (`wrap_native_reasoning`)**
   - **Claude (`_parse_claude_sse` / `_parse_claude_json`)**: Wraps native `thinking_delta` streams and `thinking` blocks in `<thinking>...</thinking>`, closing the tag immediately on `content_block_stop`, before the first `text_delta`, or at stream end.
   - **OpenAI Responses API (`_parse_openai_sse` / `_parse_openai_json`, `api_mode="responses"`)**: Wraps `response.reasoning_text.*`, `response.reasoning_summary_text.*`, and fallback `reasoning` output items in `<thinking>...</thinking>`, closing immediately on `response.output_item.done`.
   - **OpenAI Chat Completions (`_parse_openai_sse` / `_parse_openai_json`, `api_mode="chat_completions"`)**: Wraps `reasoning_content` / `reasoning` deltas and message fields in `<thinking>...</thinking>`, closing before the first `content` chunk or at stream end.
   - Enables `ga-harness`'s `ga-events` `_TagSplitter` to automatically route native reasoning into collapsible `thinking` UI blocks instead of mixing reasoning into normal assistant reply text.

2. **Extract Inline `<think>` / `<thinking>` Tags (`extract_inline_thinking`)**
   - Extracts inline `<think>...</think>` and `<thinking>...</thinking>` blocks from `response.content` into `response.thinking` and strips them from `response.content`.
   - Re-evaluates text-based tool-call fallback parsing against cleaned content so `<tool_use>` examples inside `<think>` blocks are never executed by mistake.

3. **Clean `llmcore.fast_ask()` Output (`strip_fast_ask_thinking`)**
   - Strips `<think>...</think>` and `<thinking>...</thinking>` blocks from `llmcore.fast_ask()` return values so auxiliary summarization/utility calls receive only the final answer text.

4. **Idempotent & Safe with Upstream PR #861**
   - Automatically detects if the underlying `llmcore.py` already has PR #861 applied (both via bytecode inspection and runtime tag checks) and operates as a zero-duplication passthrough.
   - Cleanly restores original `llmcore` functions when the plugin is disabled or unloaded.

## Installation

### From ga-harness Plugin Market / Git Import

Import directly via repository URL in **Settings → Plugins → Import from Git**:

```text
https://github.com/Fwind43/gah-plugin-thinking-tags
```

Or clone manually into your user plugins directory:

```bash
git clone https://github.com/Fwind43/gah-plugin-thinking-tags ~/.ga-harness/plugins/thinking-tags
```

## Settings

| Setting | Type | Default | Description |
| --- | --- | --- | --- |
| `wrap_native_reasoning` | `boolean` | `true` | Wrap native Claude/OpenAI reasoning blocks and SSE deltas in `<thinking>...</thinking>` tags |
| `extract_inline_thinking` | `boolean` | `true` | Extract inline `<think>` / `<thinking>` tags from text responses into `response.thinking` |
| `strip_fast_ask_thinking` | `boolean` | `true` | Strip `<think>` / `<thinking>` blocks from `llmcore.fast_ask()` return values |
