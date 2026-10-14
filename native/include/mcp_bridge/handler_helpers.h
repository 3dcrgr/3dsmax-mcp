#pragma once
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include <algorithm>
#include <cmath>
#include <functional>
#include <stdexcept>
#include <nlohmann/json.hpp>
#include <max.h>
#include <maxapi.h>
#include <inode.h>
#include <object.h>
#include <modstack.h>
#include <ilayer.h>
#include <ILayerProperties.h>
#include <INodeLayerProperties.h>
#include <iparamb2.h>
#include <plugapi.h>
#include <ISceneEventManager.h>

#include <maxscript/maxscript.h>
#include <maxscript/foundation/strings.h>
#include <maxscript/maxwrapper/mxsobjects.h>
#include <CoreFunctions.h>
#include "mcp_bridge/color_value.h"

class MCPBridgeGUP;

// Fails the current bridge operation when Max showed an error dialog during
// it. Core code reaches the shell's DialogWatch through the host API
// (core_bridge.cpp); shell code passes DialogWatch::ThrowIfDismissed itself.
namespace McpHost::DialogWatch { void ThrowIfDismissed(); }
// Runs work on Max's main thread, also from a direct handler (core_bridge.cpp).
namespace McpHost { std::string RunOnMainThread(const std::function<std::string()>& work); }

namespace HandlerHelpers {

using json = nlohmann::json;

// MAXScript's evaluator, value heap and class registry belong to Max's main
// thread, and TempQuietMode switches a process-wide mode. Script work on any
// other thread is a bridge defect: fail it before it touches MAXScript.
inline void RequireMaxScriptThread() {
    if (GetCurrentThreadId() == GetCOREInterface15()->GetMainThreadID()) return;
    throw std::runtime_error(json{{"type", "NativeError"}, {"code", "INTERNAL_ERROR"}, {"retryable", false},
        {"message", "Internal bridge error: MAXScript was called off 3ds Max's main thread; the script did not run"}}.dump());
}

// ── UTF-8 <-> Wide conversion ───────────────────────────────────
inline std::string WideToUtf8(const wchar_t* w) {
    if (!w || !*w) return {};
    int len = WideCharToMultiByte(CP_UTF8, 0, w, -1, nullptr, 0, nullptr, nullptr);
    std::string s(len - 1, 0);
    WideCharToMultiByte(CP_UTF8, 0, w, -1, &s[0], len, nullptr, nullptr);
    return s;
}

inline std::wstring Utf8ToWide(const std::string& s) {
    if (s.empty()) return {};
    int len = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    std::wstring w(len, 0);
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), &w[0], len);
    return w;
}

// Some plugins prefix a control character to force their class to the top of
// 3ds Max's alphabetical browsers (Corona registers "\rCoronaPhysicalMtl").
// MAXScript never exposes those raw bytes: `(classOf m) as string` reports
// _CoronaPhysicalMtl. Mirror that substitution in class-name outputs.
// JSON escaping itself is handled by the serializer.
inline std::string SanitizeScriptName(std::string value) {
    for (char& ch : value) {
        const unsigned char byte = static_cast<unsigned char>(ch);
        if (byte < 0x20 || byte == 0x7F) ch = '_';
    }
    return value;
}

// Accept both SDK labels and the control-character aliases returned to clients.
inline bool MatchesClassName(const std::wstring& requested, const MCHAR* candidate) {
    if (!candidate || !*candidate) return false;
    if (_wcsicmp(requested.c_str(), candidate) == 0) return true;
    const auto sanitized = Utf8ToWide(SanitizeScriptName(WideToUtf8(candidate)));
    return _wcsicmp(requested.c_str(), sanitized.c_str()) == 0;
}

// MAXScript's `(classOf value) as string` exposes the script-facing class
// token, not the localized UI label returned by Animatable::ClassName().
inline std::string ScriptClassName(ClassDesc* descriptor) {
    if (!descriptor) return {};

    const MCHAR* internalName = descriptor->InternalName();
    if (internalName && *internalName) {
        return SanitizeScriptName(WideToUtf8(internalName));
    }

    const MCHAR* nonLocalizedName = descriptor->NonLocalizedClassName();
    if (nonLocalizedName && *nonLocalizedName) {
        return SanitizeScriptName(WideToUtf8(nonLocalizedName));
    }

    const MCHAR* className = descriptor->ClassName();
    return className ? SanitizeScriptName(WideToUtf8(className)) : std::string();
}

