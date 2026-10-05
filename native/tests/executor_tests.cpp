// SDK-independent MainThreadExecutor regressions. Ported from Geddart's fork
// (native/tests/transport_tests.cpp, commits 599e6f7 and 8e004f7); since the
// 1.7.5 merge the expired-work guard is upstream's started/cancelled mechanism.
//
//   cmake -S native/tests -B native/build-tests -G "Visual Studio 17 2022" -A x64
//   cmake --build native/build-tests --config Release
//   ctest --test-dir native/build-tests -C Release --output-on-failure
#include "mcp_bridge/main_thread_executor.h"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

using Clock = std::chrono::steady_clock;

long long ms_since(Clock::time_point start) {
    return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start).count();
}

// Mirrors MainThreadExecutor::WM_MCP_EXECUTE (private).
constexpr UINT kExecuteMessage = WM_USER + 0x4D43;

bool execute_message_queued() {
    MSG message;
    return PeekMessage(&message, nullptr, kExecuteMessage, kExecuteMessage, PM_NOREMOVE) != FALSE;
}

// Spin (bounded) until a worker's ExecuteSync has really queued its work item.
bool wait_for_queued_message(int timeout_ms) {
    const auto start = Clock::now();
    while (ms_since(start) < timeout_ms) {
        if (execute_message_queued()) return true;
        Sleep(1);
    }
    return false;
}

void ClaimNativeInstance() {}
void require(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }

bool contains_nocase(std::string text, std::string needle) {
    auto lower = [](std::string& s) {
        std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return static_cast<char>(tolower(c)); });
    };
    lower(text);
    lower(needle);
    return text.find(needle) != std::string::npos;
}

void pump_until(const std::atomic<bool>& done, int timeout_ms) {
    const auto start = Clock::now();
    MSG message;
    while (!done.load() && ms_since(start) < timeout_ms) {
        while (PeekMessage(&message, nullptr, 0, 0, PM_REMOVE)) DispatchMessage(&message);
        Sleep(1);
    }
}

// A callback that timed out while queued must never run later: it captures the
// caller's stack by reference, and that frame is gone once ExecuteSync threw.
void expired_work_does_not_execute() {
    MainThreadExecutor executor;
    executor.Initialize();
    std::atomic<bool> ran{false}, expired{false};
    std::string error;
    std::thread worker([&] {
        try { executor.ExecuteSync([&] { ran = true; return std::string("late"); }, 10); }
        catch (const std::runtime_error& e) { expired = true; error = e.what(); }
    });
    worker.join(); // deliberately do not pump the queued callback before timeout
    MSG message;
    while (PeekMessage(&message, nullptr, 0, 0, PM_REMOVE)) DispatchMessage(&message);
    require(expired && !ran, "expired callback executed");
    // The Python client recognises this text (_QUEUE_TIMEOUT_MARKER) as "never ran".
    require(contains_nocase(error, "main thread execution timed out"), "queue timeout lost the client's marker");
    require(contains_nocase(error, "before starting"), "queue timeout does not say the work never started");
    executor.Shutdown();
}

// Work that already started is not interruptible: a caller whose timeout
// expires keeps waiting, so its captured references stay alive, and it gets
// the real result (upstream 1.7.5).
void running_work_outlives_caller_timeout() {
    MainThreadExecutor executor;
    executor.Initialize();
    std::atomic<bool> finished_work{false}, done{false};
    std::string result, error;
    std::thread worker([&] {
        try {
            result = executor.ExecuteSync([&] {
                Sleep(300);
                finished_work = true;
                return std::string("slow");
            }, 50);
        } catch (const std::exception& e) { error = e.what(); }
        require(finished_work.load(), "caller returned before its running work finished");
        done = true;
    });
    pump_until(done, 5000);
    worker.join();
    require(error.empty(), "running work past the caller's timeout reported an error");
    require(result == "slow", "running work past the caller's timeout lost its result");
    executor.Shutdown();
}

