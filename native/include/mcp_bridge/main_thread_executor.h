#pragma once
#include <windows.h>
#include <functional>
#include <string>
#include <mutex>
#include <condition_variable>
#include <deque>
#include <memory>
#include <stdexcept>
#include <atomic>

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

    // Call from main thread (GUP::Stop) BEFORE tearing down anything that
    // joins background threads (PipeServer::Stop). Closes the submission gate,
    // then fails every queued/deferred WorkItem so blocked client threads wake
    // at once instead of sleeping out their timeout while the main thread sits
    // in thread::join() and can no longer pump WM_MCP_EXECUTE (Max hangs on
    // exit). Idempotent; Shutdown() calls it.
    void BeginShutdown();

    // Call from main thread (GUP::Stop)
    void Shutdown();

    // True once BeginShutdown()/Shutdown() ran; ExecuteSync then fails fast.
    static bool IsShuttingDown() { return s_shutting_down_.load(std::memory_order_acquire); }

    // Call from ANY thread. In direct mode, runs work on calling thread.
    // Otherwise blocks until work completes on main thread.
    std::string ExecuteSync(std::function<std::string()> work,
                            DWORD timeout_ms = 120000);

    // Direct mode control (thread-local, safe for concurrent pipe clients)
    static void EnableDirectMode()  { tl_direct_mode_ = true; }
    static void DisableDirectMode() { tl_direct_mode_ = false; }
    static bool IsDirectMode()      { return tl_direct_mode_; }

    struct WorkItem {
        std::function<std::string()> work;
        std::string result;
        bool completed = false;
        bool error = false;
        std::string error_message;
        std::mutex mutex;
        std::condition_variable cv;
    };

private:
    static LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp);
    static void RunWorkItem(const std::shared_ptr<WorkItem>& item);
    // Completes an item with an error and wakes its waiter. No-op if the item
    // already finished (or timed out), so it is safe to call twice.
    static void FailWorkItem(const std::shared_ptr<WorkItem>& item, const char* message);
    // Main thread only. Fails s_deferred_ and any WM_MCP_EXECUTE still queued,
    // deleting the heap shared_ptr each message owns (DestroyWindow would
    // discard them: leaked control block + a waiter nobody ever wakes).
    void DrainPendingWork();

    HWND hwnd_ = nullptr;
    ATOM wndclass_atom_ = 0;
    // Set once in Initialize() (called on the Max main thread). Read-only after.
    DWORD main_thread_id_ = 0;

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

    // Set under s_submit_mutex_ so the check-then-post in ExecuteSync cannot
    // race the drain in BeginShutdown: a poster either got its message queued
    // before the flag was set (the drain then fails it) or sees the flag and
    // throws. Static because WndProc and the deferred queue are.
    static std::atomic<bool> s_shutting_down_;
    static std::mutex s_submit_mutex_;

    // Per-process random secret. Sent in wParam alongside every
    // WM_MCP_EXECUTE so cross-process attackers can't smuggle pointers
    // for WndProc to reinterpret_cast — same-user processes can enumerate
    // and PostMessage to top-level windows freely (UIPI doesn't block
    // WM_USER+ between same-integrity processes).
    static WPARAM s_execute_cookie_;
};