inline std::string ScriptClassName(Animatable* value) {
    if (!value) return {};

    ClassDesc* descriptor = DllDir::GetInstance().ClassDir().FindClass(
        value->SuperClassID(), value->ClassID());
    const std::string scriptName = ScriptClassName(descriptor);
    if (!scriptName.empty()) {
        return scriptName;
    }
    return SanitizeScriptName(WideToUtf8(value->ClassName().data()));
}

// The script-visible MAXClass name can differ from ClassDesc::InternalName()
// (OpenPBR_Material vs OpenPBR, for example). This is still native C++ metadata:
// it does not evaluate MAXScript. Call only from handlers marshalled to Max's
// main thread because the MAXScript class registry is not thread-safe.
inline std::string MaxScriptVisibleClassName(
    SClass_ID superClassId,
    const Class_ID& classId) {
    RequireMaxScriptThread();
    ScopedMaxScriptEvaluationContext evaluationContext;
    Class_ID lookupId = classId;
    MAXClass* maxClass = MAXClass::lookup_class(
        &lookupId,
        superClassId,
        true);
    if (maxClass && maxClass->name) {
        const MCHAR* value = maxClass->name->to_string();
        if (value && *value) {
            return SanitizeScriptName(WideToUtf8(value));
        }
    }
    ClassDesc* descriptor = DllDir::GetInstance().ClassDir().FindClass(
        superClassId,
        classId);
    return ScriptClassName(descriptor);
}

inline std::string MaxScriptVisibleClassName(Animatable* value) {
    if (!value) return {};
    return MaxScriptVisibleClassName(
        value->SuperClassID(),
        value->ClassID());
}

// ── Node property helpers ───────────────────────────────────────
inline std::string NodeClassName(INode* node) {
    ObjectState os = node->EvalWorldState(GetCOREInterface()->GetTime());
    if (os.obj) {
        return SanitizeScriptName(WideToUtf8(os.obj->ClassName().data()));
    }
    return "Unknown";
}

inline std::string NodeLayerName(INode* node) {
    INodeLayerProperties* nlp = static_cast<INodeLayerProperties*>(
        node->GetInterface(NODELAYERPROPERTIES_INTERFACE));
    if (nlp) {
        ILayerProperties* lp = nlp->getLayer();
        if (lp) {
            const MCHAR* name = lp->getName();
            if (name) return WideToUtf8(name);
        }
    }
    return "0";
}

inline unsigned long long NodeHandle(INode* node) {
    if (!node) return 0ULL;
    return static_cast<unsigned long long>(Animatable::GetHandleByAnim(node));
}

inline json NodeIdentityJson(INode* node) {
    json out;
    if (!node) return out;
    out["name"] = WideToUtf8(node->GetName());
    out["handle"] = NodeHandle(node);
    out["class"] = NodeClassName(node);
    out["layer"] = NodeLayerName(node);
    return out;
}

inline json NodePosition(INode* node, TimeValue t) {
    Matrix3 tm = node->GetNodeTM(t);
    Point3 pos = tm.GetTrans();
    return json::array({pos.x, pos.y, pos.z});
}

inline json NodeWireColor(INode* node) {
    DWORD c = node->GetWireColor();
    return json::array({GetRValue(c), GetGValue(c), GetBValue(c)});
}

// ── Scene traversal ─────────────────────────────────────────────
inline void CollectNodes(INode* node, std::vector<INode*>& out) {
    for (int i = 0; i < node->NumberOfChildren(); i++) {
        INode* child = node->GetChildNode(i);
        out.push_back(child);
        CollectNodes(child, out);
    }
}

// Nodes named `name`. Exact matches win; when there is none, names that differ
// only in letter case match, as MAXScript name lookups do.
inline std::vector<INode*> CollectNodesByName(const std::string& name) {
    std::vector<INode*> all, exact, caseless;
    CollectNodes(GetCOREInterface()->GetRootNode(), all);
    const std::wstring wide = Utf8ToWide(name);
    for (INode* n : all) {
        const MCHAR* nodeName = n->GetName();
        if (wide == nodeName) exact.push_back(n);
        else if (exact.empty() && _wcsicmp(nodeName, wide.c_str()) == 0) caseless.push_back(n);
    }
    return exact.empty() ? caseless : exact;
}