// BeginShutdown from inside a running item (Max's exit can begin from a
// nested message loop): the running item still completes, and the item it
// deferred is failed without running.
void begin_shutdown_inside_running_item() {
    MainThreadExecutor executor;
    executor.Initialize();
    std::atomic<bool> b_posted{false}, b_ran{false}, a_done{false}, b_done{false};
    std::string a_result, b_error;
    std::thread worker_b;
    std::thread worker_a([&] {
        a_result = executor.ExecuteSync([&] {
            worker_b = std::thread([&] {
                try { executor.ExecuteSync([&] { b_ran = true; return std::string("b"); }, 20000); }
                catch (const std::exception& e) { b_error = e.what(); }
                b_done = true;
            });
            require(wait_for_queued_message(5000), "second item never reached the queue");
            MSG message;  // nested pump: B is delivered while A runs, so it is deferred
            while (PeekMessage(&message, nullptr, 0, 0, PM_REMOVE)) DispatchMessage(&message);
            executor.BeginShutdown();
            return std::string("ok");
        }, 20000);
        a_done = true;
    });
    pump_until(a_done, 10000);
    worker_a.join();
    if (worker_b.joinable()) worker_b.join();
    require(a_result == "ok", "running item did not complete after BeginShutdown");
    require(b_done && !b_ran, "deferred item ran during shutdown");
    require(b_error == "MainThreadExecutor is shutting down", "deferred item not failed by the drain");
    executor.Shutdown();
}

// Direct mode (read-only handlers on the pipe thread) runs through upstream's
// DialogWatch::Guard: without dialog events it returns the value and rethrows
// the work's own exception unchanged.
void direct_mode_passes_through_dialog_guard() {
    MainThreadExecutor executor;
    executor.Initialize();
    std::string result, error;
    std::thread worker([&] {
        MainThreadExecutor::EnableDirectMode();
        result = executor.ExecuteSync([] { return std::string("direct"); }, 50);
        try { executor.ExecuteSync([]() -> std::string { throw std::runtime_error("boom"); }, 50); }
        catch (const std::exception& e) { error = e.what(); }
        MainThreadExecutor::DisableDirectMode();
    });
    worker.join();  // deliberately never pumps: direct work must not need the main thread
    require(result == "direct", "direct-mode work did not return its value");
    require(error == "boom", "direct-mode exception was not passed through unchanged");
    executor.Shutdown();
}

// Max used to hang on exit: a client thread inside ExecuteSync waits for a
// WM_MCP_EXECUTE the main thread can no longer pump (it is joining that very
// thread). BeginShutdown must fail queued items immediately.
void shutdown_wakes_queued_waiter() {
    MainThreadExecutor executor;
    executor.Initialize();
    std::atomic<bool> ran{false}, errored{false};
    std::thread worker([&] {
        try { executor.ExecuteSync([&] { ran = true; return std::string("late"); }, 20000); }
        catch (const std::runtime_error&) { errored = true; }
    });
    require(wait_for_queued_message(5000), "work item never reached the queue");

    const auto start = Clock::now();
    executor.BeginShutdown(); // deliberately never pump the message
    worker.join();
    const long long elapsed = ms_since(start);

    require(errored, "queued waiter did not fail on shutdown");
    require(!ran, "work executed during shutdown");
    require(elapsed < 2000, "queued waiter woke only after its timeout expired");
    require(!execute_message_queued(), "shutdown left work queued");
    executor.Shutdown();
}

// Once shutting down, a background ExecuteSync must throw at once instead of
// posting work nobody will ever pump.
void execute_after_shutdown_fails_fast() {
    MainThreadExecutor executor;
    executor.Initialize();
    executor.BeginShutdown();
    require(MainThreadExecutor::IsShuttingDown(), "shutdown flag not set");

    std::atomic<bool> ran{false}, errored{false};
    const auto start = Clock::now();
    std::thread worker([&] {
        try { executor.ExecuteSync([&] { ran = true; return std::string("nope"); }, 20000); }
        catch (const std::runtime_error&) { errored = true; }
    });
    worker.join();
    const long long elapsed = ms_since(start);

    require(errored, "ExecuteSync after shutdown did not fail");
    require(!ran, "work executed after shutdown");
    require(elapsed < 2000, "ExecuteSync after shutdown blocked instead of failing fast");
    require(!execute_message_queued(), "failed submission still posted work");
    executor.Shutdown();
}

