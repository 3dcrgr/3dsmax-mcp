// SDK-independent tests for the pipe-thread "health" bookkeeping:
// MainThreadExecutor::GetHealth() and the BridgeHealth client registry.
//
//   cmake -S native/tests -B native/build-tests -G "Visual Studio 17 2022" -A x64
//   cmake --build native/build-tests --config Release
//   ctest --test-dir native/build-tests -C Release --output-on-failure
#include "mcp_bridge/bridge_health.h"
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

void ClaimNativeInstance() {}
void require(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }

void pump_for(int ms) {
    const auto start = Clock::now();
    MSG message;
    while (ms_since(start) < ms) {
        while (PeekMessage(&message, nullptr, 0, 0, PM_REMOVE)) DispatchMessage(&message);
        Sleep(5);
    }
}

// Spin (bounded) until the predicate holds; GetHealth is called from this thread.
template <typename Pred>
bool wait_until(int timeout_ms, Pred pred) {
    const auto start = Clock::now();
    while (ms_since(start) < timeout_ms) {
        if (pred()) return true;
        Sleep(2);
    }
    return pred();
}

// The heartbeat is fresh while the main thread pumps and goes stale when it
// stops: that is the "main thread is stuck outside the bridge" signal.
void heartbeat_tracks_pumping() {
    MainThreadExecutor executor;
    executor.Initialize();
    pump_for(MainThreadExecutor::kHeartbeatMs + 300);
    auto health = executor.GetHealth();
    require(health.initialized, "executor not reported initialized");
    require(health.heartbeat_age_ms >= 0, "no heartbeat after pumping");
    require(health.heartbeat_age_ms < 1500, "heartbeat stale while pumping");

    Sleep(3 * MainThreadExecutor::kHeartbeatMs + 300);  // deliberately do not pump
    health = executor.GetHealth();
    require(health.heartbeat_age_ms > 3 * MainThreadExecutor::kHeartbeatMs,
            "heartbeat stayed fresh without pumping");
    executor.Shutdown();
    require(!executor.GetHealth().initialized, "executor still initialized after shutdown");
}

// Work posted by a client but not yet picked up is visible, labelled and aged.
void queued_work_is_visible_with_label() {
    MainThreadExecutor executor;
    executor.Initialize();
    const unsigned long long before = executor.GetHealth().completed;

    std::atomic<bool> done{false};
    std::thread worker([&] {
        MainThreadExecutor::SetThreadLabel("native:test_queued");
        executor.ExecuteSync([] { return std::string("ok"); }, 20000);
        done = true;
    });
    require(wait_until(5000, [&] { return executor.GetHealth().queued == 1; }),
            "posted work never showed as queued");
    Sleep(50);
    auto health = executor.GetHealth();
    require(health.oldest_queued_label == "native:test_queued", "queued work lost its label");
    require(health.oldest_queued_ms >= 40, "queued age not tracked");
    require(!health.running, "queued work reported as running");

    const auto start = Clock::now();
    MSG message;
    while (!done.load() && ms_since(start) < 5000) {
        while (PeekMessage(&message, nullptr, 0, 0, PM_REMOVE)) DispatchMessage(&message);
        Sleep(1);
    }
    worker.join();
    health = executor.GetHealth();
    require(health.queued == 0, "finished work still counted as queued");
    require(health.completed == before + 1, "completed counter not incremented");
    executor.Shutdown();
}