inline unsigned long long PayloadHandleValue(const json& payload, const std::string& key = "handle") {
    auto it = payload.find(key);
    if (it == payload.end() || it->is_null()) return 0ULL;
    try {
        if (it->is_number_unsigned()) return it->get<unsigned long long>();
        if (it->is_number_integer()) {
            long long value = it->get<long long>();
            return value > 0 ? static_cast<unsigned long long>(value) : 0ULL;
        }
        // type() check instead of is_string(): MAXScript's value.h defines a
        // function-like is_string macro that breaks the method call when this
        // header lands after maxscript includes.
        if (it->type() == json::value_t::string) {
            std::string value = it->get<std::string>();
            if (!value.empty()) return std::stoull(value);
        }
    } catch (...) {
        return 0ULL;
    }
    return 0ULL;
}

inline INode* FindNodeByHandle(unsigned long long handle) {
    if (handle == 0ULL) return nullptr;
    return NodeEventNamespace::GetNodeByKey(static_cast<NodeEventNamespace::NodeKey>(handle));
}

inline std::string StructuredErrorPayload(
    const std::string& code,
    const std::string& message,
    const json& hint = json()) {
    json payload;
    payload["type"] = "NativeError";
    payload["message"] = message;
    payload["code"] = code;
    payload["retryable"] = (
        code == "BRIDGE_DOWN" || code == "RENDER_BUSY" || code == "USER_BUSY" ||
        code == "SCENE_CONFLICT" || code == "TRANSACTION_BUSY");
    if (!hint.is_null() && !hint.empty()) {
        payload["hint"] = hint;
    }
    return payload.dump();
}

// ── Node lookup by name ─────────────────────────────────────────
// Max lets several objects share a name (imported "Object001", repeated
// furniture), and Interface::GetINodeByName then returns either one. A name
// therefore resolves only when exactly one object carries it; a shared name
// fails with every candidate's identity instead of acting on a guess.
inline constexpr const char* kPassHandleAdvice = "Pass handle to disambiguate this object name.";
inline constexpr const char* kUniqueNamesAdvice =
    "This tool takes object names only. Give the objects unique names first "
    "(set_object_property with handle and property \"name\", or scene_patch rename), then retry.";

inline json NameCandidatesJson(const std::vector<INode*>& matches) {
    json candidates = json::array();
    for (INode* node : matches) candidates.push_back(NodeIdentityJson(node));
    return candidates;
}

[[noreturn]] inline void ThrowAmbiguousName(
    const std::string& name, const std::vector<INode*>& matches, const char* advice) {
    throw std::runtime_error(StructuredErrorPayload(
        "AMBIGUOUS",
        "Ambiguous object name: " + name,
        {{"message", advice}, {"candidates", NameCandidatesJson(matches)}}));
}

// The one object with this exact name, or nullptr when none has it; the
// caller reports a missing name in its own words.
inline INode* FindUniqueNodeByName(const std::string& name, const char* advice = kUniqueNamesAdvice) {
    const std::vector<INode*> matches = CollectNodesByName(name);
    if (matches.size() > 1) ThrowAmbiguousName(name, matches, advice);
    return matches.empty() ? nullptr : matches.front();
}

// ── Transform readback ──────────────────────────────────────────
// Constraint, script and expression controllers ignore Move, Rotate, Scale
// and SetNodeTM without an error, so edits compare the node transform before
// and after each requested part.
inline bool SameTransform(const Matrix3& a, const Matrix3& b) {
    for (int row = 0; row < 4; ++row) {
        for (int axis = 0; axis < 3; ++axis) {
            const float x = a.GetRow(row)[axis];
            const float y = b.GetRow(row)[axis];
            if (std::fabs(x - y) > 1e-6f * (std::max)({1.0f, std::fabs(x), std::fabs(y)})) return false;
        }
    }
    return true;
}

inline std::string ControllerClassName(Control* control) {
    if (!control) return "";
    MSTR name;
    control->GetClassName(name, false);
    return WideToUtf8(name.data());
}

// part is "move", "rotate" or "scale". A transform-level controller (Link,
// LookAt) has no separate part controllers, so it is named instead.
inline json IgnoredTransformPart(INode* node, const std::string& part) {
    Control* tm = node->GetTMController();
    Control* sub = nullptr;
    if (tm) {
        if (part == "move") sub = tm->GetPositionController();
        else if (part == "rotate") sub = tm->GetRotationController();
        else sub = tm->GetScaleController();
    }
    return {{"part", part}, {"controller", ControllerClassName(sub ? sub : tm)}};
}

inline constexpr const char* kTransformIgnoredAdvice =
    "Edit the controller's targets or values instead, or use assign_controller to give the track "
    "a keyframable controller.";

// MAXScript expression for a node already resolved here. getAnimByHandle
// reaches it by its animatable handle, so the script never looks the node up
// by name again.
inline std::string NodeScriptRef(INode* node) {
    return "(getAnimByHandle " + std::to_string(NodeHandle(node)) + ")";
}

