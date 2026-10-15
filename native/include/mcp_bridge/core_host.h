#pragma once
#include <string>

struct McpCoreApi;
struct McpHostApi;
class MainThreadExecutor;

// The shell's side of the boundary. Shell callers (GUP, pipe server) reach the
// core only through these functions.
namespace CoreHost {

// core_host.cpp: the shell services handed to the core, with `executor` bound
// as the table's ctx. Bind once, before any core is initialized (Start, or a
// harness); cores read ctx from any thread, so the shell reuses the returned
// table instead of calling this again. `executor` may be null for harnesses
// that never marshal to Max's main thread.
const McpHostApi* HostApi(MainThreadExecutor* executor);
// Forgets a closed pipe session's scene_delta cursor (shell state).
void ReleaseSceneDeltaCursor(const std::string& session);

// core_control.cpp: the core's lifecycle and every pipe request.
//
// GUP Start, Max's main thread, before the pipe. Binds the linked core, or in
// the MCP_HOT_CORE layout loads core/mcp_core.dll through the shadow-copy
// loader. On failure returns false with `error`; the pipe still serves ping,
// native:max_dialogs and native:core (whose status reports the error).
bool Start(MainThreadExecutor& executor, std::string& error);
// GUP Stop, Max's main thread, after the pipe stopped: shuts the core down.
// A hot core stays mapped until Max exits.
void Shutdown();

// Pipe workers. Always returns one JSON response. ping, native:max_dialogs
// and native:core are answered here; everything else enters the core through
// the drain gate.
std::string Dispatch(const std::string& request, const std::string& clientSessionId);
// Pipe worker, after its client disconnected.
void ReleaseSession(const std::string& clientSessionId);

}  // namespace CoreHost
