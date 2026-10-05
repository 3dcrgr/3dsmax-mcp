#pragma once
#include <cctype>
#include <string>

// Max's quiet mode answers every prompt with its default. For the "save
// changes?" prompt of a reset, open, fetch or quit, the default can throw away
// unsaved work, silently (fork issue #12). So the bridge keeps quiet mode off
// for any script that mentions such a command, and the prompt reaches the
// agent as a blocking dialog instead.
//
// The match is deliberately blunt: anywhere in the text, strings and comments
// included, whatever the arguments. A false positive only leaves a prompt
// visible (and #noPrompt / quiet:true arguments still suppress it); a false
// negative could discard a scene. Merging is not listed: it never asks to save.
// A text check cannot see a command reached indirectly (fileIn of a .ms file,
// macros.run, actionMan.executeAction, a name built at run time, a callback):
// such scripts need an explicit quiet=false.
namespace QuietPolicy {

inline std::string Normalized(const std::string& script) {
    std::string out;
    out.reserve(script.size());
    bool space = false;
    for (unsigned char c : script) {
        if (std::isspace(c)) {
            space = true;
            continue;
        }
        if (space && !out.empty()) out.push_back(' ');
        space = false;
        out.push_back(static_cast<char>(std::tolower(c)));
    }
    return out;
}

// True when `script` mentions a command that can prompt to save or discard
// the open scene.
inline bool MentionsSceneFileCommand(const std::string& script) {
    static const char* const kNames[] = {
        "resetmaxfile", "loadmaxfile", "fetchmaxfile", "quitmax", "checkforsave",
        "max reset file", "max file new", "max file open", "max fetch",
        // Interface spellings, e.g. through Autodesk.Max's COREInterface.
        "filereset", "filefetch", "loadfromfile",
    };
    const std::string text = Normalized(script);
    for (const char* name : kNames) {
        if (text.find(name) != std::string::npos) return true;
    }
    return false;
}

// Quiet mode for one script: an explicit request wins; otherwise quiet unless
// the script could prompt to save or discard the scene.
inline bool ResolveQuiet(const std::string& script, int explicit_quiet /* -1 unset, 0 false, 1 true */,
                         bool* overridden = nullptr) {
    if (explicit_quiet >= 0) {
        if (overridden) *overridden = false;
        return explicit_quiet == 1;
    }
    const bool risky = MentionsSceneFileCommand(script);
    if (overridden) *overridden = risky;
    return !risky;
}

}  // namespace QuietPolicy
