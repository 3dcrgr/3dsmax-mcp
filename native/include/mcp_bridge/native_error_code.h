#pragma once
#include <algorithm>
#include <cctype>
#include <string>

// Error code for an unstructured native error message (structured payloads
// keep their own code). SDK-free so native/tests can check it.
namespace NativeErrors {

inline std::string CodeForMessage(const std::string& message) {
    std::string lower = message;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    if (lower.find("user_busy") != std::string::npos) return "USER_BUSY";
    if (lower.find("stale_view") != std::string::npos) return "STALE_VIEW";
    if (lower.find("safe mode") != std::string::npos) return "SAFE_MODE";
    // The executor refused or drained the request before it started (Max is
    // exiting, the bridge never came up, or the post failed): bridge down,
    // and the work provably never ran.
    if (lower.find("main thread execution timed out") != std::string::npos ||
        lower.find("mainthreadexecutor is shutting down") != std::string::npos ||
        lower.find("mainthreadexecutor not initialized") != std::string::npos ||
        lower.find("failed to post work to main thread") != std::string::npos ||
        lower.find("named pipe") != std::string::npos ||
        (lower.find("bridge") != std::string::npos && lower.find("not found") != std::string::npos))
        return "BRIDGE_DOWN";
    if (lower.find("render busy") != std::string::npos ||
        lower.find("already rendering") != std::string::npos)
        return "RENDER_BUSY";
    if (lower.find("ambiguous") != std::string::npos) return "AMBIGUOUS";
    if (lower.find("unknown object class") != std::string::npos ||
        lower.find("unknown modifier class") != std::string::npos ||
        lower.find("unknown material class") != std::string::npos ||
        lower.find("unknown class") != std::string::npos ||
        (lower.find("plugin") != std::string::npos && lower.find("missing") != std::string::npos))
        return "PLUGIN_MISSING";
    if (lower.find("not found") != std::string::npos ||
        lower.find("no material assigned") != std::string::npos ||
        lower.find("no modifiers on") != std::string::npos)
        return "NOT_FOUND";
    return "BAD_PARAM";
}

// Codes a client may retry: the request did not run, or will run when Max is free.
inline bool IsRetryable(const std::string& code) {
    return code == "BRIDGE_DOWN" || code == "RENDER_BUSY" || code == "USER_BUSY";
}

}  // namespace NativeErrors