// The gate is process-wide, so a fresh Initialize() has to reopen it; otherwise
// one Stop/Start cycle would leave the bridge permanently dead.
void initialize_reopens_after_shutdown() {
    MainThreadExecutor executor;
    executor.Initialize();
    require(!MainThreadExecutor::IsShuttingDown(), "initialize left the gate closed");

    std::atomic<bool> done{false};
    std::string result;
    std::thread worker([&] {
        result = executor.ExecuteSync([] { return std::string("ok"); }, 20000);
        done = true;
    });
    const auto start = Clock::now();
    MSG message;
    while (!done.load() && ms_since(start) < 5000) {
        while (PeekMessage(&message, nullptr, 0, 0, PM_REMOVE)) DispatchMessage(&message);
        Sleep(1);
    }
    worker.join();
    require(done && result == "ok", "executor did not run work after re-initialize");
    executor.Shutdown();
}

// Quiet mode and the MAXScript compile check key on IsMainThread(), not on
// direct mode: true for queued work and for direct mode switched on on the
// main thread (a nested Dispatch), false on a pipe thread in either mode.
void is_main_thread_ignores_direct_mode() {
    MainThreadExecutor executor;
    executor.Initialize();
    require(MainThreadExecutor::IsMainThread(), "the initializing thread is not the main thread");
    MainThreadExecutor::EnableDirectMode();
    require(MainThreadExecutor::IsMainThread(), "direct mode on the main thread hid the main thread");
    MainThreadExecutor::DisableDirectMode();

    std::atomic<bool> done{false};
    bool worker_plain = true, worker_direct = true, queued_on_main = false, direct_inline = true;
    std::thread worker([&] {
        worker_plain = MainThreadExecutor::IsMainThread();
        executor.ExecuteSync([&] { queued_on_main = MainThreadExecutor::IsMainThread(); return std::string(); }, 20000);
        MainThreadExecutor::EnableDirectMode();
        worker_direct = MainThreadExecutor::IsMainThread();
        executor.ExecuteSync([&] { direct_inline = MainThreadExecutor::IsMainThread(); return std::string(); }, 50);
        MainThreadExecutor::DisableDirectMode();
        done = true;
    });
    pump_until(done, 5000);
    worker.join();
    require(done, "worker did not finish");
    require(!worker_plain && !worker_direct, "a pipe thread reported itself as the main thread");
    require(queued_on_main, "queued work did not run on the main thread");
    require(!direct_inline, "direct-mode work on a pipe thread reported the main thread");
    executor.Shutdown();
}

// Healthy path still works: queued work runs and returns its result.
void queued_work_runs() {
    MainThreadExecutor executor;
    executor.Initialize();
    std::atomic<bool> done{false};
    std::string result;
    std::thread worker([&] {
        result = executor.ExecuteSync([] { return std::string("pong"); }, 20000);
        done = true;
    });
    const auto start = Clock::now();
    MSG message;
    while (!done.load() && ms_since(start) < 5000) {
        while (PeekMessage(&message, nullptr, 0, 0, PM_REMOVE)) DispatchMessage(&message);
        Sleep(1);
    }
    worker.join();
    require(done && result == "pong", "queued work did not run");
    executor.Shutdown();
}

int main() {
    try {
        queued_work_runs();
        expired_work_does_not_execute();
        running_work_outlives_caller_timeout();
        shutdown_wakes_queued_waiter();
        execute_after_shutdown_fails_fast();
        initialize_reopens_after_shutdown();
        begin_shutdown_inside_running_item();
        initialize_reopens_after_shutdown();
        direct_mode_passes_through_dialog_guard();
        is_main_thread_ignores_direct_mode();
        std::cout << "PASS: queued work runs; expired work skipped; running work outlives the caller's "
                     "timeout; shutdown wakes queued waiter; execute-after-shutdown fails fast; initialize "
                     "reopens the gate; shutdown inside a running item; direct mode passes the dialog guard; "
                     "IsMainThread ignores direct mode\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