inline INode* ResolveNodeFromPayload(
    const json& payload,
    const std::string& nameKey = "name",
    const std::string& handleKey = "handle") {

    const unsigned long long handle = PayloadHandleValue(payload, handleKey);
    if (handle != 0ULL) {
        INode* byHandle = FindNodeByHandle(handle);
        if (!byHandle) {
            throw std::runtime_error("Object handle not found: " + std::to_string(handle));
        }
        return byHandle;
    }

    const std::string name = payload.value(nameKey, "");
    if (name.empty()) {
        throw std::runtime_error(nameKey + " or " + handleKey + " is required");
    }

    INode* node = FindUniqueNodeByName(name, kPassHandleAdvice);
    if (!node) {
        throw std::runtime_error("Object not found: " + name);
    }
    return node;
}

// MAXScript-level wrap that captures runtime exceptions (parse errors still
// surface via ExecuteMAXScriptScript returning FALSE). The wrapped script
// returns either the user's value, or a string with this sentinel prefix.
inline const char* MaxScriptErrorSentinel() { return "__MCP_MS_ERR__:"; }

// Joins names for a warning. Callers keep the text free of "not found:",
// which the Python envelope reads as an error even inside a success payload.
inline std::string JoinNames(const json& names, size_t limit = 10) {
    std::string out;
    size_t shown = 0;
    for (const auto& entry : names) {
        if (shown == limit) { out += ", ..."; break; }
        if (shown++) out += ", ";
        if (entry.type() == json::value_t::string) out += entry.get<std::string>();
        else if (entry.is_object() && entry.contains("name") &&
                 entry["name"].type() == json::value_t::string) out += entry["name"].get<std::string>();
        else if (entry.is_object() && entry.contains("handle")) out += "handle " + entry["handle"].dump();
        else out += entry.dump();
    }
    return out;
}

// ── Batch name resolution ───────────────────────────────────────
// A batch edit applies each requested name to the one object carrying it.
// A name several objects share is applied to none of them: like a missing
// name, it is skipped and reported, with its candidates.
struct NamedNodes {
    std::vector<INode*> nodes;        // request order, each node once
    json notFound = json::array();    // names no object carries
    json ambiguous = json::array();   // {name, candidates} per shared name
    bool Unresolved() const { return !notFound.empty() || !ambiguous.empty(); }
};

inline json AmbiguousNameJson(const std::string& name, const std::vector<INode*>& matches) {
    return {{"name", name}, {"candidates", NameCandidatesJson(matches)}};
}

inline NamedNodes ResolveNodesByNames(const std::vector<std::string>& names) {
    // One scene walk serves the whole list.
    std::unordered_map<std::string, std::vector<INode*>> byName;
    for (const auto& name : names) byName.emplace(name, std::vector<INode*>{});
    std::vector<INode*> all;
    CollectNodes(GetCOREInterface()->GetRootNode(), all);
    for (INode* node : all) {
        auto it = byName.find(WideToUtf8(node->GetName()));
        if (it != byName.end()) it->second.push_back(node);
    }
    // As in CollectNodesByName: a name with no exact match may match by case alone.
    for (auto& entry : byName) {
        if (!entry.second.empty()) continue;
        const std::wstring wide = Utf8ToWide(entry.first);
        for (INode* node : all)
            if (_wcsicmp(node->GetName(), wide.c_str()) == 0) entry.second.push_back(node);
    }

    NamedNodes out;
    std::unordered_set<INode*> seen;
    std::unordered_set<std::string> reported;
    for (const auto& name : names) {
        const std::vector<INode*>& matches = byName[name];
        if (matches.empty()) {
            out.notFound.push_back(name);
        } else if (matches.size() > 1) {
            if (reported.insert(name).second) out.ambiguous.push_back(AmbiguousNameJson(name, matches));
        } else if (seen.insert(matches.front()).second) {
            out.nodes.push_back(matches.front());
        }
    }
    return out;
}

// Error code for a batch that applied nothing because of its names.
inline const char* UnresolvedNamesCode(const NamedNodes& resolved) {
    return resolved.notFound.empty() && !resolved.ambiguous.empty() ? "AMBIGUOUS" : "NOT_FOUND";
}

inline json UnresolvedNamesHint(const NamedNodes& resolved, json hint = json::object()) {
    hint["notFound"] = resolved.notFound;
    if (!resolved.ambiguous.empty()) {
        hint["ambiguous"] = resolved.ambiguous;
        hint["message"] = kUniqueNamesAdvice;
    }
    return hint;
}

