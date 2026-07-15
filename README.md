# AbletonBridge

**366 tools connecting Claude AI to Ableton Live** (347 core + 19 optional ElevenLabs voice/SFX tools)

AbletonBridge gives Claude direct control over your Ableton Live session through the Model Context Protocol. Create tracks, write MIDI, design sounds, mix, automate, browse instruments, snapshot presets, and navigate deep into device chains and modulation matrices — all through natural language conversation.

---

## What It Can Do

**Music Creation** — *"Create a MIDI track, load Operator, and write an 8-bar bass line in E minor"* · *"Build a Metro-Boomin-style 808 beat using grid notation"* · *"Make a 4-bar jazz chord progression — Cm7, Fm7, Dm7b5, G7 — with voice leading"*

**Sound Design** — *"Load Wavetable and design a warm detuned supersaw pad"* · *"Snapshot the current preset, tweak it brighter, then morph back 50%"* · *"Set the Compressor side-chain input to the kick drum track"*

**Deep Device Access** (M4L) — *"Show me what's inside the Drum Rack — all chains and nested devices"* · *"Set Wavetable's LFO1 modulation to filter cutoff at 0.6"* · *"Analyze the full spectrum of track 5 using cross-track routing"*

**Mixing & Arrangement** — *"Create a filter sweep automation from 0.2 to 0.9 over 8 bars"* · *"Create a reverb return track and send drums to it at 30%"* · *"Get arrangement clips on all tracks and give me a structure overview"*

**Creative Generation** — *"Generate a Euclidean rhythm with 16 steps and 5 pulses"* · *"Write a I-vi-IV-V chord progression with drop2 voicings"* · *"Create a trap drum pattern, 2 bars, with swing"*

**Session Management** — *"Full overview of all tracks — names, devices, volumes"* · *"Search the browser for 'vocoder' and load it on the master track"* · *"Snapshot every device on tracks 0-3 as 'verse preset'"*

---

## Architecture

```text
Claude AI  <--MCP-->  MCP Server  <--TCP:9877-->  Ableton Remote Script
                          |            <--UDP:9882-->  (real-time params)
                          +---<--UDP/OSC:9878/9879-->  M4L Bridge (optional)
                          +---<--HTTP:9880-->  Web Status Dashboard

MCP Server (modular architecture):
  server.py          — MCP orchestrator + owner-only backend lifecycle
  ownership.py       — process ownership, status, and safe handoff
  state.py           — centralized global state + locks
  constants.py       — command tiers, browser categories
  validation.py      — input validation + size limits
  connections/       — ableton.py (TCP), m4l.py (UDP/OSC)
  cache/             — browser.py (cache + disk persistence)
  dashboard/         — html.py, server.py (Starlette)
  tools/             — 16 modules (347 tools)
  prompts.py         — 4 MCP prompt templates
  instructions.py    — server instructions (cross-tool guidance)
```

- **Remote Script** (TCP+UDP) — runs inside Ableton as a Control Surface. TCP:9877 for commands, UDP:9882 for real-time parameter updates at 50+ Hz.
- **M4L Bridge** (UDP/OSC) — optional Audio Effect device for hidden parameters, rack chain internals, audio analysis, modulation matrices, event monitoring, and more.
- **ElevenLabs Server** (optional) — 19 tools for AI voice generation, sound effects, voice cloning. Requires `ELEVENLABS_API_KEY`.
- **MCP Server Instructions** — cross-tool guidance injected into the AI's context on every connection. Covers workflow sequencing, compound tool preferences, M4L fallback logic, and input constraints.
- **MCP Resources** — `ableton://session`, `ableton://tracks`, `ableton://capabilities` for direct data access.
- **MCP Prompts** — guided workflows: `create-beat`, `mix-track`, `sound-design`, `arrange-section`.
- **Web Dashboard** — real-time status, tool metrics, and server logs at `http://127.0.0.1:9880`.

---

## Multiple MCP Clients

Every MCP client receives the complete tool set, while one local process owns Ableton control at a time:

- `get_server_capabilities` reports whether this process is the owner or a standby, plus owner process metadata when available.
- The first normal Ableton tool call automatically claims control when it is free.
- Standby tools return a structured ownership error instead of terminating the MCP server.
- `release_ableton_control` hands control back explicitly. Ownership is also released when the owning MCP process shuts down.
- Port `9881` stays owned if any dashboard, connection, or background worker has not stopped; release returns `released: false` so cleanup can be retried safely.
- Control is never stolen and has no idle timeout.

---

## Tool Overview (347 core + 19 optional = 366 total)

