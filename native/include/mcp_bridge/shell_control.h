#pragma once
// The pipe requests the shell answers without entering the core (ping,
// health, native:max_dialogs, native:core) and the native:core protocol. Pure and
// SDK-independent; native/tests/core_reload_tests.cpp covers it.
#include <string>
#include <string_view>
#include <nlohmann/json.hpp>

inline bool McpIsShellType(const std::string& type) {
    return type == "ping" || type == "health" || type == "native:max_dialogs" || type == "native:core";
}

// False when `request` is not a shell request, so ordinary requests skip a
// second parse. Matches the type spelled literally, quotes included, as this
// repo's pipe clients (the Python client, the MAXScript listener) send it. A
// type spelled with \u escapes is not recognized; it goes to the core, which
// answers that it does not know the type.
inline bool McpMayBeShellRequest(const std::string& request) {
    return request.find("\"ping\"") != std::string::npos ||
           request.find("\"health\"") != std::string::npos ||
           request.find("\"native:max_dialogs\"") != std::string::npos ||
           request.find("\"native:core\"") != std::string::npos;
}

// The shell type of a parsed request, or empty when the core handles it.
inline std::string McpShellType(const nlohmann::json& request) {
    if (!request.is_object()) return {};
    const auto type = request.find("type");
    if (type == request.end() || !type->is_string()) return {};
    const std::string& name = type->get_ref<const std::string&>();
    return McpIsShellType(name) ? name : std::string();
}

// requestId and cmdType for a response the shell writes about a core request
// it did not admit, named as the core's dispatcher would name them.
struct McpRequestLabel {
    std::string requestId;
    std::string type;
};

// A scan of the top level, not a parse: values (the command string can be
// megabytes) are skipped without being decoded or copied, and only requestId
// and type are read, the last occurrence winning as in the core's parse.
// Values are not validated and keys spelled with escapes are not matched.
// What is not an object reads as {"", "unknown"}, as the core labels a
// request it cannot parse.
namespace McpScan {

inline size_t SkipSpace(const std::string& text, size_t i) {
    while (i < text.size() && (text[i] == ' ' || text[i] == '\t' || text[i] == '\r' || text[i] == '\n')) ++i;
    return i;
}

// From an opening quote to just past its closing quote; npos if unterminated.
inline size_t SkipString(const std::string& text, size_t i) {
    for (++i;;) {
        i = text.find_first_of("\"\\", i);
        if (i == std::string::npos) return i;
        if (text[i] == '"') return i + 1;
        i += 2;  // a backslash and the character it escapes
        if (i > text.size()) return std::string::npos;
    }
}

// Just past one value; npos if it does not end.
inline size_t SkipValue(const std::string& text, size_t i) {
    if (i >= text.size()) return std::string::npos;
    if (text[i] == '"') return SkipString(text, i);
    if (text[i] == '{' || text[i] == '[') {
        int depth = 0;
        for (i = text.find_first_of("\"{}[]", i); i != std::string::npos; i = text.find_first_of("\"{}[]", i)) {
            if (text[i] == '"') {
                i = SkipString(text, i);
                if (i == std::string::npos) return i;
                continue;
            }
            if (text[i] == '{' || text[i] == '[') ++depth;
            else if (--depth == 0) return i + 1;
            ++i;
        }
        return std::string::npos;
    }
    const size_t end = text.find_first_of(",}] \t\r\n", i);
    return end == std::string::npos ? text.size() : end;
}

// The text of the string token [begin, end); false when it is no JSON string.
inline bool StringToken(const std::string& text, size_t begin, size_t end, std::string& value) {
    if (end - begin < 2 || text[begin] != '"') return false;
    const std::string_view token(text.data() + begin, end - begin);
    if (token.find('\\') == std::string_view::npos) {
        value.assign(token.data() + 1, token.size() - 2);
        return true;
    }
    const nlohmann::json decoded = nlohmann::json::parse(token, nullptr, false);  // one short token
    if (!decoded.is_string()) return false;
    value = decoded.get<std::string>();
    return true;
}

}  // namespace McpScan

inline McpRequestLabel McpLabelRequest(const std::string& request) {
    using namespace McpScan;
    const McpRequestLabel unknown{"", "unknown"};
    McpRequestLabel label{"", "maxscript"};
    size_t i = SkipSpace(request, 0);
    if (i >= request.size() || request[i] != '{') return unknown;
    i = SkipSpace(request, i + 1);
    if (i < request.size() && request[i] == '}') return SkipSpace(request, i + 1) == request.size() ? label : unknown;
    for (;;) {
        if (i >= request.size() || request[i] != '"') return unknown;
        const size_t keyEnd = SkipString(request, i);
        if (keyEnd == std::string::npos) return unknown;
        const std::string_view key(request.data() + i + 1, keyEnd - i - 2);
        i = SkipSpace(request, keyEnd);
        if (i >= request.size() || request[i] != ':') return unknown;
        i = SkipSpace(request, i + 1);
        const size_t valueEnd = SkipValue(request, i);
        if (valueEnd == std::string::npos || valueEnd == i) return unknown;
        if (key == "requestId" || key == "type") {
            std::string value;
            const bool isString = StringToken(request, i, valueEnd, value);
            if (key == "type") label.type = isString ? value : "unknown";
            else label.requestId = isString ? value : "";
        }
        i = SkipSpace(request, valueEnd);
        if (i >= request.size()) return unknown;
        if (request[i] == '}') return SkipSpace(request, i + 1) == request.size() ? label : unknown;
        if (request[i] != ',') return unknown;
        i = SkipSpace(request, i + 1);
    }
}