// Fails a batch in which no requested name matched exactly one object.
// consequence says what was left unchanged.
[[noreturn]] inline void ThrowUnresolvedNames(
    const NamedNodes& resolved, const std::string& consequence, const json& hint = json::object()) {
    const char* reason = resolved.ambiguous.empty()
        ? "No object matched the requested names; "
        : resolved.notFound.empty()
            ? "Each requested name is shared by several objects; "
            : "No requested name matched exactly one object; ";
    throw std::runtime_error(StructuredErrorPayload(
        UnresolvedNamesCode(resolved), reason + consequence, UnresolvedNamesHint(resolved, hint)));
}

// Marks a batch result partial for the shared names it skipped.
inline void AddAmbiguousNamesWarning(json& result, const json& ambiguous) {
    if (ambiguous.empty()) return;
    result["ambiguous"] = ambiguous;
    result["status"] = "partial";
    result["warnings"].push_back(std::to_string(ambiguous.size()) +
        " requested name(s) are shared by several objects and were skipped: " + JoinNames(ambiguous) +
        ". See ambiguous for each object's handle.");
}

inline std::wstring WrapForErrorCapture(const std::wstring& wcmd) {
    return std::wstring(
        L"(\n"
        L"  local __mcp_err = undefined\n"
        L"  local __mcp_res = try (\n"
    ) + wcmd + std::wstring(
        L"\n  ) catch (__mcp_err = getCurrentException(); undefined)\n"
        L"  if __mcp_err != undefined then (\"__MCP_MS_ERR__:\" + __mcp_err) else __mcp_res\n"
        L")\n"
    );
}

// ── MAXScript execution (for hybrid handlers) ───────────────────
// Evaluates on the calling thread, which must be Max's main thread. Shell code
// runs there already and passes its own dialog check.
inline std::string RunMAXScript(
    const std::string& script,
    void (*throwIfDialogDismissed)()) {
    RequireMaxScriptThread();
    // Tool scripts are batch work: prompts take their defaults.
    TempQuietMode quiet;
    std::wstring wcmd = WrapForErrorCapture(Utf8ToWide(script));
    FPValue fpv;
    BOOL ok = FALSE;

    try {
        ok = ExecuteMAXScriptScript(
            wcmd.c_str(),
            MAXScript::ScriptSource::NonEmbedded,
            TRUE,    // quietErrors: never route agent diagnostics to modal UI
            &fpv,    // result
            TRUE     // logQuietErrors
        );
    } catch (...) {
        throw std::runtime_error("MAXScript execution exception");
    }

    if (!ok) {
        const std::string detail = fpv.type == TYPE_TSTR && fpv.tstr ? WideToUtf8(fpv.tstr->data()) : "";
        throw std::runtime_error("MAXScript execution failed: " + detail);
    }

    throwIfDialogDismissed();
    // Convert FPValue to string
    if (fpv.type == TYPE_STRING || fpv.type == TYPE_FILENAME) {
        return WideToUtf8(fpv.s);
    }
    if (fpv.type == TYPE_TSTR) {
        return WideToUtf8(fpv.tstr->data());
    }
    if (fpv.type == TYPE_VALUE && fpv.v != nullptr) {
        try {
            const MCHAR* str = fpv.v->to_string();
            return WideToUtf8(str);
        } catch (...) {}
    }
    if (fpv.type == TYPE_INT) {
        return std::to_string(fpv.i);
    }
    if (fpv.type == TYPE_FLOAT) {
        return std::to_string(fpv.f);
    }
    if (fpv.type == TYPE_BOOL) {
        return fpv.i ? "true" : "false";
    }

    // Never re-evaluate the script to stringify the result: scripts with side
    // effects (e.g. "max undo") would run twice.
    return "OK";
}

// Core code. A direct handler runs on a pipe thread, where ExecuteSync stays
// inline: evaluation and result conversion are sent to Max's main thread.
inline std::string RunMAXScript(const std::string& script) {
    return McpHost::RunOnMainThread([&script] {
        return RunMAXScript(script, &McpHost::DialogWatch::ThrowIfDismissed);
    });
}

// Core code that reads the FPValue itself: no error wrapper, quiet mode or
// dialog check. `read` runs on Max's main thread right after evaluation, while
// the result is valid. False when the script failed.
inline bool EvaluateMAXScript(const std::wstring& script, const std::function<void(FPValue&)>& read) {
    bool ok = false;
    McpHost::RunOnMainThread([&] {
        RequireMaxScriptThread();
        FPValue fpv;
        ok = ExecuteMAXScriptScript(script.c_str(), MAXScript::ScriptSource::NonEmbedded, TRUE, &fpv) != FALSE;
        if (ok) read(fpv);
        return std::string();
    });
    return ok;
}

