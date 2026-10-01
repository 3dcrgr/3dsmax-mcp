#pragma once
#include <windows.h>
#include <nlohmann/json.hpp>

// Reads and operates Qt dialogs through Max's own Qt (5.15 or 6.x). The
// header stays Qt-free; every function must run on Max's main (GUI) thread.
namespace QtDialogs {
// Returns null when hwnd is not a visible top-level QWidget window.
nlohmann::json Snapshot(HWND hwnd);
// Clicks buttons[index] from Snapshot's ordering. Throws if it is unavailable.
void Click(HWND hwnd, int index);
}
