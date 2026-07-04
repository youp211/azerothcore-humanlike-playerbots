# Internals: LLM Transport (query manager, API, HTTP)

Developer reference for how a built prompt travels from a caller in
mod-ollama-chat to the local Ollama server and back. This is the layer *below*
prompt construction: by the time I arrive here the prompt string already
exists and a bot's per-personality knobs have been resolved into an
`OllamaQueryOptions`. For the behavior-level framing of what generates these
prompts (personalities, playstyles, gear context, situational dialogue) see
[BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) and [BOT-ECONOMY.md](../BOT-ECONOMY.md);
this doc explains the transport code function-by-function for someone about to
modify or debug it.

Source files covered:

- `modules/mod-ollama-chat/src/mod-ollama-chat_querymanager.cpp` (+ `.h`) — concurrency cap, future/promise plumbing.
- `modules/mod-ollama-chat/src/mod-ollama-chat_api.cpp` (+ `.h`) — `/api/generate` request builder + response parser, plus the `SubmitQuery` / `QueryOllamaAPI` entry points.
- `modules/mod-ollama-chat/src/mod-ollama-chat_httpclient.cpp` (+ `.h`) — vendored cpp-httplib wrapper (`OllamaHttpClient`), timeout plumbing.

---

## 1. Purpose

This subsystem is the single choke point through which every LLM request in
mod-ollama-chat passes. It (a) bounds how many generations run at once
(`QueryManager` / `MaxConcurrentQueries`), (b) serializes a prompt plus its
per-call sampling overrides into the Ollama `/api/generate` JSON body and
parses the reply back into a plain string (`QueryOllamaAPI`), and (c) performs
the actual blocking HTTP POST over a vendored copy of cpp-httplib
(`OllamaHttpClient`). Everything here runs off the world thread on detached
workers, so the world tick is never blocked by model latency.

---

## 2. Entry points & call graph

Two public entry points are declared in `mod-ollama-chat_api.h`:

- `SubmitQuery(prompt, opts)` — **async**. Returns a `std::future<std::string>`; the actual work is dispatched onto a detached worker thread by `QueryManager`, subject to the concurrency cap.
- `QueryOllamaAPI(prompt, opts)` — **synchronous / blocking**. Does the request inline on the calling thread and returns the string. Callers that already run on their own detached thread use this to bypass the `QueryManager` cap.

Typical async path (chat reply, the common case — see
`mod-ollama-chat_handler.cpp:1470`):

```
PlayerBotChatHandler::OnPlayerCanUseChat            (world thread, hook)
  └─ ProcessChat() … GenerateBotPrompt()            (world thread: build prompt + {gear_context})
     GetPersonalityQueryOptions(bot)                (world thread: personality → OllamaQueryOptions)
     std::thread([...]{ … }).detach()               (spawns caller-side worker thread A)
        └─ SubmitQuery(prompt, queryOpts)           →  g_queryManager.submitQuery(...)
             ├─ (under cap)  std::thread(processQuery).detach()   (worker thread B)
             │                 └─ QueryOllamaAPI(prompt, opts)
             │                      ├─ httpClient.SetTimeout(g_OllamaRequestTimeout)
             │                      ├─ build nlohmann::json request body
             │                      └─ OllamaHttpClient::Post(url, body)   →  httplib POST
             │                 promise.set_value(result)
             └─ responseFuture.get()                (worker thread A blocks until B finishes)
        ObjectAccessor::FindPlayer(botGuid) …       (worker A reacquires by GUID, says the line)
```

Synchronous path (direct `QueryOllamaAPI`, bypassing the manager) is used by
callers that are *already* on a detached thread or a background task:

- `mod-ollama-chat_events.cpp:306` — event-memory generation.
- `mod-ollama-chat_sentiment.cpp:78` — sentiment scoring.
- `mod-ollama-chat_random.cpp:719` — random ambient chatter.