// RunMAXScript returns a runtime exception as a sentinel string, so a caller
// that ignores the result reports success for an edit Max rejected. Mutations
// use this form: the throw reaches the dispatcher, which cancels its undo hold.
inline std::string RunMAXScriptChecked(const std::string& script) {
    std::string result = RunMAXScript(script);
    const std::string sentinel = MaxScriptErrorSentinel();
    if (result.compare(0, sentinel.size(), sentinel) == 0)
        throw std::runtime_error(result.substr(sentinel.size()));
    return result;
}

// Parses an explicit boolean literal. Anything else is rejected rather than
// read as false, so "True" or "yes" cannot silently clear a flag.
inline bool ParseBoolLiteral(const std::string& value, bool& out) {
    std::string lower = value;
    std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);
    if (lower == "true" || lower == "1" || lower == "on" || lower == "yes") { out = true; return true; }
    if (lower == "false" || lower == "0" || lower == "off" || lower == "no") { out = false; return true; }
    return false;
}

// ── JSON escape utility ─────────────────────────────────────────
inline std::string JsonEscape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:   out += c;      break;
        }
    }
    return out;
}

// ── Wildcard pattern matching (case-insensitive) ────────────────
// Supports: * at start, end, both, or standalone. Exact match otherwise.
inline bool WildcardMatch(const std::string& name, const std::string& pattern) {
    if (pattern == "*") return true;
    // Case-insensitive copies
    std::string lname = name, lpat = pattern;
    std::transform(lname.begin(), lname.end(), lname.begin(), ::tolower);
    std::transform(lpat.begin(), lpat.end(), lpat.begin(), ::tolower);

    bool startsWild = !lpat.empty() && lpat.front() == '*';
    bool endsWild = !lpat.empty() && lpat.back() == '*';

    if (startsWild && endsWild) {
        std::string sub = lpat.substr(1, lpat.size() - 2);
        return lname.find(sub) != std::string::npos;
    }
    if (startsWild) {
        std::string suffix = lpat.substr(1);
        return lname.size() >= suffix.size() &&
               lname.compare(lname.size() - suffix.size(), suffix.size(), suffix) == 0;
    }
    if (endsWild) {
        std::string prefix = lpat.substr(0, lpat.size() - 1);
        return lname.compare(0, prefix.size(), prefix) == 0;
    }
    return lname == lpat;
}

// ── Collect nodes matching a wildcard pattern ───────────────────
inline std::vector<INode*> CollectNodesByPattern(const std::string& pattern) {
    Interface* ip = GetCOREInterface();
    INode* root = ip->GetRootNode();
    std::vector<INode*> all, matched;
    CollectNodes(root, all);
    for (INode* n : all) {
        if (WildcardMatch(WideToUtf8(n->GetName()), pattern))
            matched.push_back(n);
    }
    return matched;
}

// ── Normalize sub-anim path for execute() ───────────────────────
// Replaces [#Object (ClassName)] with .baseObject — parentheses in
// class names break MAXScript's execute() parser.
inline std::string NormalizeSubAnimPath(const std::string& path) {
    std::string result = path;

    // Ensure [name] tokens have # prefix → [#name] (needed by MAXScript execute())
    // Handles paths from get_wired_params which may omit the # prefix
    std::string fixed;
    for (size_t i = 0; i < result.size(); i++) {
        if (result[i] == '[' && (i + 1 < result.size()) && result[i + 1] != '#') {
            fixed += "[#";
        } else {
            fixed += result[i];
        }
    }
    result = fixed;

    // Replace [#Object (anything)] with .baseObject — parentheses break execute()
    auto pos = result.find("[#Object (");
    if (pos != std::string::npos) {
        auto end = result.find(")]", pos);
        if (end != std::string::npos) {
            result = result.substr(0, pos) + ".baseObject" + result.substr(end + 2);
        }
    }
    // Also handle without # (legacy): [Object (anything)]
    pos = result.find("[Object (");
    if (pos != std::string::npos) {
        auto end = result.find(")]", pos);
        if (end != std::string::npos) {
            result = result.substr(0, pos) + ".baseObject" + result.substr(end + 2);
        }
    }

    // Strip [#Parameters] — track view grouping node, not addressable in MAXScript
    std::string paramToken = "[#Parameters]";
    pos = result.find(paramToken);
    while (pos != std::string::npos) {
        result.erase(pos, paramToken.length());
        pos = result.find(paramToken);
    }
    return result;
}

