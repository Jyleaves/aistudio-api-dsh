# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/Jyleaves/aistudio-api-dsh/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/Jyleaves/aistudio-api-dsh/compare/v1.0.0...v1.0.1