// While a callback runs, an observer thread sees it as running (without ever
// blocking on the item's mutex, which the main thread holds throughout).
void running_work_is_visible() {
    MainThreadExecutor executor;
    executor.Initialize();
    std::atomic<bool> entered{false}, release{false}, done{false};
    std::thread worker([&] {
        MainThreadExecutor::SetThreadLabel("maxscript");
        executor.ExecuteSync([&] {
            entered = true;
            while (!release.load()) Sleep(1);
            return std::string("ok");
        }, 20000);
        done = true;
    });

    std::atomic<bool> observed{false};
    std::atomic<long long> observed_ms{-1};
    std::string observed_label;
    std::thread observer([&] {
        if (!wait_until(5000, [&] { return entered.load(); })) return;
        Sleep(60);
        const auto start = Clock::now();
        auto health = executor.GetHealth();
        // Must return promptly even though the running item's mutex is held.
        if (ms_since(start) < 500 && health.running) {
            observed_label = health.running_label;
            observed_ms = health.running_ms;
            observed = true;
        }
        release = true;
    });

    const auto start = Clock::now();
    MSG message;
    while (!done.load() && ms_since(start) < 10000) {
        while (PeekMessage(&message, nullptr, 0, 0, PM_REMOVE)) DispatchMessage(&message);
        Sleep(1);
    }
    release = true;
    observer.join();
    worker.join();
    require(observed, "running work not visible to an observer thread");
    require(observed_label == "maxscript", "running work lost its label");
    require(observed_ms >= 50, "running time not tracked");
    require(!executor.GetHealth().running, "finished work still reported as running");
    executor.Shutdown();
}

// A request that timed out in the queue never ran; it must not be reported as
// still queued (it will never run).
void expired_work_leaves_the_queue() {
    MainThreadExecutor executor;
    executor.Initialize();
    std::thread worker([&] {
        try { executor.ExecuteSync([] { return std::string("late"); }, 10); }
        catch (const std::runtime_error&) {}
    });
    worker.join();
    require(executor.GetHealth().queued == 0, "expired work still counted as queued");
    pump_for(20);
    executor.Shutdown();
}

void client_registry_tracks_connections_and_requests() {
    using namespace BridgeHealth;
    const auto base = Snapshot();
    ClientConnected("pipe-test-a");
    ClientConnected("pipe-test-b");
    auto snap = Snapshot();
    require(snap.connected == base.connected + 2, "connections not counted");
    require(snap.total_connections == base.total_connections + 2, "total connections not counted");
    require(snap.inflight.size() == base.inflight.size(), "idle clients reported in flight");

    {
        RequestScope outer("pipe-test-a", "req-1", "maxscript");
        {
            // A nested dispatch with a client id that never connected
            // (native_tool_registry uses "native-tool-probe").
            RequestScope probe("native-tool-probe", "req-2", "native:scene_info");
            RequestScope nested("pipe-test-a", "req-3", "native:inspect_object");
            Sleep(20);
            snap = Snapshot();
            bool sawOuter = false;
            for (const auto& r : snap.inflight) {
                if (r.client_id == "pipe-test-a") {
                    sawOuter = r.request_id == "req-1" && r.cmd_type == "maxscript" &&
                               r.nested == 1 && r.elapsed_ms >= 15;
                }
            }
            require(sawOuter, "outer request not reported with its nesting");
            require(snap.connected == base.connected + 2, "a nested probe counted as a connection");
        }
        snap = Snapshot();
        bool stillOuter = false;
        for (const auto& r : snap.inflight) {
            if (r.client_id == "pipe-test-a") stillOuter = r.request_id == "req-1" && r.nested == 0;
            require(r.client_id != "native-tool-probe", "finished probe still in flight");
        }
        require(stillOuter, "nested request end cleared the outer request");
    }
    snap = Snapshot();
    for (const auto& r : snap.inflight) {
        require(r.client_id != "pipe-test-a", "finished request still in flight");
    }

    // A disconnect mid-request (broken pipe) must not leave a ghost.
    RequestStarted("pipe-test-b", "req-4", "maxscript");
    ClientDisconnected("pipe-test-b");
    ClientDisconnected("pipe-test-a");
    snap = Snapshot();
    require(snap.connected == base.connected, "disconnects not counted");
    for (const auto& r : snap.inflight) {
        require(r.client_id != "pipe-test-b", "disconnected client still in flight");
    }
}

int main() {
    try {
        client_registry_tracks_connections_and_requests();
        queued_work_is_visible_with_label();
        running_work_is_visible();
        expired_work_leaves_the_queue();
        heartbeat_tracks_pumping();
        std::cout << "PASS: client registry; queued work visible with label; running work visible "
                     "without blocking; expired work leaves the queue; heartbeat tracks pumping\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