// ── Walk sub-anim path to resolve a track from a node ───────────
// Path format: "[#Transform][#Position][#Z Position]" or "[#transform][#position][#z_position]"
// Returns the Animatable* at that path, or nullptr if not found.
inline Animatable* ResolveSubAnimPath(INode* node, const std::string& path) {
    if (!node) return nullptr;
    Animatable* current = node;

    // Parse [#name] tokens from the path
    std::vector<std::string> tokens;
    size_t i = 0;
    while (i < path.size()) {
        auto open = path.find("[#", i);
        if (open == std::string::npos) {
            // Also try [  without #
            open = path.find('[', i);
            if (open == std::string::npos) break;
            auto close = path.find(']', open);
            if (close == std::string::npos) break;
            std::string tok = path.substr(open + 1, close - open - 1);
            if (!tok.empty() && tok != "Parameters") tokens.push_back(tok);
            i = close + 1;
        } else {
            auto close = path.find(']', open);
            if (close == std::string::npos) break;
            std::string tok = path.substr(open + 2, close - open - 2);
            if (!tok.empty() && tok != "Parameters") tokens.push_back(tok);
            i = close + 1;
        }
    }

    // Walk sub-anims matching by name (case-insensitive)
    for (const auto& tok : tokens) {
        bool found = false;
        int numSubs = current->NumSubs();
        for (int s = 0; s < numSubs; s++) {
            Animatable* sub = current->SubAnim(s);
            if (!sub) continue;
            MSTR subName = current->SubAnimName(s, false);
            std::string sname = WideToUtf8(subName.data());
            // Case-insensitive compare, also handle underscore vs space
            std::string lTok = tok, lName = sname;
            std::transform(lTok.begin(), lTok.end(), lTok.begin(), ::tolower);
            std::transform(lName.begin(), lName.end(), lName.begin(), ::tolower);
            // Replace spaces with underscores for comparison
            std::replace(lTok.begin(), lTok.end(), ' ', '_');
            std::replace(lName.begin(), lName.end(), ' ', '_');
            if (lTok == lName) {
                current = sub;
                found = true;
                break;
            }
        }
        if (!found) return nullptr;
    }
    return current;
}

// ── Find ClassDesc by class name (iterates all loaded plugins) ──
inline ClassDesc* FindClassDescByName(const std::string& className, SClass_ID superID = 0) {
    std::wstring wname = Utf8ToWide(className);
    ClassDesc* found = nullptr;
    auto& dir = DllDir::GetInstance();
    int numDlls = dir.Count();
    for (int d = 0; d < numDlls; d++) {
        const DllDesc& dll = dir[d];
        int numClasses = dll.NumberOfClasses();
        for (int c = 0; c < numClasses; c++) {
            ClassDesc* cd = dll[c];
            if (!cd) continue;
            if (superID != 0 && cd->SuperClassID() != superID) continue;
            if (!MatchesClassName(wname, cd->ClassName()) &&
                !MatchesClassName(wname, cd->InternalName()) &&
                !MatchesClassName(wname, cd->NonLocalizedClassName())) continue;
            if (found && (found->ClassID() != cd->ClassID() ||
                          found->SuperClassID() != cd->SuperClassID()))
                throw std::runtime_error(StructuredErrorPayload(
                    "AMBIGUOUS", "Class name is ambiguous: " + className));
            found = cd;
        }
    }
    return found;
}

// Whole-string numbers only: std::stof and std::stoi read "12abc" as 12 and
// throw on "abc"; a value that is not entirely a number is rejected instead.
inline bool ParseWholeNumber(const std::string& text, float& out) {
    try {
        size_t used = 0;
        out = std::stof(text, &used);
        return text.find_first_not_of(" 	", used) == std::string::npos;
    } catch (const std::exception&) {
        return false;
    }
}

inline bool ParseWholeNumber(const std::string& text, int& out) {
    try {
        size_t used = 0;
        out = std::stoi(text, &used);
        return text.find_first_not_of(" 	", used) == std::string::npos;
    } catch (const std::exception&) {
        return false;
    }
}