Other `SubmitQuery` callers (all async, each on its own detached thread):
`mod-ollama-chat_handler.cpp:1475` (chat reply) and `:2332`
(`OllamaChat_SpeakSituation`), `mod-ollama-chat_channels.cpp:534` (channel
chatter), `mod-ollama-chat_guildnames.cpp:149` (guild-name generation).

---

## 3. Function-by-function

### `SubmitQuery` — `mod-ollama-chat_api.cpp:245`

```cpp
std::future<std::string> SubmitQuery(const std::string& prompt, const OllamaQueryOptions& opts)
```

Thin free-function facade over the global manager. Forwards to
`g_queryManager.submitQuery(prompt, opts)` and returns its future. This is the
symbol other translation units link against; `g_queryManager` (defined at
`mod-ollama-chat_api.cpp:242`, declared `extern` in the header) is the single
process-wide instance.

### `QueryManager::QueryManager` — `mod-ollama-chat_querymanager.cpp:6`

```cpp
QueryManager::QueryManager()
    : maxConcurrentQueries(g_MaxConcurrentQueries), currentQueries(0)
```

Seeds `maxConcurrentQueries` from the config global `g_MaxConcurrentQueries` at
construction. Because `g_queryManager` is a global, this runs during static
init — before config is loaded — so the value here is the C++ default. The
*effective* cap is set later by `setMaxConcurrentQueries` from the config load
(Section 6).

### `QueryManager::setMaxConcurrentQueries` — `mod-ollama-chat_querymanager.cpp:12`

```cpp
void QueryManager::setMaxConcurrentQueries(int maxQueries)
```

Takes `mutex_`, overwrites `maxConcurrentQueries`. `0` means "no limit". Called
from the config loader (`mod-ollama-chat_config.cpp:549`,
`g_queryManager.setMaxConcurrentQueries(g_MaxConcurrentQueries)`), so it is
re-applied on every `.ollama reload`. Note it does **not** touch
`currentQueries` or drain `taskQueue`; lowering the cap while requests are in
flight just means new dispatch waits until `currentQueries` falls below the new
value.

### `QueryManager::submitQuery` — `mod-ollama-chat_querymanager.cpp:18`

```cpp
std::future<std::string> QueryManager::submitQuery(const std::string& prompt, const OllamaQueryOptions& opts)
```

The admission-control gate. Steps:

1. Create a `std::promise<std::string>` and grab its `future` up front (the future is always returned to the caller, whether the work runs now or is queued).
2. Under `mutex_`: if `maxConcurrentQueries == 0` (unlimited) **or** `currentQueries < maxConcurrentQueries`, increment `currentQueries` and set `shouldRunNow = true`. Otherwise push a `QueryTask{ prompt, opts, std::move(promise) }` onto `taskQueue` and leave `shouldRunNow == false`.
3. Outside the lock: if `shouldRunNow`, launch `std::thread(&QueryManager::processQuery, this, prompt, opts, std::move(promise)).detach()`.
4. Return the future.

Inputs: prompt + per-call options. Output: a future that will eventually hold
the model reply (or `""` on any failure). Side effect: may spawn a detached
thread and/or mutate `currentQueries`/`taskQueue`. Non-obvious: the thread is
launched *after* releasing the lock, so `processQuery`'s own re-lock in step 4
below can't deadlock against this one.

### `QueryManager::processQuery` — `mod-ollama-chat_querymanager.cpp:42`

```cpp
void QueryManager::processQuery(const std::string& prompt, OllamaQueryOptions opts, std::promise<std::string> promise)
```

The worker body (runs on a detached thread):

1. `std::string result = QueryOllamaAPI(prompt, opts);` — the blocking HTTP call.
2. `promise.set_value(result);` — unblocks whoever holds the future.
3. Under `mutex_`: decrement `currentQueries`. If `taskQueue` is non-empty **and** the cap now permits (`maxConcurrentQueries == 0 || currentQueries < maxConcurrentQueries`), pop the front `QueryTask`, increment `currentQueries`, and spawn another detached `processQuery` for it.

