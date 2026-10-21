---
name: efficiency-pass
description: Make a program, service or page faster or lighter the measured way - all CPU cores, GPU when it really wins, data in RAM, caching, live-updating pages, hot reload without restart, batching, limits. Use when asked to make something faster, more efficient or "optimal", or when designing a new service.
---

# Efficiency pass

## Rule 0: measure, change one thing, measure again
Baseline on real data first: wall time or p50/p95 latency, throughput, CPU %, RSS, GPU use. Profile to find the hot
spot (Python: `py-spy top/record`, `scalene`; startup: `python -X importtime`; browser: DevTools Performance).
Change one thing, measure with the same load, keep it only when it clearly wins (rule of thumb > 10 % and no fragile
complexity). Record before/after in the commit or the project's Keuzes. A pure guess is not a reason.

## Checklist (when it pays, how you see it worked, the trap)

### CPU and parallelism
- **asyncio** for many waits on I/O (HTTP, scraping, sockets). Trap: one blocking call (sync HTTP, `time.sleep`,
  SQLite, CPU work) stalls everything: `await asyncio.to_thread(...)`; `PYTHONASYNCIODEBUG=1` logs callbacks > 100 ms.
- **Threads** for blocking I/O and for C code that releases the GIL (numpy, onnxruntime, hashing). In practice: a
  thread pool of 6 for per-symbol analysis took 20 s to 3-4 s. Trap: pure-Python CPU work does not speed up; shared
  state needs a lock (orders behind one lock); imports at module level (a missing import inside the pool killed a
  whole day of work silently).
- **Processes** (`ProcessPoolExecutor`) for CPU-bound Python; size to `os.process_cpu_count()` (3.13+). Trap: pickling
  large arguments costs more than the work: send paths or ids; each worker copies its memory.
- **Slow periodic job in the background** (thread or task) and let the main loop reuse the last result: a 12-minute
  screener in the main loop once left 39 % of the time without stop-loss checks.
- **Parallel per site/host with a cap**: 4 → 8 sites at once took one job 7.5 → 2.5 min. Pace per domain where sites
  block bursts (e.g. 10 s between pages); `asyncio.Semaphore` / connection limits, never an unbounded `gather`.
- **Free-threaded Python** (3.14, opt-in): not for production yet; C extensions that are not ready re-enable the GIL.
- **numpy vectorisation, then numba** for numeric loops (indicators, backtests); numba `cache=True` against compile
  time on every start.

### GPU
- Wins for **big batches and big models** (embeddings in bulk, Whisper, LLMs) with data that stays on the device.
  Benchmark batch 1, 8, 32, 128 on CPU and GPU before you choose. Loses for small single requests (transfer + launch).
- **Pick the backend that measures best**: on AMD, llama.cpp with Vulkan beat ROCm (and ROCm OpenCL failed); ONNX
  Runtime: set providers explicitly and log `session.get_providers()` (it falls back to CPU silently; ROCm EP is gone
  since 1.23, MIGraphX replaces it; OpenVINO for Intel).
- **A shared iGPU is often slower than the CPU**: embeddings on an iGPU ran at 1.2 chunks/s against 3.2-5.5 on 2-6
  CPU threads and ran out of memory at batch 16, while transcoding and other services needed it too. Check the GPU's
  current users first (`intel_gpu_top`, `radeontop`, `amdgpu_top`); one job at a time on a shared device.
- An iGPU may get only part of system RAM (GTT limit) and can be switched off by the BIOS when a dGPU is added.

### Memory and caching
- **Keep the working set in RAM** when it fits: an in-memory index of ~1,300 files (24 MB) took search from 15.4 s to
  21-50 ms at ~120 MB RAM. Keep a safety rescan (e.g. every 60 s, stat only) next to the event-driven refresh.
- **Cache expensive results by content hash**, recompute only what changed (embeddings per chunk hash).
- **TTL caches** for slow lookups that rarely change (daily bars, registry answers: hours to days); a disk copy for a
  warm start (12-minute cold start → ~10 s). Traps: types change on the way through disk (timestamps come back as
  strings); a broad `except` around a cache served frozen data for days: fail loudly; a wrong equality check wiped
  the cache on every cycle.
