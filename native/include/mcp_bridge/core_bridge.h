#pragma once
// Core-side view of the shell. Core code (dispatcher, handlers, plugin access)
// includes this header and never a shell header (bridge_gup.h,
// main_thread_executor.h, dialog_watch.h, scene_journal.h, gdiplus_runtime.h,
// agent_viewport.h, render_host.h, undo_records.h, qt_dialogs.h). Every call
// here goes through the McpHostApi bound by McpCoreApi::initialize
// (core_bridge.cpp).
#include <windows.h>
#include <functional>
#include <string>
#include <nlohmann/json.hpp>
#include "mcp_bridge/host_api.h"

// Same contract as MainThreadExecutor, served by the shell's executor.
class HostExecutor {
public:
    std::string ExecuteSync(std::function<std::string()> work, DWORD timeout_ms = 120000);
    static void EnableDirectMode();
    static void DisableDirectMode();
    static bool IsDirectMode();
};

// Handlers keep their signature `std::string Fn(const std::string& params,
// MCPBridgeGUP* gup)` and call `gup->GetExecutor().ExecuteSync(...)`. In the
// core, MCPBridgeGUP is this stateless facade, owned by the core, over
// McpHostApi::executeSync; the Max plugin object is the shell's
// MCPBridgeShellGUP (bridge_gup.h). The core never sees the plugin's layout,
// and no vtable, std::function or shell object pointer crosses the boundary,
// while the ~100 handler call sites compile unchanged.
class MCPBridgeGUP {
public:
    HostExecutor& GetExecutor() { return executor_; }

private:
    HostExecutor executor_;
};

// Mirrors of the shell services, named after the shell namespaces they reach.
// Failures rethrow the shell's exception text as std::runtime_error.
namespace McpHost {

// Binds the shell's services (McpCoreApi::initialize, test harnesses); nullptr unbinds.
void Bind(const McpHostApi* host);

// Runs `work` on Max's main thread: inline there, otherwise through the shell's
// executor. A direct handler's ExecuteSync runs inline on its pipe thread, so
// direct mode is left on this thread while the work waits. For work that must
// never leave the main thread: MAXScript (HandlerHelpers::RunMAXScript).
std::string RunOnMainThread(const std::function<std::string()>& work);

namespace SceneJournal {
void Reset();
unsigned long long Epoch();
bool IsRegistered();
bool FlushPending();
unsigned long long CurrentSeq();
unsigned long long CurrentMutationSeq();
unsigned long long AppendSynthetic(const std::string& type,
                                   const nlohmann::json& details = nlohmann::json::object());
nlohmann::json ChangesSince(unsigned long long since, size_t limit = 256);
nlohmann::json MutationChangesSince(unsigned long long since, size_t limit = 256);
}  // namespace SceneJournal

// scene_delta's per-session cursors, kept by the shell so they survive a core
// reload. Thread-safe.
namespace SceneDeltaCursors {
struct Cursor {
    unsigned long long journalSeq = 0;
    unsigned long long epoch = 0;  // SceneJournal::Epoch() when it was set
};
bool Get(const std::string& session, Cursor& cursor);
void Set(const std::string& session, const Cursor& cursor);
}  // namespace SceneDeltaCursors

namespace DialogWatch {
// One bridge request on the calling thread (DialogWatch::RequestContext).
class RequestContext {
public:
    RequestContext(const std::string& request, const std::string& command);
    ~RequestContext();
    RequestContext(const RequestContext&) = delete;
    RequestContext& operator=(const RequestContext&) = delete;

private:
    const McpHostApi* host_;
    void* handle_;
};
void ThrowIfDismissed();
nlohmann::json OpenDialogs();
std::string Control(const std::string& command);
}  // namespace DialogWatch

namespace GdiPlusRuntime {
bool EnsureStarted();
}  // namespace GdiPlusRuntime

// The shell's agent viewport (agent_viewport.h). Max's main thread only.
namespace AgentViewport {
using CameraState = McpAgentCameraState;  // opaque; only the shell reads it
nlohmann::json Execute(const nlohmann::json& params);
ViewExp& Get();
bool IsOwned();
bool IsRequested(const nlohmann::json& params);
void Redraw();
nlohmann::json Snapshot();
void Configure(const nlohmann::json& params);
CameraState SaveCamera();
void RestoreCamera(const CameraState& state);
nlohmann::json Project(const nlohmann::json& points);
}  // namespace AgentViewport

// render_start's armed job, owned by the shell (render_host.h).
namespace RenderHost {
// True with the job id while a render_start job is armed.
bool ActiveJob(std::string& jobId);
void Arm(const std::string& jobId, const std::string& output, const std::string& signalPath,
         int framesExpected);
struct CancelReport {
    int outcome = kMcpRenderCancelRefused;
    bool hadActiveJob = false;
    std::string jobId;
    int framesDone = 0;
    int framesExpected = 0;
};
CancelReport Cancel(const std::string& expectedJobId);
}  // namespace RenderHost

// Objects Max keeps after a request, owned by the shell (undo_records.h).
namespace UndoRecords {
void PutNodeFlagsRestore(INode* node, unsigned before, unsigned after);
void PutEnvironmentRestore(Texmap* before, BOOL beforeOn, Texmap* after, BOOL afterOn);
bool IsPin(ReferenceMaker* maker);

// A shell Pin (a ReferenceMaker holding one reference) for the life of this object.
class Pin {
public:
    explicit Pin(ReferenceTarget* target);
    ~Pin();
    Pin(const Pin&) = delete;
    Pin& operator=(const Pin&) = delete;
    ReferenceTarget* GetReference(int) const;

private:
    const McpHostApi* host_;
    void* handle_;
};
}  // namespace UndoRecords

}  // namespace McpHost
