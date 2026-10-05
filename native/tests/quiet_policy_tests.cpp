// SDK-independent tests for QuietPolicy (fork issue #12): a script that could
// prompt to save or discard the scene never runs in Max's quiet mode.
#include "mcp_bridge/quiet_policy.h"
#include <iostream>
#include <stdexcept>
#include <string>

void require(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }

void detects_file_commands_anywhere() {
    using QuietPolicy::MentionsSceneFileCommand;
    require(MentionsSceneFileCommand("resetMaxFile()"), "plain call");
    require(MentionsSceneFileCommand("RESETMAXFILE #noPrompt"), "case and #noPrompt");
    require(MentionsSceneFileCommand("loadMaxFile @\"C:/a.max\" quiet:true"), "loadMaxFile");
    require(MentionsSceneFileCommand("fetchMaxFile quiet:true"), "fetchMaxFile");
    require(MentionsSceneFileCommand("(quitMax #noPrompt; \"x\")"), "quitMax");
    require(MentionsSceneFileCommand("if checkForSave() do resetMaxFile #noPrompt"), "checkForSave");
    require(MentionsSceneFileCommand("execute \"resetMaxFile()\""), "inside a string");
    require(MentionsSceneFileCommand("-- resetMaxFile later\n1+1"), "inside a comment");
    require(MentionsSceneFileCommand("local f = \"reset\" + \"MaxFile\"\nexecute (f + \"()\")") == false,
            "string-built names are not visible to a text check (documented limit)");
    require(MentionsSceneFileCommand("max reset file"), "max reset file");
    require(MentionsSceneFileCommand("max   file\topen"), "whitespace-tolerant max file open");
    require(MentionsSceneFileCommand("MAX FILE NEW"), "max file new");
    require(MentionsSceneFileCommand("max fetch"), "max fetch (Edit > Fetch) discards changes since Hold");
    require(MentionsSceneFileCommand("max\n  Fetch"), "whitespace-tolerant max fetch");
    require(MentionsSceneFileCommand("(dotNetClass \"Autodesk.Max.GlobalInterface\").Instance.COREInterface.FileReset false"),
            "Interface FileReset");
    require(MentionsSceneFileCommand("core.FileFetch()"), "Interface FileFetch");
    require(MentionsSceneFileCommand("core.LoadFromFile @\"C:/a.max\" 0 true"), "Interface LoadFromFile");
}

void leaves_other_scripts_alone() {
    using QuietPolicy::MentionsSceneFileCommand;
    require(!MentionsSceneFileCommand("box(); 1+1"), "unrelated script");
    require(!MentionsSceneFileCommand("mergeMaxFile @\"C:/a.max\" #autoRenameDups quiet:true"),
            "merging never asks to save");
    require(!MentionsSceneFileCommand("holdMaxFile()"), "hold does not prompt");
    require(!MentionsSceneFileCommand("saveMaxFile fp"), "save does not discard");
    require(!MentionsSceneFileCommand("max file save"), "max file save");
    require(!MentionsSceneFileCommand("max hold"), "max hold only saves the hold buffer");
}

void resolves_quiet() {
    bool overridden = true;
    require(QuietPolicy::ResolveQuiet("box()", -1, &overridden) && !overridden, "default quiet");
    require(!QuietPolicy::ResolveQuiet("resetMaxFile()", -1, &overridden) && overridden, "file command: not quiet");
    require(QuietPolicy::ResolveQuiet("resetMaxFile()", 1, &overridden) && !overridden, "explicit quiet wins");
    require(!QuietPolicy::ResolveQuiet("box()", 0, &overridden) && !overridden, "explicit not quiet wins");
}

int main() {
    try {
        detects_file_commands_anywhere();
        leaves_other_scripts_alone();
        resolves_quiet();
        std::cout << "PASS: quiet policy\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
