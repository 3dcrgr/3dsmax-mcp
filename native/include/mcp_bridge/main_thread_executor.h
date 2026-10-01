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
#include <chrono>
#include <list>

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

    // Names the work items this thread submits (the dispatcher uses the
    // request's cmd type, client id and request id) so health snapshots can
    // say whose request is queued or running.
    struct WorkLabel {
        std::string cmd_type;
        std::string client_id;
        std::string request_id;
    };
    static void SetThreadLabel(const WorkLabel& label) { tl_label_ = label; }
    static void SetThreadLabel(const std::string& cmd_type) { tl_label_ = WorkLabel{cmd_type, {}, {}}; }
    static const WorkLabel& ThreadLabel() { return tl_label_; }

    // Main-thread heartbeat period. A thread-pool timer posts a beat message to
    // the executor window (one outstanding at a time); the main thread stamps
    // the heartbeat when it retrieves it. Posted messages are retrieved in
    // order and ahead of input, WM_PAINT and WM_TIMER, exactly like
    // WM_MCP_EXECUTE, so a heartbeat older than a few periods means queued
    // bridge work would not be picked up either (busy, blocked, or a loop
    // that filters our messages). WM_TIMER is not used: it is starved by any
    // steady stream of posted messages or paints while the thread still pumps.
    static constexpr UINT kHeartbeatMs = 1000;

    // Snapshot for the pipe-thread "health" command. Never waits on the main
    // thread or on a running item's mutex, so it answers while Max is hung.
    struct Health {
        bool initialized = false;
        bool shutting_down = false;
        size_t queued = 0;                 // posted or deferred, not started
        long long oldest_queued_ms = -1;   // -1: nothing queued
        WorkLabel oldest_queued_label;
        bool running = false;
        WorkLabel running_label;
        long long running_ms = -1;
        long long heartbeat_age_ms = -1;   // -1: heartbeat unavailable (timer not created)
        bool executor_window_hung = false; // IsHungAppWindow on the hidden window
        unsigned long long completed = 0;
    };
    Health GetHealth() const;

    static constexpr int kQueued = 0;
    static constexpr int kRunning = 1;
    static constexpr int kDone = 2;

    // Health bookkeeping, kept apart from WorkItem so the pending list never
    // owns a WorkItem: destroying one destroys its callback and captures, which
    // must not happen on a pipe thread under s_stats_mutex_. phase is atomic
    // because RunWorkItem holds the item mutex for the whole callback and
    // GetHealth must never block on it.
    struct Ticket {
        WorkLabel label;
        std::chrono::steady_clock::time_point posted_at{};
        std::atomic<int> phase{kQueued};
    };

    struct WorkItem {
        std::function<std::string()> work;
        std::string result;
        bool completed = false;
        bool error = false;
        std::string error_message;
        std::mutex mutex;
        std::condition_variable cv;
        std::shared_ptr<Ticket> ticket = std::make_shared<Ticket>();
    };

private:
    static LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp);
    static void RunWorkItem(const std::shared_ptr<WorkItem>& item);
    // Health bookkeeping: remember a posted item / which item is running.
    static void TrackPending(const std::shared_ptr<WorkItem>& item);
    static void SetRunning(const WorkItem* item);
    static long long SteadyNowMs();
    // Thread-pool timer callback: posts one heartbeat beat to the executor window.
    static void CALLBACK HeartbeatTimerProc(PTP_CALLBACK_INSTANCE, PVOID context, PTP_TIMER);
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
    PTP_TIMER heartbeat_timer_ = nullptr;

    static thread_local bool tl_direct_mode_;
    static thread_local WorkLabel tl_label_;
    static constexpr UINT WM_MCP_EXECUTE = WM_USER + 0x4D43;
    // Heartbeat beat (cookie in wParam, no payload). +1 is the macroscript channel.
    static constexpr UINT WM_MCP_HEARTBEAT = WM_MCP_EXECUTE + 2;
    // An unanswered beat is re-posted after this long, so a beat a foreign
    // loop swallowed cannot freeze the heartbeat for good (a hung thread gains
    // at most one tiny message per interval).
    static constexpr long long kBeatRepostMs = 10LL * kHeartbeatMs;

    // Health state. s_pending_ holds the tickets of posted items and is pruned
    // lazily; the running fields describe the item RunWorkItem is executing.
    // Guarded by s_stats_mutex_, which is only ever taken for short reads and
    // writes (never while waiting on anything else).
    static std::mutex s_stats_mutex_;
    static std::list<std::shared_ptr<Ticket>> s_pending_;
    static bool s_running_;
    static WorkLabel s_running_label_;
    static std::chrono::steady_clock::time_point s_running_since_;
    static std::atomic<unsigned long long> s_completed_;
    static std::atomic<long long> s_heartbeat_ms_;   // SteadyNowMs(); 0 = heartbeat unavailable
    static std::atomic<long long> s_beat_posted_ms_; // SteadyNowMs() of the outstanding beat; 0 = none
    // Copy of hwnd_ readable from pipe threads (hwnd_ itself is main-thread state).
    std::atomic<HWND> health_hwnd_{nullptr};

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
