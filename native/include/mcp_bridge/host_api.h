#pragma once
// The shell/core boundary, shell side: services the shell (mcp_bridge.gup)
// provides to the core. Same rules as core_api.h: C types only, and no
// exception crosses. A function with an `error` sink returns 0, or writes what()
// of the shell's exception and returns nonzero; core_bridge.cpp rethrows that
// text as std::runtime_error, so messages reach clients unchanged.
//
// Max SDK objects (INode, Texmap, ...) pass as pointers: Max owns them and both
// sides are built against the same SDK (McpCoreApi::maxSdkVersion).
#include "mcp_bridge/core_api.h"

class INode;
class ReferenceMaker;
class ReferenceTarget;
class Texmap;
class ViewExp;

// 2: the scene_delta cursors are implemented and the core relies on them.
constexpr int kMcpHostApiVersion = 2;

// Core work the host runs inside MainThreadExecutor (dialog guard included).
// Writes its result or the failure's what(); returns 0 on success.
typedef int (*McpWorkFn)(void* work, McpSink result, McpSink error);

// AgentViewport::CameraState, copied as bytes. Only the shell interprets it.
struct McpAgentCameraState {
    alignas(16) unsigned char bytes[256];
};

// Node flag bits for the shell's NodeFlagsRestore (scene_patch set_flags).
enum : unsigned {
    kMcpNodeHidden = 1u << 0,
    kMcpNodeFrozen = 1u << 1,
    kMcpNodeRenderable = 1u << 2,
    kMcpNodeCastShadows = 1u << 3,
    kMcpNodeReceiveShadows = 1u << 4,
};

// RenderHost::Cancel outcomes (render_cancel).
enum : int {
    kMcpRenderCancelRefused = 0,      // expected job is not the active, started one
    kMcpRenderCancelNoInterface = 1,  // GetCOREInterface() returned null
    kMcpRenderCancelRequested = 2,    // AbortRender() raised
};

struct McpHostApi {
    int version;  // kMcpHostApiVersion
    void* ctx;    // passed back to every function

    // ── MainThreadExecutor ───────────────────────────────────────
    // Nonzero on the executor's own (Max main) thread, where ExecuteSync runs
    // work inline; the core does that itself so nothing crosses the boundary.
    int (*isMainThread)(void* ctx);
    // MainThreadExecutor::ExecuteSync of `work(workCtx, ...)`: blocks until it
    // ran on Max's main thread, or runs it on this thread in direct mode.
    int (*executeSync)(void* ctx, McpWorkFn work, void* workCtx, unsigned long timeoutMs,
                       McpSink result, McpSink error);
    // The calling thread's direct mode (MainThreadExecutor::Enable/DisableDirectMode,
    // IsDirectMode). The flag is a shell thread_local.
    void (*setDirectMode)(void* ctx, int enabled);
    int (*getDirectMode)(void* ctx);

    // ── DialogWatch ──────────────────────────────────────────────
    // A DialogWatch::RequestContext on the calling thread. Returns its handle,
    // or null with `error`; endRequest must run on the same thread.
    void* (*beginRequest)(void* ctx, const char* requestId, size_t requestIdLen,
                          const char* commandType, size_t commandTypeLen, McpSink error);
    void (*endRequest)(void* ctx, void* request);
    // DialogWatch::ThrowIfDismissed: nonzero with the MAX_DIALOG_ERROR payload.
    int (*throwIfDialogDismissed)(void* ctx, McpSink error);
    // DialogWatch::OpenDialogs as JSON.
    int (*openDialogs)(void* ctx, McpSink out, McpSink error);
    // DialogWatch::Control (the pipe's native:max_dialogs channel).
    int (*dialogControl)(void* ctx, const char* command, size_t commandLen, McpSink out, McpSink error);

    // ── SceneJournal ─────────────────────────────────────────────
    int (*journalReset)(void* ctx, McpSink error);
    int (*journalEpoch)(void* ctx, unsigned long long* epoch, McpSink error);
    int (*journalIsRegistered)(void* ctx);
    int (*journalFlushPending)(void* ctx, int* flushed, McpSink error);
    int (*journalCurrentSeq)(void* ctx, unsigned long long* seq, McpSink error);
    int (*journalCurrentMutationSeq)(void* ctx, unsigned long long* seq, McpSink error);
    // `details` is a JSON object.
    int (*journalAppendSynthetic)(void* ctx, const char* type, size_t typeLen, const char* details,
                                  size_t detailsLen, unsigned long long* mutationSeq, McpSink error);
    // JSON arrays of journal entries.
    int (*journalChangesSince)(void* ctx, unsigned long long since, size_t limit, McpSink out, McpSink error);
    int (*journalMutationChangesSince)(void* ctx, unsigned long long since, size_t limit, McpSink out,
                                       McpSink error);

