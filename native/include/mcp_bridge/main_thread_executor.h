#pragma once
#include <windows.h>
#include <functional>
#include <string>
#include <mutex>
#include <condition_variable>
#include <deque>
#include <memory>
#include <stdexcept>
#include <unordered_map>

// Executes work on the 3ds Max main thread from a background thread.
// Uses a hidden Win32 window + WM_USER message to marshal calls.
//
// Direct mode: when enabled (per-thread), ExecuteSync runs the work
// function directly on the calling thread, skipping the main-thread
// roundtrip. Use for read-only handlers that don't mutate scene state
// or call RunMAXScript. Eliminates PostMessage + condition_variable
// latency for reads.
class MainThreadExecutor {
public:
    MainThreadExecutor() = default;
    ~MainThreadExecutor();

    // Call from main thread (GUP::Start)
    void Initialize();

    // Main thread, before GUP::Stop joins the pipe threads. Max no longer
    // pumps the window while it exits: every request that has not started
    // fails now, and later requests fail without being queued.
    void StopAccepting();

    // Call from main thread (GUP::Stop)
    void Shutdown();

    // Call from ANY thread. In direct mode, runs work on calling thread.
    // Otherwise blocks until work completes on main thread.
    std::string ExecuteSync(std::function<std::string()> work,
                            DWORD timeout_ms = 120000);

    // Direct mode control (thread-local, safe for concurrent pipe clients).
    // Out of line: thread_local data cannot cross a DLL boundary, so the core
    // reaches these through McpHostApi::setDirectMode/getDirectMode.
    static void EnableDirectMode();
    static void DisableDirectMode();
    static bool IsDirectMode();

    // True on the thread that called Initialize() (Max's main thread), where
    // ExecuteSync runs work inline.
    bool IsOwnThread() const {
        return main_thread_id_ != 0 && GetCurrentThreadId() == main_thread_id_;
    }

    // Max's main thread: true while it runs a request, including inside the
    // nested message loops that request's SDK work pumps.
    static bool IsRunningWork() { return s_executing_; }

    struct WorkItem {
        std::function<std::string()> work;
        std::string result;
        bool completed = false;
        bool started = false;
        bool cancelled = false;
        bool error = false;
        std::string error_message;
        std::string request_id, command_type;
        std::mutex mutex;
        std::condition_variable cv;
    };

private:
    static LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp);
    static void RunWorkItem(const std::shared_ptr<WorkItem>& item);

    HWND hwnd_ = nullptr;
    ATOM wndclass_atom_ = 0;
    // Set once in Initialize() (called on the Max main thread). Read-only after.
    DWORD main_thread_id_ = 0;

    // Requests whose callers are waiting, by id. WM_MCP_EXECUTE carries only
    // the id; a caller removes its entry when it stops waiting, so a message
    // for a request nobody waits on finds nothing and is ignored.
    static std::mutex s_waiting_mutex_;
    static std::unordered_map<UINT64, std::shared_ptr<WorkItem>> s_waiting_;
    static UINT64 s_last_id_;
    static bool s_accepting_;

    static thread_local bool tl_direct_mode_;
    static constexpr UINT WM_MCP_EXECUTE = WM_USER + 0x4D43;

    // Re-entrancy guard. SDK calls inside a work item can run nested message
    // pumps (progress UI, redraws, deferred plugin loads); without this guard
    // a queued WM_MCP_EXECUTE gets dispatched in the MIDDLE of the running
    // item, interleaving theHold transactions on the global undo system —
    // observed as 0xC0000005 under concurrent mutating requests, followed by
    // persistent scene-state corruption. Items arriving while one is running
    // are deferred and drained after it completes. Main-thread-only state.
    static bool s_executing_;
    static std::deque<std::shared_ptr<WorkItem>> s_deferred_;

    // Per-process random secret. Sent in wParam alongside every
    // WM_MCP_EXECUTE so other processes can't start queued requests — same-user
    // processes can enumerate and PostMessage to top-level windows freely
    // (UIPI doesn't block WM_USER+ between same-integrity processes).
    static WPARAM s_execute_cookie_;
};
