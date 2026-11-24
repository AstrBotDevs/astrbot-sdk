# AstrBot SDK

This package contains the public API and transport-independent runtime used by
AstrBot SDK plugins.

The current minimum implementation includes:

- `metadata.yaml` v2 parsing;
- immutable message and event DTOs;
- `Plugin`, `on.command`, `on.message`, and lifecycle registration;
- plugin entrypoint loading;
- coroutine and async-generator handler invocation;
- protocol v1 frames and SDK DTO codecs;
- isolated stdio Runner and Host client;
- bidirectional Peer requests with connection-bound capability grants;
- cross-process yield/ack flow control;
- `ctx.storage` (Host-held plugin KV) and `ctx.messages.send` services;
- `ctx.conversations` (conversation.read/write) with a generic dataclass
  protocol codec;
- `ctx.assets` (assets.transfer) with chunked upload/download and automatic
  upload of local media sources before results cross the protocol;
- a segment codec registry with forward-compatible downgrading of unknown
  segment types (Face and Forward included);
- `on.message` handlers (message.receive) with host-side filter compilation
  (message types, platforms, roles, regex);
- `ctx.plugins` (plugin.inspect) and `ctx.render` (render.image) services;
- `on.tool` static LLM tools and `ctx.tools` dynamic tool registration
  (llm.tool.register) with annotation-derived parameter schemas;
- `ctx.llm` (llm.generate): provider queries, one-shot generate, and
  flow-controlled streaming over the capability yield/ack protocol;
- `ctx.llm.embed/transcribe/synthesize` (llm.embed, speech.transcribe,
  speech.synthesize), with ambient UMO provider selection;
- `ctx.llm.run_agent` (llm.agent): the built-in tool-loop agent with a
  plugin-scoped toolset, reserving custom agent runner fields;
- `hooks.*` Pipeline hooks with write-tracked in-place DTO modification and
  modify-capability-gated write application (12 stages including lifecycle
  and error notification hooks);
- command parameter schema in the Runner handshake;
- an AstrBot Core bridge (`astrbot.core.star.sdk_bridge`) that loads SDK
  plugins into the real Pipeline with command matching, typed arguments,
  yield/ack semantics, and Host capability dispatch.

The Runner supports local stdio and external WebSocket connections. SDK plugins
use metadata schema v2 and a `Plugin` entry point.