// ── native:core ──────────────────────────────────────────────────
// command: {"action": "status" | "reload", "wait_ms"?: int, "path"?: string}.
// An empty command or a missing action means status.
constexpr long long kMcpCoreDefaultWaitMs = 5000;
constexpr long long kMcpCoreMaxWaitMs = 600000;

struct McpCoreCommand {
    std::string action = "status";
    long long waitMs = kMcpCoreDefaultWaitMs;
    std::string path;  // UTF-8; empty when not given
};

// Empty, or the BAD_PARAM message.
inline std::string McpParseCoreCommand(const std::string& command, McpCoreCommand& out) {
    out = McpCoreCommand{};
    if (command.find_first_not_of(" \t\r\n") == std::string::npos) return {};
    const nlohmann::json args = nlohmann::json::parse(command, nullptr, false);
    if (args.is_discarded() || !args.is_object()) return "native:core command must be a JSON object";
    if (const auto action = args.find("action"); action != args.end() && !action->is_null()) {
        if (!action->is_string() || (*action != "status" && *action != "reload"))
            return "native:core action must be status or reload";
        out.action = action->get<std::string>();
    }
    if (const auto wait = args.find("wait_ms"); wait != args.end() && !wait->is_null()) {
        // nlohmann parses non-negative integers as unsigned, negative ones as signed.
        const bool valid = wait->is_number_unsigned() &&
                           wait->get<unsigned long long>() <= static_cast<unsigned long long>(kMcpCoreMaxWaitMs);
        if (!valid) return "native:core wait_ms must be an integer from 0 to " + std::to_string(kMcpCoreMaxWaitMs);
        out.waitMs = wait->get<long long>();
    }
    if (const auto path = args.find("path"); path != args.end() && !path->is_null()) {
        if (!path->is_string()) return "native:core path must be a string";
        out.path = path->get<std::string>();
    }
    return {};
}

// A native:core failure: NativeError with an explicit retryable flag and the
// calls in flight; CORE_BUSY also reports the wait it used (waitMs >= 0).
// `extra` adds members to details.
inline std::string McpCoreError(const std::string& code, const std::string& message, bool retryable, int inFlight,
                                long long waitMs = -1, const nlohmann::json& extra = nlohmann::json::object()) {
    nlohmann::json details = {{"in_flight", inFlight}};
    if (waitMs >= 0) details["wait_ms"] = waitMs;
    if (extra.is_object()) details.update(extra);
    return nlohmann::json{{"type", "NativeError"},
                          {"code", code},
                          {"message", message},
                          {"retryable", retryable},
                          {"details", std::move(details)}}
        .dump();
}

struct McpCoreStatus {
    bool hot = false;           // MCP_HOT_CORE layout
    bool loaded = false;
    std::string build;          // the bound core's stamps; empty when none is bound
    std::string bridgeVersion;
    std::string source;         // the file a hot core was copied from
    std::string loadedFrom;     // the copy that is mapped
    long long loadedAtMs = -1;  // Unix time in ms; negative when none is bound
    int reloads = 0;            // successful reloads since 3ds Max started
    int inFlight = 0;
    std::string lastError;      // the last failed load or reload; cleared by a success
    int coreApiVersion = 0;     // the versions this shell speaks
    int hostApiVersion = 0;
};

inline nlohmann::json McpCoreStatusJson(const McpCoreStatus& status) {
    const auto text = [](const std::string& value) { return value.empty() ? nlohmann::json() : nlohmann::json(value); };
    return {
        {"layout", status.hot ? "hot" : "static"},
        {"loaded", status.loaded},
        {"build", status.loaded ? text(status.build) : nlohmann::json()},
        {"bridge_version", status.loaded ? text(status.bridgeVersion) : nlohmann::json()},
        {"core_api_version", status.coreApiVersion},
        {"host_api_version", status.hostApiVersion},
        {"source", status.loaded ? text(status.source) : nlohmann::json()},
        {"loaded_from", status.loaded ? text(status.loadedFrom) : nlohmann::json()},
        {"loaded_at_ms", status.loaded && status.loadedAtMs >= 0 ? nlohmann::json(status.loadedAtMs) : nlohmann::json()},
        {"reloads", status.reloads},
        {"in_flight", status.inFlight},
        {"last_error", text(status.lastError)},
    };
}

// The reload answer: the status after the swap plus what it replaced.
inline nlohmann::json McpCoreReloadJson(const McpCoreStatus& status, const std::string& previousBuild,
                                        long long drainMs) {
    nlohmann::json result = McpCoreStatusJson(status);
    result["reloaded"] = true;
    result["previous_build"] = previousBuild.empty() ? nlohmann::json() : nlohmann::json(previousBuild);
    result["drain_ms"] = drainMs;
    return result;
}
