// SDK-independent tests for the fork's DialogWatch rules on top of upstream
// 1.7.5: Cosmos browser, Material Editor and viewport windows are never
// blocking dialogs; dialogs of other threads are listed but never messaged;
// Qt reads never send into a main thread that stopped pumping.
//
//   cmake -S native/tests -B native/build-tests -G "Visual Studio 17 2022" -A x64
//   cmake --build native/build-tests --config Release
//   ctest --test-dir native/build-tests -C Release --output-on-failure
#include "mcp_bridge/dialog_watch.h"
#include <atomic>
#include <chrono>
#include <iostream>
#include <map>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>

using json = nlohmann::json;
using Clock = std::chrono::steady_clock;

void require(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }

long long ms_since(Clock::time_point start) {
    return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start).count();
}

// Messages a reader or a press would send or post to a dialog or its controls:
// reads arrive as sends from another thread; presses and closes are posted.
// (Edit controls notify their parent with WM_COMMAND EN_* on their own thread,
// and the shell may read titles; neither counts.)
std::mutex g_mutex;
std::map<HWND, int> g_intrusive;
bool intrusive(UINT msg, WPARAM wp) {
    if (msg == WM_GETTEXT || msg == BM_GETCHECK) return (InSendMessageEx(nullptr) & ISMEX_SEND) != 0;
    return msg == BM_CLICK || msg == WM_CLOSE || (msg == WM_COMMAND && HIWORD(wp) == BN_CLICKED);
}
LRESULT CALLBACK CountingProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (intrusive(msg, wp)) {
        std::lock_guard<std::mutex> lock(g_mutex);
        ++g_intrusive[hwnd];
        if (msg == WM_CLOSE) return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}
WNDPROC g_edit_proc = nullptr;
LRESULT CALLBACK CountingEditProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (intrusive(msg, wp)) {
        std::lock_guard<std::mutex> lock(g_mutex);
        ++g_intrusive[hwnd];
    }
    return CallWindowProcW(g_edit_proc, hwnd, msg, wp, lp);
}
int intrusive_count(HWND hwnd) {
    std::lock_guard<std::mutex> lock(g_mutex);
    auto it = g_intrusive.find(hwnd);
    return it == g_intrusive.end() ? 0 : it->second;
}

void register_class(const wchar_t* name) {
    WNDCLASSEXW wc{sizeof(wc)};
    wc.lpfnWndProc = CountingProc;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.lpszClassName = name;
    RegisterClassExW(&wc);
}

HWND top(const wchar_t* cls, const wchar_t* title, HWND owner) {
    HWND hwnd = CreateWindowExW(0, cls, title, WS_POPUP | WS_VISIBLE, -32000, -32000, 200, 100, owner, nullptr,
                                GetModuleHandleW(nullptr), nullptr);
    require(hwnd != nullptr, "window not created");
    return hwnd;
}

HWND edit_child(HWND parent) {
    HWND edit = CreateWindowExW(0, L"Edit", L"value", WS_CHILD | WS_VISIBLE, 0, 0, 80, 20, parent, nullptr,
                                GetModuleHandleW(nullptr), nullptr);
    require(edit != nullptr, "edit not created");
    g_edit_proc = reinterpret_cast<WNDPROC>(SetWindowLongPtrW(edit, GWLP_WNDPROC,
                                                             reinterpret_cast<LONG_PTR>(CountingEditProc)));
    return edit;
}

void pump() {
    MSG message;
    while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) DispatchMessageW(&message);
}

// Control must run off the main thread; the main thread keeps pumping meanwhile
// (the Qt lane needs it), exactly like Max inside a modal loop.
json control(const json& request, std::string* error = nullptr) {
    std::atomic<bool> done{false};
    std::string result, failure;
    std::thread worker([&] {
        try { result = DialogWatch::Control(request.dump()); }
        catch (const std::exception& e) { failure = e.what(); }
        done = true;
    });
    const auto start = Clock::now();
    while (!done.load() && ms_since(start) < 10000) { pump(); Sleep(2); }
    worker.join();
    if (error) *error = failure;
    else require(failure.empty(), failure.c_str());
    return result.empty() ? json() : json::parse(result);
}

json find_dialog(const json& listing, const std::string& title) {
    for (const auto& dialog : listing["dialogs"])
        if (dialog.value("title", "") == title) return dialog;
    return json();
}

std::atomic<int> g_qt_reads{0};
std::atomic<bool> g_pumping{true};

