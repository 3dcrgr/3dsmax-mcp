#pragma once
// The MCP_HOT_CORE loader: finds core/mcp_core.dll, maps a private copy of it
// so a rebuild can replace the original while Max runs, and checks its stamps.
// Windows only and SDK-independent, so native/tests drive it with fake cores.
// Paths are wide; messages are UTF-8.
#include <windows.h>
#include <string>
#include "mcp_bridge/core_reload.h"

namespace McpCoreLoader {

// ── Source resolution (pure) ─────────────────────────────────────
enum class Origin { Request, Environment, PathFile, Default };

struct Source {
    std::wstring path;
    Origin origin = Origin::Default;
};

inline bool IsSpace(wchar_t c) { return c == L' ' || c == L'\t' || c == L'\r' || c == L'\n' || c == 0xFEFF; }

inline std::wstring Trim(const std::wstring& text) {
    size_t begin = 0, end = text.size();
    while (begin < end && IsSpace(text[begin])) ++begin;
    while (end > begin && IsSpace(text[end - 1])) --end;
    std::wstring value = text.substr(begin, end - begin);
    if (value.size() >= 2 && value.front() == L'"' && value.back() == L'"') value = value.substr(1, value.size() - 2);
    return value;
}

// mcp_core.path's first non-blank line, without whitespace, BOM or quotes.
inline std::wstring PathFileLine(const std::wstring& text) {
    size_t start = 0;
    while (start <= text.size()) {
        const size_t end = text.find(L'\n', start);
        const std::wstring line = Trim(text.substr(start, end == std::wstring::npos ? std::wstring::npos : end - start));
        if (!line.empty()) return line;
        if (end == std::wstring::npos) break;
        start = end + 1;
    }
    return {};
}

// Drive-qualified ("C:..."), rooted ("\...") or UNC ("\\server\...").
inline bool IsAbsolute(const std::wstring& path) {
    return (path.size() >= 2 && path[1] == L':') || (!path.empty() && (path[0] == L'\\' || path[0] == L'/'));
}

inline std::wstring Join(const std::wstring& directory, const std::wstring& path) {
    if (IsAbsolute(path) || directory.empty()) return path;
    const wchar_t last = directory.back();
    return directory + (last == L'\\' || last == L'/' ? L"" : L"\\") + path;
}

// The first source that is set wins, whether or not its file exists, so a
// typo fails loudly instead of loading another core: the request's `path`,
// MCP_CORE_DLL, mcp_core.path next to the .gup, then <gup dir>\core\mcp_core.dll.
// Relative paths are relative to the .gup's folder.
inline Source ResolveSource(const std::wstring& requestPath, const std::wstring& environmentPath,
                            const std::wstring& pathFileText, const std::wstring& gupDirectory) {
    if (const std::wstring path = Trim(requestPath); !path.empty()) return {Join(gupDirectory, path), Origin::Request};
    if (const std::wstring path = Trim(environmentPath); !path.empty())
        return {Join(gupDirectory, path), Origin::Environment};
    if (const std::wstring path = PathFileLine(pathFileText); !path.empty())
        return {Join(gupDirectory, path), Origin::PathFile};
    return {Join(gupDirectory, L"core\\mcp_core.dll"), Origin::Default};
}

// ── Shadow copies (pure naming) ──────────────────────────────────
// mcp_core-<pid>-<tick><extension>
inline std::wstring ShadowName(unsigned long pid, unsigned long long tick, const wchar_t* extension) {
    return L"mcp_core-" + std::to_wstring(pid) + L"-" + std::to_wstring(tick) + extension;
}

// A name ShadowName produces, with extension .dll or .pdb.
inline bool IsShadowName(const std::wstring& name) {
    const std::wstring prefix = L"mcp_core-";
    if (name.size() < prefix.size() + 7 || _wcsnicmp(name.c_str(), prefix.c_str(), prefix.size()) != 0) return false;
    const std::wstring extension = name.substr(name.size() - 4);
    if (_wcsicmp(extension.c_str(), L".dll") != 0 && _wcsicmp(extension.c_str(), L".pdb") != 0) return false;
    const std::wstring stamp = name.substr(prefix.size(), name.size() - prefix.size() - 4);
    const size_t dash = stamp.find(L'-');
    if (dash == std::wstring::npos || dash == 0 || dash + 1 == stamp.size()) return false;
    for (size_t i = 0; i < stamp.size(); ++i)
        if (i != dash && (stamp[i] < L'0' || stamp[i] > L'9')) return false;
    return true;
}

// ── Loading (Windows) ────────────────────────────────────────────
// One mapped core.
struct Module {
    HMODULE handle = nullptr;
    const McpCoreApi* api = nullptr;
    std::wstring source;     // the file the copy was made from
    std::wstring shadow;     // the mapped copy
    std::wstring shadowPdb;  // its symbols, when they were copied
};

struct Failure {
    std::string code;  // CORE_LOAD_FAILED or CORE_VERSION_MISMATCH
    std::string message;
};

std::string ToUtf8(const std::wstring& text);
std::wstring FromUtf8(const std::string& text);

// %TEMP%\3dsmax-mcp\core
std::wstring DefaultShadowDirectory();
// The folder of the module that contains `address`.
std::wstring ModuleDirectory(const void* address);
// An environment variable, or empty.
std::wstring Environment(const wchar_t* name);
// A small text file (UTF-8 with or without BOM, or UTF-16LE with BOM), or empty.
std::wstring ReadText(const std::wstring& path);
// GetFullPathName, or `path` unchanged.
std::wstring FullPath(const std::wstring& path);

// Best effort: deletes copies no process maps any more, except `keep` and its
// symbols. A mapped copy cannot be deleted; its .pdb goes only with it.
void SweepShadows(const std::wstring& directory, const std::wstring& keep);

// Copies `source` (and its .pdb when present) into `directory`, maps the copy
// and checks its stamps against `expected`. On failure nothing stays mapped
// or copied and `failure` says why. Runs no code of the core besides its
// static initializers and the mcp_core_api export.
bool Load(const std::wstring& source, const std::wstring& directory, const McpCoreExpectation& expected,
          Module& out, Failure& failure);

// Unmaps the core and deletes its copy (best effort: a file that is still
// locked stays for the next sweep). Leaves `module` empty; no-op when empty.
void Unload(Module& module);

}  // namespace McpCoreLoader