// ── Parse a MAXScript-style value string into typed PB2 value ───
// Returns true if the param was set successfully.
inline bool SetPB2ParamFromString(IParamBlock2* pb, ParamID pid, ParamType2 ptype,
                                  const std::string& valStr, TimeValue t) {
    // Strip the TYPE_TAB flag for base type comparison
    int baseType = ptype & ~TYPE_TAB;

    switch (baseType) {
    case TYPE_FLOAT:
    case TYPE_ANGLE:
    case TYPE_PCNT_FRAC:
    case TYPE_WORLD:
    case TYPE_COLOR_CHANNEL: {
        float f = 0.0f;
        if (!ParseWholeNumber(valStr, f)) return false;
        return pb->SetValue(pid, t, f) != 0;
    }
    case TYPE_INT:
    case TYPE_BOOL:
    case TYPE_TIMEVALUE:
    case TYPE_RADIOBTN_INDEX:
    case TYPE_INDEX: {
        // Handle "true"/"false" for bools
        if (valStr == "true" || valStr == "on") return pb->SetValue(pid, t, 1) != 0;
        if (valStr == "false" || valStr == "off") return pb->SetValue(pid, t, 0) != 0;
        int i = 0;
        if (!ParseWholeNumber(valStr, i)) return false;
        return pb->SetValue(pid, t, i) != 0;
    }
    case TYPE_RGBA:
    case TYPE_FRGBA: {
        std::array<float, 4> rgba;
        if (!ParseColorValue(valStr, rgba)) return false;
        if (baseType == TYPE_FRGBA)
            return pb->SetValue(pid, t, AColor(rgba[0], rgba[1], rgba[2], rgba[3])) != 0;
        return pb->SetValue(pid, t, Color(rgba[0], rgba[1], rgba[2])) != 0;
    }
    case TYPE_POINT3: {
        // Parse multiple formats:
        // "[x,y,z]" or "x,y,z" — direct Point3
        // "(color r g b)" or "color r g b" — MAXScript color (0-255 range for RGBA)
        std::string s = valStr;
        float x = 0, y = 0, z = 0;

        // Try "(color r g b)" format
        if (sscanf(s.c_str(), "(color %f %f %f)", &x, &y, &z) == 3 ||
            sscanf(s.c_str(), "color %f %f %f", &x, &y, &z) == 3) {
            Point3 pt(x, y, z);
            return pb->SetValue(pid, t, pt) != 0;
        }

        // Try "[x,y,z]" or "x,y,z"
        s.erase(std::remove(s.begin(), s.end(), '['), s.end());
        s.erase(std::remove(s.begin(), s.end(), ']'), s.end());
        if (sscanf(s.c_str(), "%f,%f,%f", &x, &y, &z) == 3) {
            Point3 pt(x, y, z);
            return pb->SetValue(pid, t, pt) != 0;
        }
        return false;
    }
    case TYPE_TEXMAP: {
        // "undefined" or "null" → clear the texture map slot
        std::string lower = valStr;
        std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);
        if (lower == "undefined" || lower == "null" || lower == "none") {
            return pb->SetValue(pid, t, (Texmap*)nullptr) != 0;
        }
        // For actual texmap assignment by reference, caller must use a different mechanism
        return false;
    }
    case TYPE_MTL: {
        std::string lower = valStr;
        std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);
        if (lower == "undefined" || lower == "null" || lower == "none") {
            return pb->SetValue(pid, t, (Mtl*)nullptr) != 0;
        }
        return false;
    }
    case TYPE_STRING:
    case TYPE_FILENAME: {
        // Remove surrounding quotes if present
        std::string s = valStr;
        if (s.size() >= 2 && s.front() == '"' && s.back() == '"')
            s = s.substr(1, s.size() - 2);
        std::wstring ws = Utf8ToWide(s);
        return pb->SetValue(pid, t, ws.c_str()) != 0;
    }
    default:
        return false;
    }
}

// ── Find and set a parameter by name on an object's IParamBlock2s ──
inline bool SetParamByName(Animatable* anim, const std::string& paramName,
                           const std::string& value, TimeValue t) {
    if (!anim) return false;
    std::wstring wparam = Utf8ToWide(paramName);

    // Iterate all param blocks on the object
    int numPB = anim->NumParamBlocks();
    for (int pb_idx = 0; pb_idx < numPB; pb_idx++) {
        IParamBlock2* pb = anim->GetParamBlock(pb_idx);
        if (!pb) continue;

        ParamBlockDesc2* desc = pb->GetDesc();
        if (!desc) continue;

        for (int p = 0; p < desc->count; p++) {
            const ParamDef& pd = desc->GetParamDef(desc->IndextoID(p));
            if (pd.int_name && _wcsicmp(pd.int_name, wparam.c_str()) == 0) {
                return SetPB2ParamFromString(pb, pd.ID, pd.type, value, t);
            }
        }
    }
    return false;
}

} // namespace HandlerHelpers
