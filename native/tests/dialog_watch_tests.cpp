// SDK-independent tests for the fork's DialogWatch rules on top of upstream
// 1.7.5: Cosmos browser, Material Editor and viewport windows are never
// blocking dialogs; dialogs of other threads are listed but never messaged;
// Qt reads never send into a main thread that stopped pumping; a Qt press that
// timed out never clicks later; an acknowledged error is published before the
// operation can commit; a Qt error box refused while the main thread did not
// pump is acknowledged once it pumps again; a Win32 press goes to the button's
// own parent, is refused while the main thread does not pump and is dropped if
// the button changed before delivery; timed-out field reads of closed dialogs
// never disable later reads.
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

std::atomic<int> g_qt_reads{0}, g_qt_clicks{0}, g_qt_error_reads{0}, g_qt_error_clicks{0};
std::atomic<bool> g_pumping{true}, g_slow_next_read{false};
std::atomic<HWND> g_qt_error{nullptr};

json qt_snapshot(HWND hwnd) {
    const json ok = json::array({{{"index", 0}, {"label", "OK"}, {"enabled", true}, {"kind", "push"}}});
    if (hwnd == g_qt_error.load()) {
        ++g_qt_error_reads;
        return {{"kind", "qt"}, {"title", "MAXScript Runtime Error"}, {"text", "-- Runtime error: qt test"},
                {"buttons", ok}, {"fields", json::array()}, {"complete", true}};
    }
    ++g_qt_reads;
    if (g_slow_next_read.exchange(false)) Sleep(2200);  // longer than the 1.5 s lane timeout
    return {{"kind", "qt"}, {"title", "Qt Dialog"}, {"text", "qt text"},
            {"buttons", ok}, {"fields", json::array()}, {"complete", true}};
}
void qt_click(HWND hwnd, int) {
    if (hwnd == g_qt_error.load()) {
        ++g_qt_error_clicks;
        DestroyWindow(hwnd);
    } else {
        ++g_qt_clicks;
    }
}

