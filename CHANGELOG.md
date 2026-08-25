# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.6] - 2026-08-25

### Fixed

- Normalize omitted, nullable, and union-style JSON Schema types before
  encoding AI Studio function declarations. Third-party dsh tools can no
  longer make an otherwise valid request fail with HTTP 400 by exposing an
  untyped nested argument.
- Preserve each OpenAI-compatible tool invocation's name, original arguments,
  and matching result in Gemini conversation history. Multi-step agents can
  now advance task lists and continue from prior tool state instead of
  repeatedly restarting the first step.
- Accept the optional OpenAI tool-message `name` field as a fallback when a
  client cannot provide a matching `tool_call_id`.

## [1.0.5] - 2026-08-25

### Fixed

- Emit monotonically increasing OpenAI stream indexes for consecutive function
  calls, preventing clients from concatenating one tool's JSON arguments onto
  the next tool call, while keeping stream-only indexes out of non-streaming
  completion payloads.
- Recover OpenAI-, Gemini-, and Anthropic-compatible streams from AI Studio's
  specific pre-output ambiguous-service 404 by clearing the stale capture
  template and retrying on another account.
- Keep the backend, desktop version badge, update state, and both Windows
  installer manifests on one release version.

## [1.0.4] - 2026-08-25

### Fixed

- Decode AI Studio's sparse protobuf values recursively for every function
  tool, including arrays of nested objects. This prevents valid dsh arguments
  such as `todo_write.todos` from becoming arrays of `null` values.

## [1.0.3] - 2026-08-24

### Added

- Advertise supported reasoning efforts and the default effort in OpenAI model
  metadata for model-aware clients such as dsh.
- Show a compact version badge beside the Asteria name, sourced from the
  running backend version.

### Changed

- Use the highest AI Studio thinking level when an OpenAI-compatible client
  omits `reasoning_effort`.
- Route inline media of 8 MiB or more through a dedicated single-request queue
  by default, preventing concurrent large PDFs from multiplying memory across
  API and Chromium workers. The threshold and concurrency remain configurable.
- Reject declared HTTP request bodies larger than 40 MiB before parsing their
  JSON, returning HTTP 413 instead of risking process-wide memory exhaustion.

### Fixed

- Package the complete pywebview module and run a frozen-runtime smoke check
  before creating installers, preventing startup from importing an empty
  `webview` namespace without `create_window`.
- Keep the update action clickable after a successful update check when no
  error is present. The previous empty-string state could still emit an HTML
  `disabled` attribute.
- Accept `minimal`, `low`, `medium`, and `high` OpenAI reasoning efforts and
  preserve the selected level through request sanitization and tool calls.
- Create the configured temporary directory before decoding Gemini inline
  images, preventing first-run image requests from failing with HTTP 500 after
  the temporary directory has been cleaned.
- Clear generation settings inherited from the browser capture template before
  applying API options. In particular, a stale newline stop sequence no longer
  truncates otherwise valid responses after their first line.

## [1.0.2] - 2026-08-24

### Added

- Add resumable update downloads using HTTP range requests, with automatic
  retry and exponential backoff after transient network failures.
- Show the current version, latest version, package size, downloaded bytes,
  retry state, and resume state on the desktop update page.
- Add Windows file and product version metadata to `Asteria.exe`.
- Define v1.0.0 as the minimum cumulative-update baseline and direct older
  installations to the full installer instead of attempting an unsafe jump.

### Changed

- Download updates to a `.part` file, flush them to disk, verify SHA-256, and
  atomically rename them before installation. A verified cached package is
  reused instead of downloaded again.
- Retain at most two recent update installers, remove installers after their
  version is running, and discard completed installers or partial downloads
  older than seven days.
- Batch streaming Playground updates to reduce repeated Markdown rendering,
  merge pending scroll operations, slow account-status polling to five
  seconds, and pause it while the window is hidden.
- Bound persisted Playground history to 100 recent messages and approximately
  one million characters, excluding embedded base64 images from synchronous
  browser storage.
- Exclude Playwright TypeScript declarations and optional trace/recorder web
  assets from the Windows application bundle.
- Remove those development-only Playwright assets during cumulative updates
  when an older installation left them behind.
- Refresh desktop static-asset cache keys so upgraded clients immediately load
  the v1.0.2 performance and interface changes.
- Pin the full installer to the original CloakBrowser 146.0.7680.177 runtime,
  verify its executable with SHA-256, record the source archive checksum, and
  never substitute Playwright-downloaded Chromium.
- Preserve every file from the pinned CloakBrowser archive; incremental
  updates continue to leave the user's installed browser untouched.
- Build the full installer locally from that non-public browser archive; the
  GitHub workflow builds only the cumulative updater and never downloads a
  replacement browser.

### Fixed

- Preserve partial update downloads after a connection interruption so the
  next attempt can continue instead of restarting from zero.
- Restart a partial download safely when a server ignores the requested byte
  range, preventing duplicated or corrupted packages.
- Prevent long streaming responses and accumulated local chat history from
  causing progressively more expensive UI work.
- Skip a warm browser immediately after its account fails when another account
  can be started, while retaining single-account self-recovery behavior.

### Security

- Keep incomplete and unverified update data separate from executable files;
  only a package matching the published SHA-256 digest becomes installable.

## [1.0.1] - 2026-08-24

### Added

- Add `AISTUDIO_MAX_IDLE_BROWSERS` to control how many idle Chromium workers
  remain resident. The default is `1`; setting it to `0` closes every worker
  after its request completes.
- Add standby account reporting to the runtime status API and management UI.

### Changed

- Warm only the active account during startup. Other accounts now launch on
  demand for concurrent traffic, rate-limit failover, or account recovery.
- Prefer an eligible warm worker for sequential traffic and trim temporary
  burst workers back to the configured idle limit after use.
- Reduce background Chromium work by disabling images, animations, audio,
  synchronization, extensions, component updates, and crash reporting.
- Limit retained Anthropic tool contexts to 256 entries.
- Require a matching SHA-256 checksum asset before accepting a desktop update.
- Restart Asteria as the original non-elevated user after a silent update.

### Fixed

- Release streaming XHR objects, response buffers, event queues, and waiters
  after success, failure, timeout, or cancellation to prevent long-running
  browser memory growth.
- Close the desktop application through its normal shutdown path before an
  update replaces files, allowing Chromium workers and profile locks to exit
  cleanly.
- Stop the management UI from treating standby accounts as indefinitely
  initializing and polling them continuously.
- Preserve the installed Chromium bundle, accounts, login state, API keys,
  settings, and `%LOCALAPPDATA%\Asteria` data during incremental updates.

### Security

- Reject incremental update packages when the SHA-256 checksum is missing or
  does not match the downloaded file.

[Unreleased]: https://github.com/Jyleaves/aistudio-api-dsh/compare/v1.0.6...HEAD
[1.0.6]: https://github.com/Jyleaves/aistudio-api-dsh/compare/v1.0.5...v1.0.6
[1.0.5]: https://github.com/Jyleaves/aistudio-api-dsh/compare/v1.0.4...v1.0.5
[1.0.4]: https://github.com/Jyleaves/aistudio-api-dsh/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/Jyleaves/aistudio-api-dsh/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/Jyleaves/aistudio-api-dsh/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/Jyleaves/aistudio-api-dsh/compare/v1.0.0...v1.0.1
