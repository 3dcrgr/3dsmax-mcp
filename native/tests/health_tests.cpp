// SDK-independent tests for the pipe-thread "health" bookkeeping:
// MainThreadExecutor::GetHealth() and the BridgeHealth client registry.
//
//   cmake -S native/tests -B native/build-tests -G "Visual Studio 17 2022" -A x64
//   cmake --build native/build-tests --config Release
//   ctest --test-dir native/build-tests -C Release --output-on-failure
#include "mcp_bridge/bridge_health.h"
#include "mcp_bridge/dialog_watch.h"
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
    pump_for(300);  // the one outstanding beat is answered as soon as it pumps again
    require(executor.GetHealth().heartbeat_age_ms < (long long)MainThreadExecutor::kHeartbeatMs,
            "heartbeat did not recover after pumping resumed");
    executor.Shutdown();
    require(!executor.GetHealth().initialized, "executor still initialized after shutdown");
    require(executor.GetHealth().heartbeat_age_ms < 0, "heartbeat still reported after shutdown");
}

// A main thread that keeps pumping under a steady stream of posted messages
// (progressive render, UI update loops) never lets WM_TIMER through; the
// heartbeat must still read fresh, since queued bridge work would still run.
std::atomic<bool> g_flood{false};
LRESULT CALLBACK FloodProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (msg == WM_APP) {
        if (g_flood.load()) PostMessage(hwnd, WM_APP, 0, 0);
        return 0;
    }
    return DefWindowProc(hwnd, msg, wp, lp);
}

void heartbeat_survives_a_posted_message_flood() {
    WNDCLASSEX wc = {};
    wc.cbSize = sizeof(wc);
    wc.lpfnWndProc = FloodProc;
    wc.hInstance = GetModuleHandle(nullptr);
    wc.lpszClassName = L"HealthTestFlood";
    RegisterClassEx(&wc);
    HWND flood = CreateWindowEx(0, L"HealthTestFlood", L"", 0, 0, 0, 0, 0, nullptr, nullptr,
                                GetModuleHandle(nullptr), nullptr);
    require(flood != nullptr, "flood window not created");

    MainThreadExecutor executor;
    executor.Initialize();
    g_flood = true;
    PostMessage(flood, WM_APP, 0, 0);
    const auto start = Clock::now();
    long long worst = 0;
    unsigned long long pumped = 0;
    MSG message;
    while (ms_since(start) < 3500) {
        if (PeekMessage(&message, nullptr, 0, 0, PM_REMOVE)) {
            DispatchMessage(&message);
            ++pumped;
        }
        if (ms_since(start) > 1500) worst = (std::max)(worst, executor.GetHealth().heartbeat_age_ms);
    }
    g_flood = false;
    pump_for(20);
    executor.Shutdown();
    DestroyWindow(flood);
    UnregisterClass(L"HealthTestFlood", GetModuleHandle(nullptr));
    require(pumped > 1000, "flood did not run");
    require(worst >= 0 && worst < 2 * MainThreadExecutor::kHeartbeatMs + 500,
            "heartbeat went stale while the main thread was pumping a message flood");
}