// A recognized Win32 MAXScript error box. Its OK press is the first moment the
// operation's thread runs again, so it checks for the acknowledgment there.
std::string g_ack_seen;
std::atomic<bool> g_ack_done{false};
LRESULT CALLBACK ErrorBoxProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (msg == WM_COMMAND && LOWORD(wp) == IDOK && HIWORD(wp) == BN_CLICKED) {
        try {
            DialogWatch::ThrowIfDismissed();
            g_ack_seen = "no error";
        } catch (const std::exception& e) {
            g_ack_seen = e.what();
        }
        g_ack_done = true;
        DestroyWindow(hwnd);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

// Pumps like a modal loop until done() or the timeout.
template <class Done>
bool pump_until(Done done, int timeout_ms) {
    const auto start = Clock::now();
    while (!done() && ms_since(start) < timeout_ms) {
        MsgWaitForMultipleObjects(0, nullptr, FALSE, 20, QS_ALLINPUT);
        pump();
    }
    return done();
}

json error_payload(const std::string& error) {
    const json parsed = json::parse(error, nullptr, false);
    return parsed.is_object() ? parsed : json::object();
}

int run() {
    register_class(L"DWTestWindow");
    register_class(L"Qt5QWindowIcon");  // what IsQt() recognises
    DialogWatch::SetMainThreadPumping([] { return g_pumping.load(); });
    DialogWatch::Start({qt_snapshot, qt_click});
    {
        WNDCLASSEXW wc{sizeof(wc)};
        wc.lpfnWndProc = ErrorBoxProc;
        wc.hInstance = GetModuleHandleW(nullptr);
        wc.lpszClassName = L"DWErrorBox";
        RegisterClassExW(&wc);
    }

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
    require(!control({{"action", "status"}}).value("main_thread_pumping", true),
            "status does not report a main thread that stopped pumping");
    g_pumping = true;
    require(control({{"action", "status"}}).value("main_thread_pumping", false),
            "status does not report a pumping main thread");

    // 5. A Qt press whose send timed out before the main thread took it is
    //    withdrawn: MAIN_THREAD_BUSY (retryable) and no click afterwards.
    //    (Windows 11 already drops such a message; the lane does not rely on it.)
    inspect = control({{"action", "inspect"}});
    json qt_dialog_read = find_dialog(inspect, "Qt Dialog");
    {
        std::string failure;
        std::thread worker([&] {
            try {
                DialogWatch::Control(json{{"action", "respond"}, {"dialog_id", qt_dialog_read["dialog_id"]},
                                          {"expected_dialog", qt_dialog_read["expected_dialog"]},
                                          {"button", 0}}.dump());
            } catch (const std::exception& e) { failure = e.what(); }
        });
        Sleep(2200);  // busy main thread, heartbeat still fresh: the 1.5 s send times out
        worker.join();
        const auto start = Clock::now();
        while (ms_since(start) < 400) { pump(); Sleep(5); }  // were it delivered late, it does nothing
        const json failed = error_payload(failure);
        require(failed.value("code", "") == "MAIN_THREAD_BUSY" && failed.value("retryable", false),
                ("timed-out press not reported as retryable MAIN_THREAD_BUSY: " + failure).c_str());
        require(g_qt_clicks.load() == 0, "a press reported as not made clicked after its timeout");
    }

    // 6. A Qt press the main thread started but did not finish in time may
    //    still click: its outcome is reported unknown, never retryable.
    inspect = control({{"action", "inspect"}});
    qt_dialog_read = find_dialog(inspect, "Qt Dialog");
    g_slow_next_read = true;
    error.clear();
    control({{"action", "respond"}, {"dialog_id", qt_dialog_read["dialog_id"]},
             {"expected_dialog", qt_dialog_read["expected_dialog"]}, {"button", 0}}, &error);
    {
        const json unknown = error_payload(error);
        require(unknown.value("code", "") == "DIALOG_OUTCOME_UNKNOWN" && !unknown.value("retryable", true),
                ("started press not reported as outcome unknown: " + error).c_str());
        require(pump_until([] { return g_qt_clicks.load() == 1; }, 3000), "a started press did not click");
    }

    // 7. A recognized error box during an operation on this thread is
    //    acknowledged and published before the operation resumes, so a check
    //    right where it resumes (before a commit) already fails.
    {
        // One CPU, operation thread above the monitor: the posted click preempts
        // the monitor before it publishes, the order that let a commit slip by.
        DWORD_PTR process_mask = 0, system_mask = 0;
        GetProcessAffinityMask(GetCurrentProcess(), &process_mask, &system_mask);
        DWORD_PTR one_cpu = process_mask & (~process_mask + 1);
        SetProcessAffinityMask(GetCurrentProcess(), one_cpu);
        SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST);
        struct Restore {
            DWORD_PTR mask;
            ~Restore() {
                SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_NORMAL);
                SetProcessAffinityMask(GetCurrentProcess(), mask);
            }
        } restore{process_mask};
        DialogWatch::Guard guard("req-ack", "native:test");
        HWND box = top(L"DWErrorBox", L"MAXScript Runtime Error", owner);
        CreateWindowExW(0, L"Static", L"-- Runtime error: test", WS_CHILD | WS_VISIBLE, 0, 0, 150, 20, box, nullptr,
                        GetModuleHandleW(nullptr), nullptr);
        CreateWindowExW(0, L"Button", L"OK", WS_CHILD | WS_VISIBLE | BS_DEFPUSHBUTTON, 0, 40, 60, 20, box,
                        reinterpret_cast<HMENU>(static_cast<INT_PTR>(IDOK)), GetModuleHandleW(nullptr), nullptr);
        require(pump_until([] { return g_ack_done.load(); }, 5000), "recognized error box was not acknowledged");
        require(g_ack_seen.find("MAX_DIALOG_ERROR") != std::string::npos,
                ("operation resumed before its acknowledged error was published: " + g_ack_seen).c_str());
        require(guard.Error().find("MAX_DIALOG_ERROR") != std::string::npos, "operation not failed by the error box");
        {
            DialogWatch::DeferDismissed cleanup;
            DialogWatch::ThrowIfDismissed();  // cleanup steps keep running
        }
        bool thrown = false;
        try { DialogWatch::ThrowIfDismissed(); } catch (const std::exception&) { thrown = true; }
        require(thrown, "the dismissed error is lost after a deferred scope");
    }

    // 8. A Qt error box seen while the main thread does not pump is not read
    //    (and not used up); once the main thread pumps it is acknowledged.
    {
        g_pumping = false;
        DialogWatch::Guard guard("req-qt-ack", "native:test");
        g_qt_error = top(L"Qt5QWindowIcon", L"MAXScript Runtime Error", owner);
        pump_until([] { return false; }, 700);  // several monitor ticks
        require(g_qt_error_reads.load() == 0 && g_qt_error_clicks.load() == 0,
                "a Qt error box was read while the main thread was not pumping");
        g_pumping = true;
        require(pump_until([] { return g_qt_error_clicks.load() == 1; }, 3000),
                "a Qt error box refused while the main thread did not pump was never acknowledged");
        // The click can run before the monitor publishes; the check waits for it.
        std::string failure;
        try { DialogWatch::ThrowIfDismissed(); } catch (const std::exception& e) { failure = e.what(); }
        require(failure.find("MAX_DIALOG_ERROR") != std::string::npos,
                "the late acknowledgment did not fail the operation");
    }

    // 9. A Win32 press reaches the button's own parent (a nested pane here,
    //    like a file dialog's template), is refused while the main thread does
    //    not pump, and is checked again when the main thread takes it.
    const HINSTANCE instance = GetModuleHandleW(nullptr);
    HWND nested = top(L"DWTestWindow", L"Nested Pane Dialog", owner);
    HWND pane = CreateWindowExW(0, L"DWTestWindow", L"", WS_CHILD | WS_VISIBLE, 0, 0, 180, 80, nested, nullptr,
                                instance, nullptr);
    HWND apply = CreateWindowExW(0, L"Button", L"Apply", WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 0, 0, 60, 20, pane,
                                 reinterpret_cast<HMENU>(static_cast<INT_PTR>(1234)), instance, nullptr);
    require(pane && apply, "nested pane not created");
    {
        inspect = control({{"action", "inspect"}});
        const json nested_read = find_dialog(inspect, "Nested Pane Dialog");
        require(!nested_read.is_null() && nested_read["buttons"].size() == 1, "nested pane button not listed");
        const json respond = {{"action", "respond"}, {"dialog_id", nested_read["dialog_id"]},
                              {"expected_dialog", nested_read["expected_dialog"]}, {"button", "Apply"}};
        control(respond);
        require(pump_until([&] { return intrusive_count(pane) == 1; }, 3000),
                "the press did not reach the button's own parent pane");
        require(intrusive_count(nested) == 0, "the press went to the top-level dialog, which does not own the button");

        // Not pumping: refused (retryable), and nothing is queued for later.
        g_pumping = false;
        error.clear();
        control(respond, &error);
        g_pumping = true;
        const json busy = error_payload(error);
        require(busy.value("code", "") == "MAIN_THREAD_BUSY" && busy.value("retryable", false),
                ("Win32 press while the main thread does not pump not refused: " + error).c_str());
        pump_until([] { return false; }, 300);
        require(intrusive_count(pane) == 1, "a refused Win32 press clicked anyway");

        // Posted, but the button is disabled before the main thread takes it:
        // the press is dropped on delivery.
        std::string posted_error;
        std::thread worker([&] {
            try { DialogWatch::Control(respond.dump()); }
            catch (const std::exception& e) { posted_error = e.what(); }
        });
        worker.join();  // the main thread does not pump meanwhile
        require(posted_error.empty(), ("press not posted: " + posted_error).c_str());
        EnableWindow(apply, FALSE);
        pump_until([] { return false; }, 300);
        require(intrusive_count(pane) == 1, "a press delivered after its button was disabled still clicked");
    }

    // 10. Timed-out field reads keep their buffers only while their window
    //     exists: 70 dialogs whose edits could not be read (main thread not
    //     pumping) do not stop a later dialog's fields from being read.
    {
        g_pumping = false;  // Qt reads fail fast instead of waiting 1.5 s each
        for (int i = 0; i < 70; ++i) {
            HWND busy = top(L"DWTestWindow", L"Busy Fields", owner);
            edit_child(busy);
            std::thread worker([] {
                try { DialogWatch::Control(json{{"action", "inspect"}}.dump()); } catch (...) {}
            });
            worker.join();  // not pumping: the edit's WM_GETTEXT times out
            DestroyWindow(busy);
        }
        g_pumping = true;
        pump();
        HWND fresh = top(L"DWTestWindow", L"Fresh Fields", owner);
        edit_child(fresh);
        inspect = control({{"action", "inspect"}});
        const json fresh_read = find_dialog(inspect, "Fresh Fields");
        require(!fresh_read.is_null() && fresh_read["fields"].size() == 1 &&
                    fresh_read["fields"][0].value("value", "") == "value",
                "field reads stayed disabled after many timed-out reads of closed dialogs");
        DestroyWindow(fresh);
    }
    DestroyWindow(nested);

    stop = true;
    other_thread.join();
    for (const auto& [title, hwnd] : tools) DestroyWindow(hwnd);
    DestroyWindow(qt_dialog);
    DestroyWindow(save);
    DestroyWindow(owner);
    DialogWatch::Stop();
    std::cout << "PASS: tool windows are never dialogs; other-thread dialogs are listed, never read or pressed; "
                 "Qt reads need a pumping main thread; timed-out presses never click; acknowledgments publish "
                 "before the operation resumes; refused Qt error reads are retried; Win32 presses reach the "
                 "button's parent, need a pumping main thread and are re-checked on delivery; field reads "
                 "recover after timeouts\n";
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