| Area | Examples | Count |
|---|---|---|
| Session & Transport | ownership, tempo, play/record, capture, Link, punch, playback position | 52 |
| Tracks | create/rename tracks, routing, monitoring, groups, implicit arm | 29 |
| Clips | create/edit clips, notes, follow actions, warp markers | 56 |
| Scenes | create/delete/duplicate, fire, name, color, tempo, follow actions | 10 |
| Mixer | unified set_mixer, sends, crossfader | 13 |
| Devices & Parameters | load/configure, rack chains, rack macros, sidechain, plugin info | 50 |
| Browser & Presets | search/load instruments, presets, device presets | 12 |
| Automation | clip/track automation, envelopes, curves | 12 |
| Arrangement | arrangement clips, time editing, composition analysis | 17 |
| Creative Generation | Euclidean rhythms, chords, drums, arpeggios, bass, transforms | 17 |
| Deep Access (M4L) | hidden params, chain internals, audio analysis, note surgery | 40 |
| Snapshots & Macros | snapshot/restore, morph, macros, parameter maps | 19 |
| Audio Analysis | audio clip info, track meters, input meters | 3 |
| Grid Notation | ASCII drum/melodic pattern I/O | 2 |
| Compound Workflows | create instrument/drum track, batch mixer, effect chains | 10 |
| MIDI CC | mapped plugin control, channel assignment, raw CC | 5 |
| **Core subtotal** | | **347** |
| ElevenLabs (optional) | voice generation, SFX, cloning, transcription | 19 |
| **Total** | | **366** |

See [CHANGELOG.md](CHANGELOG.md) for the complete per-tool breakdown.

---

## Stability & Reliability

AbletonBridge is built to handle real-world sessions without crashing Ableton:

- **Chunked async LiveAPI** — large device discovery split into 4-param chunks with 50ms delays
- **Chunked response protocol** — large responses split, base64-encoded, reassembled automatically
- **URL-safe base64** — `A-Z a-z 0-9 - _` only; avoids Max OSC routing conflicts
- **Deferred processing** — all M4L outlets use `Task.schedule()` to avoid blocking audio/UI thread
- **LiveAPI cursor reuse** — `goto()` reuses 3 cursors instead of creating ~193 new instances
- **Fire-and-forget writes** — no post-set readback (the #1 crash pattern)
- **Command-specific timeouts** — per-command timeouts (e.g., freeze_track → 60s, load_instrument → 30s) instead of fixed 10s/15s
- **Socket drain** — clears stale UDP responses before each command
- **Single backend owner** — every MCP process stays available while one process exclusively owns Live, M4L, and dashboard connections
- **Disk-persisted cache** — 6,400+ browser items in gzip; instant startup (~50ms)
- **Auto-reconnect** — exponential backoff for TCP and UDP connections
- **Tiered command delays** — 3-tier system (0ms/10ms/20ms) eliminates unnecessary waits for property setters
- **Async tool handlers** — all tools run via `asyncio.to_thread()`, preventing sync I/O from blocking the event loop
- **Concurrency control** — async semaphore serializes tool dispatch; threading locks protect TCP and UDP sockets from corruption
- **Tool execution timeout** — 120s hard timeout prevents stuck tools from blocking the entire pipeline
- **Bounded thread pool** — explicit 8-worker limit prevents resource exhaustion during rapid tool call bursts
- **Standardized responses** — all 347 tools return consistent `tool_success()`/`tool_error()` JSON envelopes via decorator
- **Chunk reassembly hardening** — duplicate detection, progress logging, missing chunk index reporting
- **Parameter resolution cache** — 500-entry FIFO cache for brute-force display→value resolution (O(1) after first call)
- **Effect chain persistence** — saved templates survive server restarts via `~/.ableton-bridge/chain_templates.json`
- **225 tests** — 12 test files covering ownership, multi-client stdio, connections, M4L, cache, creative tools, workflows, and validation edge cases

---

## Flexibility

- **Any MCP client** — Claude Desktop, Cursor, Claude Code, or any MCP-compatible tool
- **307 tools without Max for Live** — full session control via TCP/UDP Remote Script; M4L is optional
- **+40 deep-access tools with M4L** — hidden parameters, rack internals, audio analysis, event monitoring
- **+19 optional ElevenLabs tools** — AI voice generation, sound effects, cloning, transcription
- **Ableton Live 10, 11, and 12** — graceful API fallbacks for version-specific features
- **Cross-platform** — Windows and macOS
- **Quick setup** — `uv run` for server, one folder for Remote Script, one M4L device for bridge

---

## Version

**v4.0.0** — see [CHANGELOG.md](CHANGELOG.md) for full release history.

---

## Optional: ElevenLabs Voice & SFX Server

19 tools for AI voice generation, sound effects, voice cloning, and transcription. Generated audio saves to your Ableton User Library.

See [installation_process.txt](installation_process.txt) for setup instructions, or add to your MCP config:

```json
{
  "elevenlabs": {
    "command": "uv",
    "args": ["run", "elevenlabs-mcp"],
    "env": { "ELEVENLABS_API_KEY": "your_key_here" }
  }
}
```
