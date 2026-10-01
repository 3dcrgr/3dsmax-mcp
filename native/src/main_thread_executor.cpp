#include "mcp_bridge/main_thread_executor.h"

#include <random>

namespace {
constexpr char kShutdownError[] = "MainThreadExecutor is shutting down";
}

thread_local bool MainThreadExecutor::tl_direct_mode_ = false;
WPARAM MainThreadExecutor::s_execute_cookie_ = 0;
bool MainThreadExecutor::s_executing_ = false;
std::deque<std::shared_ptr<MainThreadExecutor::WorkItem>> MainThreadExecutor::s_deferred_;
std::atomic<bool> MainThreadExecutor::s_shutting_down_{false};
std::mutex MainThreadExecutor::s_submit_mutex_;

MainThreadExecutor::~MainThreadExecutor() {
    Shutdown();
}

void MainThreadExecutor::Initialize() {
    // A previous Start/Stop cycle may have closed the gate; reopen it before
    // anything can post.
    {
        std::lock_guard<std::mutex> lock(s_submit_mutex_);
        s_shutting_down_.store(false, std::memory_order_release);
    }

    // Initialize() is called from GUP::Start on the Max main thread, so this is
    // the thread that owns hwnd_ and pumps WM_MCP_EXECUTE. ExecuteSync uses it
    // to detect re-entrant calls already on the main thread.
    main_thread_id_ = GetCurrentThreadId();

    // Generate a per-process cookie before the window exists. std::random_device
    // on MSVC is non-deterministic. Reject 0 so we have a single sentinel value
    // any unauthenticated sender will fail against.
    if (s_execute_cookie_ == 0) {
        std::random_device rd;
        uint64_t c = (static_cast<uint64_t>(rd()) << 32) ^ rd();
        if (c == 0) c = 0xC001'D00D'C0FFEEULL; // unreachable in practice
        s_execute_cookie_ = static_cast<WPARAM>(c);
    }

    // Register a hidden window class
    WNDCLASSEX wc = {};
    wc.cbSize = sizeof(WNDCLASSEX);
    wc.lpfnWndProc = WndProc;
    wc.hInstance = GetModuleHandle(nullptr);
    wc.lpszClassName = L"MCPBridgeExecutor";

    wndclass_atom_ = RegisterClassEx(&wc);
    if (!wndclass_atom_) return;

    // Create hidden window — NOT HWND_MESSAGE so FindWindow/getChildHWND can
    // find it. The title is process-specific because MAXScript macroscripts
    // are persisted in a shared usermacros folder across Max instances.
    std::wstring window_title = L"MCPBridgeExecutor-" + std::to_wstring(GetCurrentProcessId());
    hwnd_ = CreateWindowEx(
        0, L"MCPBridgeExecutor", window_title.c_str(),
        0, 0, 0, 0, 0,
        nullptr,
        nullptr, GetModuleHandle(nullptr), nullptr
    );
}

void MainThreadExecutor::BeginShutdown() {
    // Close the gate first: from here on ExecuteSync throws instead of posting,
    // so the drain below cannot race a fresh submission into the queue.
    {
        std::lock_guard<std::mutex> lock(s_submit_mutex_);
        s_shutting_down_.store(true, std::memory_order_release);
    }
    DrainPendingWork();
}

void MainThreadExecutor::DrainPendingWork() {
    // Items parked by the re-entrancy guard never reach the message queue.
    while (!s_deferred_.empty()) {
        auto item = std::move(s_deferred_.front());
        s_deferred_.pop_front();
        FailWorkItem(item, kShutdownError);
    }

    if (!hwnd_) return;

    MSG message;
    while (PeekMessage(&message, hwnd_, WM_MCP_EXECUTE, WM_MCP_EXECUTE, PM_REMOVE)) {
        if (message.wParam != s_execute_cookie_ || message.lParam == 0) continue;
        auto* raw = reinterpret_cast<std::shared_ptr<WorkItem>*>(message.lParam);
        auto item = *raw;
        delete raw;
        FailWorkItem(item, kShutdownError);
    }
}

void MainThreadExecutor::Shutdown() {
    BeginShutdown();
    if (hwnd_) {
        DestroyWindow(hwnd_);
        hwnd_ = nullptr;
    }
    if (wndclass_atom_) {
        UnregisterClass(L"MCPBridgeExecutor", GetModuleHandle(nullptr));
        wndclass_atom_ = 0;
    }
}