- **`functools.lru_cache`** for pure functions; check `cache_info()` (aim > 80 % hits); bounded size.
- **Precompute cheap forms**: search a lowercase copy instead of case-insensitive regex (12x faster).
- **SQLite**: `journal_mode=WAL`, `synchronous=NORMAL`, larger `cache_size`, `mmap_size`, `busy_timeout`,
  `PRAGMA optimize`; batch writes in one transaction. Trap: WAL on a network filesystem is not supported.
- **Keep models loaded** (load once at startup; Ollama `keep_alive`), but not when the model is rarely used and the
  RAM/VRAM is needed elsewhere.

### Network and HTTP
- **One shared client/pool per process** (httpx `AsyncClient`, requests `Session` with a pool as large as the threads),
  HTTP keep-alive or HTTP/2; retries with backoff and jitter, honour `Retry-After`, never retry non-idempotent calls
  (orders).
- **HTTP caching**: versioned assets `Cache-Control: public, max-age=31536000, immutable` (`app.js?v=<release>`),
  HTML and APIs `no-cache` + `ETag` → 304. **Compression** (gzip/brotli) for text, not for images or video, not twice.
- **DNS**: Python does not cache DNS; a headless browser with a fresh profile per page made ~250,000 DNS queries an
  hour. Disable its background networking, reuse profiles, run a local caching resolver for heavy scrapers.
- **Event-driven instead of polling** when the source can notify (inotify/watchfiles → invalidate within ~0.4 s).
  Poll only when nothing can push, and then with ETag/If-Modified-Since.

### Live pages
- **Server-Sent Events** for server → browser updates (plain HTTP, auto-reconnect, `Last-Event-ID`); WebSockets only
  for two-way high-frequency traffic. Send a heartbeat (~15 s) so proxies keep it open; reconnect with backoff;
  after a reconnect fetch what was missed. Keep the user's view (layout, scroll, unsaved edits) when data refreshes.
- **Reload on a new release**: the stream's hello (or `/version`) carries the build id; the page reloads itself, but
  not while someone is typing or has a dialog open.
- Open streams can hold up a restart: give the server a short graceful-shutdown timeout (uvicorn
  `timeout_graceful_shutdown`).
- Next.js: `revalidatePath` / `revalidateTag` after a change instead of turning caching off.

### Hot reload and no-restart changes
- **Config reload without restart**: reload the config module or file each cycle or on SIGHUP; validate first and keep
  the old config when the new one is invalid. Secrets and connections usually still need a restart: say which.
- **Code changes**: a watchdog that restarts within seconds (systemd `Restart=always` + a file watcher) for services;
  `uvicorn --reload`, `docker compose watch`, Next.js Fast Refresh only in development.
- **Stateful services** (open orders, running jobs): drain on SIGTERM and resume work after start; refuse a deploy
  while a long job runs (a guard in the deploy script).
- Rebuild derived data when its source changes (a watcher event), not on a timer.

### Startup, limits and the rest of the machine
- **Lazy imports** for heavy modules; Docker layer order and BuildKit cache mounts; keep model caches in a volume.
- **Fences for background work**: `nice -n 10 ionice -c3`, compose `mem_limit`/`cpus`, and for heavy tests
  `systemd-run --user --scope -p MemoryMax=<n>G` (an unbounded benchmark once OOM-killed the agent's own session).
  Alert on OOM kills (exit code 137), do not trust only an "OOMKilled" flag inside containers.
- **Batching and incremental work**: process only what changed (mtime/hash), insert in batches, debounce watchers and
  search inputs, prefetch the next item while working on the current one.
- **Energy counts too**: idle power, disks that can spin down (stop polling them), CPU power profiles.

## Rejected for good reasons (do not suggest again without new numbers)
- A reranker that found 2 more hits but cost ~6 s per query and ~2 GB RAM; hybrid keyword + vector search that was
  never better than vector search alone on a real question set.
- Websocket/tick-level exits for a trading bot: a replay of 203 trades showed no gain over a 60 s check.
- A local LLM on a shared iGPU for voice: ~130 s for a 7k-token tool prompt.

## Report
Before/after numbers on the same load, what was changed, what was tried and rejected (with numbers), and what was not
measured.