// Work posted by a client but not yet picked up is visible, labelled and aged.
void queued_work_is_visible_with_label() {
    MainThreadExecutor executor;
    executor.Initialize();
    const unsigned long long before = executor.GetHealth().completed;

    std::atomic<bool> done{false};
    std::thread worker([&] {
        MainThreadExecutor::SetThreadLabel({"native:test_queued", "pipe-7", "req-q"});
        executor.ExecuteSync([] { return std::string("ok"); }, 20000);
        done = true;
    });
    require(wait_until(5000, [&] { return executor.GetHealth().queued == 1; }),
            "posted work never showed as queued");
    Sleep(50);
    auto health = executor.GetHealth();
    require(health.oldest_queued_label.cmd_type == "native:test_queued", "queued work lost its label");
    require(health.oldest_queued_label.request_id == "req-q", "queued work lost its request id");
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

// While a callback runs, an observer thread sees it as running. GetHealth never
// takes an item mutex, so it answers whatever the item's waiter is doing.
void running_work_is_visible() {
    MainThreadExecutor executor;
    executor.Initialize();
    std::atomic<bool> entered{false}, release{false}, done{false};
    std::thread worker([&] {
        MainThreadExecutor::SetThreadLabel({"maxscript", "pipe-3", "req-run"});
        executor.ExecuteSync([&] {
            entered = true;
            while (!release.load()) Sleep(1);
            return std::string("ok");
        }, 20000);
        done = true;
    });

    std::atomic<bool> observed{false};
    std::atomic<long long> observed_ms{-1};
    MainThreadExecutor::WorkLabel observed_label;
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
    require(observed_label.cmd_type == "maxscript", "running work lost its label");
    // Owner attribution: the running item names its client and request, not
    // just its cmd type (another client's same-type request may be queued).
    require(observed_label.client_id == "pipe-3" && observed_label.request_id == "req-run",
            "running work lost its client/request id");
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

// A running item whose caller timed out is still running, not queued, and is
// counted as completed once it really finishes (upstream 1.7.5 waits for it).
void running_past_timeout_stays_running() {
    MainThreadExecutor executor;
    executor.Initialize();
    const unsigned long long before = executor.GetHealth().completed;
    std::atomic<bool> entered{false}, release{false}, done{false};
    std::string result;
    std::thread worker([&] {
        MainThreadExecutor::SetThreadLabel({"maxscript", "pipe-5", "req-slow"});
        result = executor.ExecuteSync([&] {
            entered = true;
            while (!release.load()) Sleep(1);
            return std::string("late");
        }, 50);
        done = true;
    });
    std::atomic<bool> observed{false};
    std::thread observer([&] {
        if (!wait_until(5000, [&] { return entered.load(); })) return;
        Sleep(200);  // well past the caller's 50 ms timeout
        auto health = executor.GetHealth();
        observed = health.running && health.queued == 0 && health.running_label.request_id == "req-slow";
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
    require(observed, "running item past its caller's timeout not reported as running");
    require(result == "late", "waiter did not get the result of its running item");
    require(executor.GetHealth().completed == before + 1, "completed counter not incremented");
    require(!executor.GetHealth().running, "finished item still reported as running");
    executor.Shutdown();
}

// The two request registries agree: DialogWatch (max_dialogs status) and the
// executor tickets (health) both see the same queued request.
void dialogwatch_and_health_agree() {
    MainThreadExecutor executor;
    executor.Initialize();
    std::atomic<bool> done{false};
    std::thread worker([&] {
        DialogWatch::RequestContext context("req-dw", "maxscript");
        MainThreadExecutor::SetThreadLabel({"maxscript", "pipe-9", "req-dw"});
        executor.ExecuteSync([] { return std::string("ok"); }, 20000);
        done = true;
    });
    require(wait_until(5000, [&] { return executor.GetHealth().queued == 1; }), "request never queued");
    std::string status;
    std::thread control([&] { status = DialogWatch::Control(R"({"action":"status"})"); });
    control.join();
    const auto parsed = nlohmann::json::parse(status);
    bool queued = false;
    for (const auto& request : parsed["requests"])
        queued = queued || (request.value("request_id", "") == "req-dw" && request.value("state", "") == "queued");
    require(queued, "max_dialogs status does not list the queued request");
    require(executor.GetHealth().oldest_queued_label.request_id == "req-dw", "health lost the request id");
    const auto start = Clock::now();
    MSG message;
    while (!done.load() && ms_since(start) < 5000) {
        while (PeekMessage(&message, nullptr, 0, 0, PM_REMOVE)) DispatchMessage(&message);
        Sleep(1);
    }
    worker.join();
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
            bool sawOuter = false, sawProbe = false;
            for (const auto& r : snap.inflight) {
                if (r.client_id == "pipe-test-a") {
                    sawOuter = r.request_id == "req-1" && r.cmd_type == "maxscript" &&
                               r.nested == 1 && r.elapsed_ms >= 15 && !r.internal;
                }
                if (r.client_id == "native-tool-probe") sawProbe = r.internal;
            }
            require(sawOuter, "outer request not reported with its nesting");
            require(sawProbe, "never-connected probe not marked internal");
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
        running_past_timeout_stays_running();
        expired_work_leaves_the_queue();
        dialogwatch_and_health_agree();
        heartbeat_tracks_pumping();
        heartbeat_survives_a_posted_message_flood();
        std::cout << "PASS: client registry; queued work visible with label; running work visible "
                     "without blocking; running past its timeout stays running; expired work leaves the "
                     "queue; dialog watch and health agree; heartbeat tracks pumping; heartbeat survives "
                     "a posted-message flood\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
