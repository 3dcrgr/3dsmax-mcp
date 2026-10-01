// SDK-independent MainThreadExecutor regressions. Ported from Geddart's fork
// (native/tests/transport_tests.cpp, commits 599e6f7 and 8e004f7).
//
//   cmake -S native/tests -B native/build-tests -G "Visual Studio 17 2022" -A x64
//   cmake --build native/build-tests --config Release
//   ctest --test-dir native/build-tests -C Release --output-on-failure
#include "mcp_bridge/main_thread_executor.h"
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

// A callback that timed out while queued must never run later: it captures the
// caller's stack by reference, and that frame is gone once ExecuteSync threw.
void expired_work_does_not_execute() {
    MainThreadExecutor executor;
    executor.Initialize();
    std::atomic<bool> ran{false}, expired{false};
    std::thread worker([&] {
        try { executor.ExecuteSync([&] { ran = true; return std::string("late"); }, 10); }
        catch (const std::runtime_error&) { expired = true; }
    });
    worker.join(); // deliberately do not pump the queued callback before timeout
    MSG message;
    while (PeekMessage(&message, nullptr, 0, 0, PM_REMOVE)) DispatchMessage(&message);
    require(expired && !ran, "expired callback executed");
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
        shutdown_wakes_queued_waiter();
        execute_after_shutdown_fails_fast();
        initialize_reopens_after_shutdown();
        std::cout << "PASS: queued work runs; expired work skipped; shutdown wakes queued waiter; "
                     "execute-after-shutdown fails fast; initialize reopens the gate\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