std::string MainThreadExecutor::ExecuteSync(
    std::function<std::string()> work, DWORD timeout_ms) {

    // Already on the main thread (e.g. a handler that re-enters Dispatch).
    // Posting to ourselves would
    // block the only thread that can pump the message — a guaranteed deadlock
    // until timeout. Run inline; we are already where the work needs to run.
    if (main_thread_id_ != 0 && GetCurrentThreadId() == main_thread_id_) {
        return work();
    }

    // Shutting down: the main thread is tearing the bridge down and is about to
    // block joining this very thread, so nothing will ever pump our message.
    // Checked after the main-thread path so teardown code running inline still
    // works, and before direct mode so no background thread touches a dying scene.
    if (s_shutting_down_.load(std::memory_order_acquire)) {
        throw std::runtime_error(kShutdownError);
    }

    // Direct mode: run on calling thread, skip main-thread roundtrip.
    // Used for read-only handlers on pipe worker threads.
    if (tl_direct_mode_) {
        return work();
    }

    if (!hwnd_) {
        throw std::runtime_error("MainThreadExecutor not initialized");
    }

    auto item = std::make_shared<WorkItem>();
    item->work = std::move(work);

    // prevent shared_ptr from dying before main thread processes it
    auto* raw = new std::shared_ptr<WorkItem>(item);

    {
        // Re-check under the submit lock: BeginShutdown sets the flag while
        // holding it, so either our PostMessage lands before its drain runs or
        // we see the flag here and never post at all.
        std::lock_guard<std::mutex> submit(s_submit_mutex_);
        if (s_shutting_down_.load(std::memory_order_acquire)) {
            delete raw;
            throw std::runtime_error(kShutdownError);
        }
        if (!PostMessage(hwnd_, WM_MCP_EXECUTE, s_execute_cookie_, reinterpret_cast<LPARAM>(raw))) {
            delete raw;
            throw std::runtime_error("Failed to post work to main thread");
        }
    }

    // Wait for main thread to complete the work
    std::unique_lock<std::mutex> lock(item->mutex);
    bool finished = item->cv.wait_for(lock,
        std::chrono::milliseconds(timeout_ms),
        [&] { return item->completed; });

    if (!finished) {
        // Still queued: prevent late execution of callbacks that capture caller
        // stack references. Running work holds this mutex until it completes.
        item->completed = true;
        item->work = {};
        throw std::runtime_error("Main thread execution timed out");
    }

    if (item->error) {
        throw std::runtime_error(item->error_message);
    }

    return item->result;
}

LRESULT CALLBACK MainThreadExecutor::WndProc(
    HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {

    // WM_MCP_EXECUTE + 1 with small wParam commands: macroscript actions.
    if (msg == WM_MCP_EXECUTE + 1) {
        if (wp == 2) {
            extern void ClaimNativeInstance();
            ClaimNativeInstance();
        }
        return 0;
    }

    if (msg == WM_MCP_EXECUTE) {
        // Reject any sender that doesn't know our per-process cookie. lParam
        // is reinterpret_cast'd as a heap pointer; an attacker-supplied value
        // would be an arbitrary read/write/free + vtable-call primitive.
        if (wp != s_execute_cookie_) return 0;

        auto* raw = reinterpret_cast<std::shared_ptr<WorkItem>*>(lp);
        auto item = *raw;
        delete raw;

        // Raced the shutdown drain (or arrived from a nested pump during it).
        // Never start new scene work while the bridge is being torn down.
        if (s_shutting_down_.load(std::memory_order_acquire)) {
            FailWorkItem(item, kShutdownError);
            return 0;
        }

        // Delivered by a nested message pump while another item is running
        // (SDK work can pump: progress UI, redraws, deferred plugin loads).
        // Running it here would interleave theHold transactions on the global
        // undo system. Defer; the outer invocation drains after its item.
        if (s_executing_) {
            s_deferred_.push_back(std::move(item));
            return 0;
        }

        s_executing_ = true;
        RunWorkItem(item);
        while (!s_deferred_.empty()) {
            auto next = std::move(s_deferred_.front());
            s_deferred_.pop_front();
            RunWorkItem(next);
        }
        s_executing_ = false;
        return 0;
    }
    return DefWindowProc(hwnd, msg, wp, lp);
}

void MainThreadExecutor::RunWorkItem(const std::shared_ptr<WorkItem>& item) {
    {
        std::lock_guard<std::mutex> lock(item->mutex);
        if (item->completed) return; // timed out (or failed) before it started
        try {
            item->result = item->work();
        } catch (const std::exception& e) {
            item->error = true;
            item->error_message = e.what();
        } catch (...) {
            item->error = true;
            item->error_message = "Unknown exception on main thread";
        }
        item->completed = true;
    }
    item->cv.notify_all();
}

void MainThreadExecutor::FailWorkItem(const std::shared_ptr<WorkItem>& item,
                                      const char* message) {
    {
        std::lock_guard<std::mutex> lock(item->mutex);
        if (item->completed) return; // already ran, or the caller timed out
        item->error = true;
        item->error_message = message;
        item->work = {};
        item->completed = true;
    }
    item->cv.notify_all();
}