int run() {
    register_class(L"DWTestWindow");
    register_class(L"Qt5QWindowIcon");  // what IsQt() recognises
    DialogWatch::SetMainThreadPumping([] { return g_pumping.load(); });
    DialogWatch::Start({[](HWND) -> json {
                            ++g_qt_reads;
                            return {{"kind", "qt"}, {"title", "Qt Dialog"}, {"text", "qt text"},
                                    {"buttons", json::array()}, {"fields", json::array()}, {"complete", true}};
                        },
                        [](HWND, int) {}});

    HWND owner = top(L"DWTestWindow", L"Owner", nullptr);
    EnableWindow(owner, FALSE);  // a modal loop disabled it
    std::map<std::string, HWND> tools;
    for (const char* title : {"Chaos Cosmos Browser", "Material Editor - 01 - Default", "Slate Material Editor",
                              "AGENT VIEWPORT (do not close or minimize while agent is working)",
                              "Floating Viewport - 3"}) {
        const std::wstring wide(title, title + strlen(title));  // ASCII titles
        tools[title] = top(L"DWTestWindow", wide.c_str(), owner);
    }
    HWND save = top(L"DWTestWindow", L"Save Changes", owner);
    HWND save_edit = edit_child(save);
    HWND qt_dialog = top(L"Qt5QWindowIcon", L"Qt Dialog", owner);

    // A dialog on a separate thread, e.g. one owned by a Cosmos importer thread.
    std::atomic<bool> stop{false};
    std::atomic<HWND> other{nullptr}, other_edit{nullptr};
    struct Joiner {
        std::atomic<bool>& stop;
        std::thread& thread;
        ~Joiner() { stop = true; if (thread.joinable()) thread.join(); }
    };
    std::thread other_thread;
    Joiner joiner{stop, other_thread};
    other_thread = std::thread([&] {
        HWND other_owner = top(L"DWTestWindow", L"Importer owner", nullptr);
        EnableWindow(other_owner, FALSE);
        HWND dialog = top(L"DWTestWindow", L"Other Thread Dialog", other_owner);
        other_edit = edit_child(dialog);
        other = dialog;
        MSG message;
        while (!stop.load()) {
            while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) DispatchMessageW(&message);
            Sleep(2);
        }
        DestroyWindow(dialog);
        DestroyWindow(other_owner);
    });
    const auto start = Clock::now();
    while (!other.load() && ms_since(start) < 5000) Sleep(2);
    require(other.load() != nullptr, "other-thread dialog not created");
    pump();

    // 1. status: Cosmos/Material Editor/viewport windows are never dialogs.
    json status = control({{"action", "status"}});
    for (const auto& [title, hwnd] : tools)
        require(find_dialog(status, title).is_null(), ("tool window listed as a dialog: " + title).c_str());
    require(DialogWatch::OpenDialogs().size() == 3, "openDialogs should list exactly the three real dialogs");
    const json save_summary = find_dialog(status, "Save Changes");
    require(!save_summary.is_null() && save_summary.value("main_thread", false), "main-thread dialog missing");
    const json other_summary = find_dialog(status, "Other Thread Dialog");
    require(!other_summary.is_null() && !other_summary.value("main_thread", true), "other-thread dialog missing");

    // 2. inspect reads the main-thread dialog, never the other thread's.
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        g_intrusive.clear();  // only what the bridge sends from here on counts
    }
    json inspect = control({{"action", "inspect"}});
    const json save_read = find_dialog(inspect, "Save Changes");
    require(save_read["fields"].size() == 1 && save_read["fields"][0].value("value", "") == "value",
            "main-thread dialog fields not read");
    require(intrusive_count(save_edit) > 0, "main-thread edit was not read");
    const json other_read = find_dialog(inspect, "Other Thread Dialog");
    require(other_read.contains("unavailable"), "other-thread dialog not marked unavailable");
    require(intrusive_count(other.load()) == 0 && intrusive_count(other_edit.load()) == 0,
            "a message reached a dialog on another thread");
    for (const auto& [title, hwnd] : tools)
        require(intrusive_count(hwnd) == 0, ("a message reached a tool window: " + title).c_str());

    // 3. respond refuses another thread's dialog without sending anything.
    std::string error;
    control({{"action", "respond"}, {"dialog_id", other_summary["dialog_id"]},
             {"expected_dialog", other_read["expected_dialog"]}, {"button", 0}}, &error);
    require(error.find("DIALOG_NOT_ON_MAIN_THREAD") != std::string::npos, "other-thread press not refused");
    require(intrusive_count(other.load()) == 0, "refused press still messaged the dialog");

    // 4. Qt dialogs are read through the lane while the main thread pumps,
    //    and never while it is known not to pump.
    require(find_dialog(inspect, "Qt Dialog").value("text", "") == "qt text", "Qt dialog not read through the lane");
    const int reads = g_qt_reads.load();
    g_pumping = false;
    inspect = control({{"action", "inspect"}});
    const json qt_read = find_dialog(inspect, "Qt Dialog");
    require(g_qt_reads.load() == reads, "Qt dialog read while the main thread was not pumping");
    require(qt_read.value("unavailable", "").find("not processing messages") != std::string::npos,
            "Qt dialog not reported unreadable while the main thread is not pumping");
    g_pumping = true;

    stop = true;
    other_thread.join();
    for (const auto& [title, hwnd] : tools) DestroyWindow(hwnd);
    DestroyWindow(qt_dialog);
    DestroyWindow(save);
    DestroyWindow(owner);
    DialogWatch::Stop();
    std::cout << "PASS: tool windows are never dialogs; other-thread dialogs are listed, never read or pressed; "
                 "Qt reads need a pumping main thread\n";
    return 0;
}

int main() {
    try {
        return run();
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        DialogWatch::Stop();
        return 1;
    }
}
