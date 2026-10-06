#pragma once
// The shell/core boundary of mcp_bridge, core side (host_api.h is the reverse).
//
// SHELL (mcp_bridge.gup): plugin registration, named pipe, main-thread
// executor, dialog monitor and its Qt reader, scene journal, GDI+ lifetime,
// agent viewport, render_start's armed job, undo records and every callback
// Max holds. CORE: command dispatch and the handlers. The shell reaches the
// core only through McpCoreApi; the core reaches the shell only through
// McpHostApi.
//
// Only C types cross: UTF-8 bytes through sinks, integers and opaque pointers.
// No std::string, nlohmann::json, exception or C++ object owned by one side is
// handed to the other, so MCP_HOT_CORE can link the same core objects into a
// separate DLL without changing either side. Both sides are still built by the
// same compiler with identical settings (CMakeLists.txt, mcp_bridge_settings).
#include <cstddef>
#include <cstring>
#include <string>

// 2: hostApiVersion and iteratorDebugLevel; the core uses the host's
// scene_delta cursors; shutdown leaves nothing registered with Max.
constexpr int kMcpCoreApiVersion = 2;

// Receives UTF-8 bytes. write may run several times; the receiver appends and
// copies before it returns. The side that created the sink owns its storage.
struct McpSink {
    void (*write)(void* ctx, const char* utf8, size_t n);
    void* ctx;
};

struct McpHostApi;

struct McpCoreApi {
    // Frozen prefix: these five fields lead every version of the table, so a
    // shell can report them for a core of any version before refusing it.
    int version;                // kMcpCoreApiVersion; the shell refuses any other
    const char* build;          // build stamp of the core
    const char* bridgeVersion;  // MCP_BRIDGE_VERSION the core was built from
    int maxSdkVersion;          // MAX_SDK_VERSION the core was built against
    int msvcVersion;            // _MSC_FULL_VER the core was built with

    // Version 2 onward.
    int hostApiVersion;         // kMcpHostApiVersion the core requires
    int iteratorDebugLevel;     // _ITERATOR_DEBUG_LEVEL the core was built with

    // Max's main thread. Binds the host services. Returns 0, or writes the
    // reason to `error` and stays unbound.
    int (*initialize)(const McpHostApi* host, McpSink error);
    // Max's main thread, with no request in flight. Removes everything the
    // core registered with Max (watch_scene's notifications and overlay),
    // releases its C++/WinRT factory cache and unbinds the host. Afterwards
    // neither Max nor the shell holds a pointer into the core, and the shell
    // may free it. Returns nonzero with the reason, changing nothing, while a
    // Max callback of the core is running (the shell's step then sits in a
    // message loop nested inside core code, which must not be unloaded).
    int (*shutdown)(McpSink error);
    // Any thread (pipe workers). Writes exactly one JSON response to `out`.
    // Returns 0, or nonzero when an exception reached the boundary; the
    // response then has the pipe's "Internal bridge error" shape.
    int (*dispatch)(const char* requestJson, size_t requestLen, const char* clientSessionId, McpSink out);
    // Pipe worker, after its client disconnected: drops per-client scene_delta state.
    void (*releaseSession)(const char* clientSessionId);
};

// The core's single export.
extern "C" typedef const McpCoreApi* (*McpCoreApiFn)();
#define MCP_CORE_API_EXPORT "mcp_core_api"

#if defined(MCP_CORE_DLL)
#define MCP_CORE_EXPORT __declspec(dllexport)
#else
#define MCP_CORE_EXPORT
#endif

// Defined by core_api.cpp. The static build (MCP_HOT_CORE off) links the core
// into the shell and resolves this directly.
extern "C" MCP_CORE_EXPORT const McpCoreApi* mcp_core_api();

// ── Helpers compiled into each side ──────────────────────────────
// A sink appending to a std::string of the module that creates it, so each
// side allocates and frees only its own strings.
inline void McpAppendUtf8(void* ctx, const char* utf8, size_t n) noexcept {
    try { static_cast<std::string*>(ctx)->append(utf8, n); } catch (...) {}
}
inline McpSink McpStringSink(std::string& target) noexcept { return {&McpAppendUtf8, &target}; }
inline void McpWrite(McpSink sink, const std::string& text) noexcept {
    if (sink.write) sink.write(sink.ctx, text.data(), text.size());
}
// For exception messages: never allocates.
inline void McpWriteText(McpSink sink, const char* text) noexcept {
    if (sink.write && text) sink.write(sink.ctx, text, std::strlen(text));
}

// The response the pipe has always returned for an exception that escaped dispatch.
inline std::string McpInternalErrorResponse(const std::string& message) {
    std::string response = "{\"success\":false,\"error\":\"Internal bridge error: ";
    for (char c : message) {
        if (c == '"') response += "\\\"";
        else if (c == '\\') response += "\\\\";
        else if (c == '\n') response += "\\n";
        else response += c;
    }
    response += "\",\"meta\":{\"transport\":\"namedpipe\"}}";
    return response;
}