    // ── GdiPlusRuntime ───────────────────────────────────────────
    int (*gdiplusEnsureStarted)(void* ctx, int* started, McpSink error);

    // ── AgentViewport (the whole agent viewport lives in the shell) ──
    // Max's main thread. JSON in and out; the view is Max's own ViewExp.
    int (*agentViewportIsOwned)(void* ctx, int* owned, McpSink error);
    int (*agentViewportIsRequested)(void* ctx, const char* params, size_t paramsLen, int* requested,
                                    McpSink error);
    int (*agentViewportExecute)(void* ctx, const char* params, size_t paramsLen, McpSink out, McpSink error);
    int (*agentViewportGet)(void* ctx, ViewExp** view, McpSink error);
    int (*agentViewportRedraw)(void* ctx, McpSink error);
    int (*agentViewportSnapshot)(void* ctx, McpSink out, McpSink error);
    int (*agentViewportConfigure)(void* ctx, const char* params, size_t paramsLen, McpSink error);
    int (*agentViewportSaveCamera)(void* ctx, McpAgentCameraState* state, McpSink error);
    int (*agentViewportRestoreCamera)(void* ctx, const McpAgentCameraState* state, McpSink error);
    int (*agentViewportProject)(void* ctx, const char* points, size_t pointsLen, McpSink out, McpSink error);

    // ── RenderHost (render_start's armed job and its Max notifications) ──
    // Writes the armed job's id and sets *active when a job is in flight.
    int (*renderJobActive)(void* ctx, int* active, McpSink jobId, McpSink error);
    // Arms the next render's done-signal.
    int (*renderJobArm)(void* ctx, const char* jobId, size_t jobIdLen, const char* output, size_t outputLen,
                        const char* signalPath, size_t signalPathLen, int framesExpected, McpSink error);
    // render_cancel's locked section: refuses unless `expectedJobId` is empty or
    // names the active, started job; otherwise reports the job, raises Max's
    // abort flag and marks the job cancel-requested. *outcome: kMcpRenderCancel*.
    int (*renderJobCancel)(void* ctx, const char* expectedJobId, size_t expectedJobIdLen, int* outcome,
                           int* hadActiveJob, McpSink jobId, int* framesDone, int* framesExpected,
                           McpSink error);

    // ── Undo records (objects Max keeps after the request) ───────
    // theHold.Put of a NodeFlagsRestore (kMcpNode* bits). Inside the caller's hold.
    int (*putNodeFlagsRestore)(void* ctx, INode* node, unsigned before, unsigned after, McpSink error);
    // theHold.Put of an EnvironmentRestore pinning both maps.
    int (*putEnvironmentRestore)(void* ctx, Texmap* before, int beforeOn, Texmap* after, int afterOn,
                                 McpSink error);
    // A Pin: a ReferenceMaker keeping `target` alive. Returns its handle, or null with `error`.
    void* (*makePin)(void* ctx, ReferenceTarget* target, McpSink error);
    void (*destroyPin)(void* ctx, void* pin);
    ReferenceTarget* (*pinTarget)(void* ctx, void* pin);
    // Nonzero when `maker` is one of the shell's Pins (here or inside an EnvironmentRestore).
    int (*isPin)(void* ctx, ReferenceMaker* maker);

    // ── scene_delta cursors ──────────────────────────────────────
    // Per session: the journal position and journal epoch of its last
    // scene_delta. The shell keeps them, so a core reload does not reset
    // clients' deltas; the pipe releases a session's cursor on disconnect.
    // Thread-safe; *found is 0 for an unknown session.
    int (*sceneDeltaGetCursor)(void* ctx, const char* session, size_t sessionLen, int* found,
                               unsigned long long* journalSeq, unsigned long long* epoch, McpSink error);
    int (*sceneDeltaSetCursor)(void* ctx, const char* session, size_t sessionLen,
                               unsigned long long journalSeq, unsigned long long epoch, McpSink error);
    void (*sceneDeltaReleaseCursor)(void* ctx, const char* session, size_t sessionLen);
};