This is the pump: each finishing worker pulls at most one queued task, so a
backlog drains one slot at a time as workers complete. `opts` and `promise` are
taken **by value** (note the signature differs from `submitQuery`, which takes
`opts` by const-ref) so the detached thread owns its own copies — nothing here
outlives the caller's stack frame. Non-obvious ordering: `set_value` happens
*before* the requeue block, so a caller blocked on `future.get()` is released
as early as possible and the next queued task starts only after this thread has
already handed off its result.

### `QueryOllamaAPI` — `mod-ollama-chat_api.cpp:25`

```cpp
std::string QueryOllamaAPI(const std::string& prompt, const OllamaQueryOptions& opts)
```

Builds the request, does the POST, parses the reply. Step by step:

1. **HTTP client**: `static OllamaHttpClient httpClient;` — a single function-local static shared by all threads (see Section 5 for why that's safe).
2. **Timeout, per call**: `httpClient.SetTimeout(static_cast<int>(g_OllamaRequestTimeout));`. Applied on every call specifically so `.ollama reload` picks up `OllamaChat.RequestTimeout` without a server restart (comment at `:30`).
3. **Availability guard**: if `!httpClient.IsAvailable()`, log an error and return `""`. (In practice `m_available` is always `true` — see Section 7.)
4. **Endpoint/model**: read `g_OllamaUrl` and `g_OllamaModel` into locals.
5. **UTF-8 sanitize**: `SanitizeUTF8(prompt)` (from `mod-ollama-chat-utilities`) so the JSON encoder never chokes on invalid bytes.
6. **Base body**: `nlohmann::json requestData = {{"model", model}, {"prompt", sanitizedPrompt}, {"stream", false}};`.
7. **`options` sub-object** — populated only for values that differ from Ollama's own defaults, so I never override the model unnecessarily (`hasOptions` tracks whether anything was set):
   - `num_predict`: `opts.numPredictOverride > 0 ? opts.numPredictOverride : g_OllamaNumPredict`, emitted only if `> 0`. This is where a personality's `num_predict_override` reaches the wire.
   - `temperature`: if `opts.temperatureOverride >= 0.0f`, send it verbatim — **including an explicit `0.0`** (that's why the sentinel for "unset" is `-1.0f`, not `0`). Otherwise send `g_OllamaTemperature` **only if it differs from the default `0.8f`**.
   - `top_p`: sent only if `g_OllamaTopP != 0.95f`.
   - `repeat_penalty`: sent only if `g_OllamaRepeatPenalty != 1.1f`.
   - `num_ctx`: sent if `g_OllamaNumCtx > 0`.
   - `num_thread`: sent (as JSON key `"num_thread"`) if `g_OllamaNumThreads > 0`.
   - `seed`: `g_OllamaSeed` is a *string*; parsed with `std::stoi` inside a try/catch and sent as int `"seed"` on success; a parse failure is logged (debug only) and skipped.
   - If `hasOptions`, attach: `requestData["options"] = options;`.
8. **Root-level params** (Ollama expects these outside `options`):
   - `stop`: `g_OllamaStop` is comma-split, each token whitespace-trimmed, and sent as a JSON array `requestData["stop"]` if non-empty.
   - `system`: if `g_OllamaSystemPrompt` non-empty, sanitized and sent as `requestData["system"]`.
   - Think mode: if `g_ThinkModeEnableForModule`, set `requestData["think"] = true;` and `requestData["hidethinking"] = true;`.
9. **Serialize + POST**: `requestData.dump()` → `httpClient.Post(url, requestDataStr)`.
10. **Empty-response guard**: if the POST returns `""`, log the unreachable-API error (with `url`) and return `""`.
11. **Parse**: iterate the response body line by line (`std::getline` on a `stringstream`), skip blank/whitespace-only lines, `nlohmann::json::parse` each line, and append every non-empty `"response"` field to an `ostringstream`. This tolerates both the `stream:false` single-object body *and* a newline-delimited streaming body. Any parse exception logs the error (debug logs the raw buffer) and returns `""`.
12. **Post-process the concatenated text** (`botReply`):
    - `ExtractTextBetweenDoubleQuotes(botReply)` (see below) strips surrounding quotes.
    - **Think-tag check**: if `botReply` still contains `<think>` or `</think>`, treat it as a truncated generation — log the remediation hints (`ThinkModeEnableForModule=1`, `NumPredict=0`, `NumCtx=0`) and return `""`.
    - Empty check: if `botReply` is empty, log and return `""`.
13. Return `botReply` (debug-logs it when `g_DebugEnabled`).

Inputs: prompt + `OllamaQueryOptions`. Output: the model's reply, or `""` on
**any** failure (unreachable, non-200, JSON error, truncated think tags, empty).
Callers uniformly treat `""` as "stay silent". Side effects: network I/O,
logging; mutates the shared `httpClient`'s `m_timeout`.

### `ExtractTextBetweenDoubleQuotes` — `mod-ollama-chat_api.cpp:14`

```cpp
std::string ExtractTextBetweenDoubleQuotes(const std::string& response)
```

Finds the first `"` and the next `"` and returns the substring between them; if
two quotes aren't found, returns the input unchanged. Intended to unwrap models
that wrap their whole reply in quotes. **Gotcha** (Section 7): if the reply legitimately
contains an early quoted phrase, this truncates everything after the second
quote.

### `IsValidAPIResponse` — `mod-ollama-chat_api.cpp:232`

```cpp
bool IsValidAPIResponse(const std::string& response)
```

Returns `false` for an empty string, `true` otherwise. A trivial helper; the
main path in `QueryOllamaAPI` does not call it (callers may).

### `OllamaHttpClient::OllamaHttpClient` — `mod-ollama-chat_httpclient.cpp:12`

```cpp
OllamaHttpClient::OllamaHttpClient()
    : m_timeout(120), m_available(true)
```

Constructs with a 120-second default timeout and `m_available = true`. There is
no connectivity probe at construction — availability is a static `true`.

### `OllamaHttpClient::Post` — `mod-ollama-chat_httpclient.cpp:22`

```cpp
std::string OllamaHttpClient::Post(const std::string& url, const std::string& jsonData)
```

The vendored-httplib workhorse. Everything is wrapped in one try/catch that
returns `""` on any exception (logged). Steps:

1. **URL parse** via `std::regex` `^(https?)://([^:/]+)(?::(\d+))?(/.*)?$` → protocol, host, optional port, optional path. A non-match logs "Invalid URL format" and returns `""`.
2. **Port defaulting**: explicit `:port` wins; else `443` for `https`, `11434` (Ollama's default) for `http`. `path` defaults to `/`.
3. **Client construction, per call**:
   - `https` branch (guarded by `#ifdef CPPHTTPLIB_OPENSSL_SUPPORT`): `httplib::SSLClient`, with `enable_server_certificate_verification(false)` (so self-signed / ngrok certs work). If SSL support wasn't compiled in, logs a "rebuild with OpenSSL" error and returns `""`.
   - `http` branch: `httplib::Client`.
   - Both call `set_connection_timeout(m_timeout)`, `set_read_timeout(m_timeout)`, `set_write_timeout(m_timeout)` — reading the atomic once each.
4. **Headers**: `Content-Type: application/json`, `User-Agent: AzerothCore-OllamaChat/1.0`, `Accept: application/json`. If `host` contains `"ngrok"` (or `"ngrok-free.app"`), also add `ngrok-skip-browser-warning: true`.
5. **POST**: `client.Post(path, headers, jsonData, "application/json")`.
6. **Result handling**:
   - Falsy `httplib::Result` (no response / connection failure) → log, return `""`.
   - `response->status != 200` → log the status (debug-logs the body), return `""`.
   - Otherwise return `response->body`.

Inputs: full URL + serialized JSON. Output: raw response body string, or `""`
on any failure. Side effects: network I/O, logging. Non-obvious: a fresh
`httplib::Client`/`SSLClient` is created on the stack **per call**, so the
persistent `OllamaHttpClient` holds no live socket between requests — only the
`m_timeout` and `m_available` fields persist.

### `OllamaHttpClient::SetTimeout` — `mod-ollama-chat_httpclient.cpp:159`

```cpp
void OllamaHttpClient::SetTimeout(int seconds)
```

Stores `seconds` into the atomic `m_timeout`. Called from `QueryOllamaAPI` on
every request.

### `OllamaHttpClient::IsAvailable` — `mod-ollama-chat_httpclient.cpp:164`

```cpp
bool OllamaHttpClient::IsAvailable() const
```

Returns `m_available`, which is only ever `true` (set in the ctor, never
cleared). It is a placeholder guard, not a live health check.

---

## 4. Data structures & DB

- **`struct OllamaQueryOptions`** (`mod-ollama-chat_querymanager.h:11`): the per-call override bundle. Fields:
  - `uint32_t numPredictOverride = 0;` — `0` means "use global `g_OllamaNumPredict`".
  - `float temperatureOverride = -1.0f;` — `< 0` means "use global `g_OllamaTemperature`"; `0.0f` is a *valid, explicit* value.
  Populated by `GetPersonalityQueryOptions(bot)` (`mod-ollama-chat_personality.cpp:163`), which copies `numPredictOverride` / `temperatureOverride` out of the bot's row in `g_PersonalityTemplates` (falling back to defaults when the bot has no template). Those template fields originate from the `num_predict_override` / `temperature_override` columns of `mod_ollama_chat_personality_templates` — see [BOT-BEHAVIOR.md Section 2](../BOT-BEHAVIOR.md).
- **`struct QueryManager::QueryTask`** (`mod-ollama-chat_querymanager.h:25`): `{ std::string prompt; OllamaQueryOptions opts; std::promise<std::string> promise; }`. Move-only (owns a promise); lives in `taskQueue`.
- **`class QueryManager`** members: `int maxConcurrentQueries` (0 = unlimited), `int currentQueries`, `std::mutex mutex_`, `std::queue<QueryTask> taskQueue`.
- **`QueryManager g_queryManager`** (`mod-ollama-chat_api.cpp:242`) — the one process-wide instance.
- **`class OllamaHttpClient`** members: `std::atomic<int> m_timeout` (written per call from worker threads), `bool m_available`.

**Database**: this subsystem touches **no DB tables directly.** All state is
in-memory (the queue, the counters, the timeout atomic). DB reads that shape a
request happen upstream in prompt/personality code, not here.

---

## 5. Concurrency & threading

- **World thread**: only the *setup* runs here — the chat hook, `GenerateBotPrompt`, and `GetPersonalityQueryOptions`. No network I/O ever touches the world thread; the actual HTTP call is always on a detached worker. This is the whole point of the design (see the "detached worker thread, which only reads and enqueues a result" note at `mod-ollama-chat_guildnames.cpp:10`).
- **Worker threads**: `QueryManager::processQuery` runs on a thread detached in `submitQuery` (or re-spawned by a finishing `processQuery`). In the common chat path there are effectively **two** stacked detached threads: caller-side thread A (spawned in the handler) calls `SubmitQuery` and then blocks on `future.get()`, while `QueryManager` runs `processQuery` on thread B; thread B's `promise.set_value` releases thread A. Blocking a worker on `get()` is fine precisely because it is not the world thread.
- **Mutex**: `QueryManager::mutex_` guards `maxConcurrentQueries`, `currentQueries`, and `taskQueue` — the only shared mutable manager state. Threads are always launched *outside* the lock, so no thread creation happens while holding it.
- **Shared `OllamaHttpClient`**: the `static OllamaHttpClient httpClient` in `QueryOllamaAPI` is shared across all worker threads. It is safe because (a) `m_timeout` is a `std::atomic<int>` — concurrent `SetTimeout` writes and timeout reads don't tear; (b) the actual `httplib::Client`/`SSLClient` sockets are constructed as **per-call locals** on each worker's own stack, so no socket or buffer is shared between concurrent requests. The only cross-thread interaction is the atomic timeout, which is benign (worst case a request uses another thread's freshly-reloaded timeout value).
- **Reacquire-by-GUID**: worker threads never hold a `Player*` across the blocking call. The handler captures `botGuid`/`senderGuid` (raw `uint64`) into the lambda and re-resolves via `ObjectAccessor::FindPlayer(ObjectGuid(...))` *after* `get()` returns (`mod-ollama-chat_handler.cpp:1484` onward). A logout during generation just yields a null pointer that the caller checks. This obeys the project's "no long-lived `Player*`" rule.
- **`QueryOllamaAPI` bypass**: callers at `events.cpp:306`, `sentiment.cpp:78`, `random.cpp:719` call `QueryOllamaAPI` directly, so those generations are **not** counted against `MaxConcurrentQueries`. If you rely on the cap to protect the Ollama box, note that only `SubmitQuery` callers are throttled.

---

## 6. Config keys

All read via `sConfigMgr->GetOption<T>(...)` in `mod-ollama-chat_config.cpp`
(and re-read on `.ollama reload`). Keys and defaults relevant to transport:

| Key | Type | Default | Global | Used by |
|---|---|---|---|---|
| `OllamaChat.Url` | string | `http://localhost:11434/api/generate` | `g_OllamaUrl` | `QueryOllamaAPI` endpoint |
| `OllamaChat.Model` | string | `llama3.2:1b` | `g_OllamaModel` | request `"model"` |
| `OllamaChat.NumPredict` | uint32 | `40` | `g_OllamaNumPredict` | `options.num_predict` fallback |
| `OllamaChat.Temperature` | float | `0.8` | `g_OllamaTemperature` | `options.temperature` (sent only if ≠ 0.8) |
| `OllamaChat.TopP` | float | `0.95` | `g_OllamaTopP` | `options.top_p` (sent only if ≠ 0.95) |
| `OllamaChat.RepeatPenalty` | float | `1.1` | `g_OllamaRepeatPenalty` | `options.repeat_penalty` (sent only if ≠ 1.1) |
| `OllamaChat.NumCtx` | uint32 | `0` | `g_OllamaNumCtx` | `options.num_ctx` (sent if > 0) |
| `OllamaChat.NumThreads` | uint32 | `0` | `g_OllamaNumThreads` | `options.num_thread` (sent if > 0) |
| `OllamaChat.Stop` | string | `""` | `g_OllamaStop` | root `"stop"` (comma-split → array) |
| `OllamaChat.SystemPrompt` | string | `""` | `g_OllamaSystemPrompt` | root `"system"` |
| `OllamaChat.Seed` | string | `""` | `g_OllamaSeed` | `options.seed` (parsed via `std::stoi`) |
| `OllamaChat.MaxConcurrentQueries` | uint32 | `0` (no limit) | `g_MaxConcurrentQueries` | `QueryManager` cap |
| `OllamaChat.RequestTimeout` | uint32 | `120` | `g_OllamaRequestTimeout` | `OllamaHttpClient` conn/read/write timeout (seconds) |
| `OllamaChat.ThinkModeEnableForModule` | bool | `false` | `g_ThinkModeEnableForModule` | request `"think"`/`"hidethinking"` |
| `OllamaChat.DebugEnabled` | bool | `false` | `g_DebugEnabled` | verbose logging throughout |

Note the C++ initializer defaults in `mod-ollama-chat_config.cpp` (e.g.
`g_OllamaModel = "llama3.2:1b"`) match the `GetOption` defaults; the deployed
realm overrides `OllamaChat.Model` to the fine-tuned wow-chat model in
`mod_ollama_chat.conf` (see the wow-chat memory note and BUILD-NOTES).

`OllamaChat.MaxConcurrentQueries` is pushed into the live manager at
`mod-ollama-chat_config.cpp:549` via
`g_queryManager.setMaxConcurrentQueries(g_MaxConcurrentQueries)`, so a reload
changes the cap without restart. `OllamaChat.RequestTimeout` is applied per call
inside `QueryOllamaAPI`, so it too is reload-live.

---

## 7. Failure modes & gotchas

- **Everything degrades to silence**: every failure in `QueryOllamaAPI` / `OllamaHttpClient::Post` returns `""`, and every caller treats `""` as "bot says nothing". There is no retry and no fallback string — a down Ollama server just means quiet bots, not errors in-game.
- **`ExtractTextBetweenDoubleQuotes` over-truncation**: it returns only the text *between the first two `"`*. A reply like `He said "hi" and left` becomes `hi`. This is a real content-mangling edge case for any model that emits mid-sentence quotes; it exists to strip whole-reply quoting but is not quote-aware.
- **Think-tag truncation guard**: a reply still containing `<think>`/`</think>` after extraction is discarded (returns `""`) on the assumption the generation was cut off mid-thought. The logged remediation is `ThinkModeEnableForModule=1`, `NumPredict=0`, `NumCtx=0`. If you enable think mode, the request sends `think:true` + `hidethinking:true` so Ollama strips the block server-side and this guard shouldn't fire.
- **`temperature` sentinel**: the "unset" marker for `temperatureOverride` is `-1.0f`, *not* `0`. A personality can deliberately request `temperature 0.0` (fully deterministic) and it will be sent. Don't "fix" the `>= 0.0f` test to `> 0.0f`.
- **Defaults are silently omitted**: `temperature`/`top_p`/`repeat_penalty` are only sent when they differ from the hard-coded default constants (`0.8f`/`0.95f`/`1.1f`). If you change a default via config to exactly one of those literals, that key is *not* sent and Ollama's own default applies — usually the same value, but a trap if Ollama's default ever diverges.
- **Seed is a string**: `OllamaChat.Seed` is stored as text and parsed with `std::stoi`; a non-numeric value is caught, debug-logged, and skipped (no seed sent). An empty string means "no seed".
- **`IsAvailable()` is not a health check**: `m_available` is hard-`true`. The `if (!httpClient.IsAvailable())` guard in `QueryOllamaAPI` can never fire in the current code; real unreachability is detected only by the empty-body return from `Post`.
- **HTTPS requires a build flag**: the `https://` branch is compiled only under `CPPHTTPLIB_OPENSSL_SUPPORT`. Without OpenSSL at build time, an `https` URL logs "rebuild with OpenSSL" and returns `""`. Plain `http://localhost:11434` (the default) needs no SSL.
- **ngrok special-casing**: any host containing `ngrok` gets `enable_server_certificate_verification(false)` (SSL branch) and the `ngrok-skip-browser-warning` header, so a tunnelled Ollama works without extra config.
- **URL regex is strict**: only `http`/`https` with an optional port and path parse. A malformed `g_OllamaUrl` returns `""` (logged "Invalid URL format") and the bot stays silent.
- **Cap doesn't apply to direct callers**: as noted in Section 5, `QueryOllamaAPI` callers bypass `MaxConcurrentQueries`. Under heavy ambient-chatter/sentiment load the effective concurrency can exceed the configured cap.
- **Lowering the cap mid-flight**: `setMaxConcurrentQueries` doesn't interrupt running workers; it only gates new dispatch. In-flight requests above the new cap run to completion.

---

## 8. Cross-references

- [../BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) — Section 2 personalities (source of `num_predict_override` / `temperature_override`), Section 8 the wow-chat voice model that fills `OllamaChat.Model`.
- [../BOT-ECONOMY.md](../BOT-ECONOMY.md) — Section 4 `OllamaChat_SpeakSituation`, a `SubmitQuery` caller that voices item hand-overs.
- Sibling internals docs (same `docs/internals/` directory) cover the layers this one sits under and over: prompt construction / `GenerateBotPrompt` and the chat hook (`ProcessChat`, `OnPlayerCanUseChat`) upstream, and the personality/template system (`GetPersonalityQueryOptions`, `g_PersonalityTemplates`) that produces the `OllamaQueryOptions` consumed here.
