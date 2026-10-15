#pragma once
// Shell side of the core's lifecycle, the parts that need neither Max nor a
// DLL: the gate every shell -> core call passes, the stamps a core must match
// before it is bound, and a reload's drain-and-swap sequence. Header-only so
// native/tests/core_reload_tests.cpp drives it with fake cores.
#include "mcp_bridge/core_api.h"

#include <chrono>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <string>
#include <vector>

struct McpHostApi;

// Admits calls into the bound core, counts them, and closes for a swap. Its
// mutex guards only the counter and the binding and is never held across a
// core call, so concurrent calls cannot deadlock on it. Only the reloading
// pipe worker waits on it; Max's main thread never does, because a call in
// flight may itself be waiting for the main thread.
class McpCoreGate {
public:
    enum class Admit { Entered, Busy, Unloaded };

    // One shell -> core call (dispatch, releaseSession), counted while it lives.
    class Admission {
    public:
        explicit Admission(McpCoreGate& gate) : gate_(gate) { status_ = gate_.Enter(api_, inFlight_); }
        ~Admission() {
            if (status_ == Admit::Entered) gate_.Leave();
        }
        Admission(const Admission&) = delete;
        Admission& operator=(const Admission&) = delete;

        Admit Status() const { return status_; }
        const McpCoreApi* Api() const { return api_; }
        // Busy: the calls in flight when this one was refused.
        int InFlight() const { return inFlight_; }

    private:
        McpCoreGate& gate_;
        Admit status_ = Admit::Unloaded;
        const McpCoreApi* api_ = nullptr;
        int inFlight_ = 0;
    };

    const McpCoreApi* Bound() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return core_;
    }
    int InFlight() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return inFlight_;
    }
    bool Swapping() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return swapping_;
    }

    // A reload's pipe worker. Refuses new calls (Busy), then waits up to `wait`
    // for the calls in flight. True: none is left and the gate stays closed
    // until Open(). False: calls remained after `wait`, or another swap holds
    // the gate; the gate is as before. `inFlight` is the count at the end.
    bool Close(std::chrono::milliseconds wait, int& inFlight, std::chrono::milliseconds& waited) {
        const auto start = std::chrono::steady_clock::now();
        std::unique_lock<std::mutex> lock(mutex_);
        if (swapping_) {
            inFlight = inFlight_;
            waited = std::chrono::milliseconds(0);
            return false;
        }
        swapping_ = true;
        const bool drained = drained_.wait_for(lock, wait, [this] { return inFlight_ == 0; });
        inFlight = inFlight_;
        waited = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - start);
        if (!drained) swapping_ = false;
        return drained;
    }

    void Open() {
        std::lock_guard<std::mutex> lock(mutex_);
        swapping_ = false;
    }

    // Binds `next` and returns the core it replaces. Callers hold the gate
    // closed, or run at GUP Start/Stop while no pipe serves requests.
    const McpCoreApi* Swap(const McpCoreApi* next) {
        std::lock_guard<std::mutex> lock(mutex_);
        const McpCoreApi* previous = core_;
        core_ = next;
        return previous;
    }

private:
    Admit Enter(const McpCoreApi*& api, int& inFlight) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (swapping_) {
            inFlight = inFlight_;
            return Admit::Busy;
        }
        if (!core_) return Admit::Unloaded;
        ++inFlight_;
        api = core_;
        return Admit::Entered;
    }

    void Leave() {
        bool idle = false;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            idle = --inFlight_ == 0;
        }
        if (idle) drained_.notify_all();
    }

    mutable std::mutex mutex_;
    std::condition_variable drained_;
    const McpCoreApi* core_ = nullptr;
    int inFlight_ = 0;
    bool swapping_ = false;
};

// Pipe sessions that closed while a reload held the gate. Their core-side
// state (scene_delta's snapshot fallback) is released when the reload settles,
// in the core that saw them if it is still bound; a core that replaced it
// never saw them.
class McpDeferredReleases {
public:
    // A pipe worker whose client disconnected: releases `session` in the
    // bound core now, or records it while a reload holds the gate.
    void Release(McpCoreGate& gate, const std::string& session) {
        for (;;) {
            {
                McpCoreGate::Admission admission(gate);
                if (admission.Status() == McpCoreGate::Admit::Entered) {
                    admission.Api()->releaseSession(session.c_str());
                    return;
                }
                if (admission.Status() == McpCoreGate::Admit::Unloaded) return;
            }
            // Busy. The reload reopens the gate before it takes this lock in
            // Settle, so a gate still closed here means Settle will see the
            // entry; an open one means it settled already: retry.
            std::lock_guard<std::mutex> lock(mutex_);
            if (gate.Swapping()) {
                sessions_.push_back(session);
                return;
            }
        }
    }

    // The reload's pipe worker, after the gate reopened: releases the
    // recorded sessions in `core` while it is still the bound core, or drops
    // them (null `core`: the swap replaced or unbound it).
    void Settle(McpCoreGate& gate, const McpCoreApi* core) {
        std::vector<std::string> sessions;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            sessions.swap(sessions_);
        }
        if (!core) return;
        for (const auto& session : sessions) {
            McpCoreGate::Admission admission(gate);
            if (admission.Status() == McpCoreGate::Admit::Entered && admission.Api() == core)
                admission.Api()->releaseSession(session.c_str());
        }
    }

    void Clear() {
        std::lock_guard<std::mutex> lock(mutex_);
        sessions_.clear();
    }

    size_t Pending() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return sessions_.size();
    }

private:
    mutable std::mutex mutex_;
    std::vector<std::string> sessions_;
};

// ── Version gate ─────────────────────────────────────────────────
// What this shell requires of a core.
struct McpCoreExpectation {
    int coreApi;
    int hostApi;
    int maxSdk;
    int msvc;
    int iteratorDebugLevel;
};

// Empty when the shell may bind `api`; otherwise why not, listing each value
// with the shell's own. Reads only the frozen prefix unless the version matches.
inline std::string McpCoreMismatch(const McpCoreApi& api, const McpCoreExpectation& expected) {
    const bool sameVersion = api.version == expected.coreApi;
    if (sameVersion && api.hostApiVersion == expected.hostApi && api.maxSdkVersion == expected.maxSdk &&
        api.msvcVersion == expected.msvc && api.iteratorDebugLevel == expected.iteratorDebugLevel)
        return {};
    const auto value = [](const char* name, const std::string& core, int bridge) {
        return std::string(name) + " " + core + " (bridge " + std::to_string(bridge) + ")";
    };
    const auto number = [](int v) { return std::to_string(v); };
    return "the native core does not match this bridge: " +
           value("core API", number(api.version), expected.coreApi) + ", " +
           value("host API", sameVersion ? number(api.hostApiVersion) : "unknown", expected.hostApi) + ", " +
           value("3ds Max SDK", number(api.maxSdkVersion), expected.maxSdk) + ", " +
           value("MSVC", number(api.msvcVersion), expected.msvc) + ", " +
           value("iterator debug level", sameVersion ? number(api.iteratorDebugLevel) : "unknown",
                 expected.iteratorDebugLevel);
}

// Every entry point present. Only meaningful once McpCoreMismatch passed.
inline bool McpCoreTableComplete(const McpCoreApi& api) {
    return api.initialize && api.shutdown && api.dispatch && api.releaseSession;
}

// ── Drain and swap ───────────────────────────────────────────────
struct McpSwapResult {
    enum class Status {
        Swapped,            // `next` is bound; the previous core is shut down and retired
        Busy,               // calls did not drain in time; nothing changed
        NotRun,             // the main-thread step never started; nothing changed
        Refused,            // the bound core declined to shut down now; nothing changed
        InitializeFailed,   // `next` refused to initialize; see previousRestored
    };
    Status status = Status::Busy;
    int inFlight = 0;            // Busy: calls still in flight when the wait ended
    long long drainMs = 0;       // time spent waiting for the drain
    std::string error;           // NotRun / Refused / InitializeFailed: why
    // InitializeFailed: the previous core was shut down (which stopped its
    // watch_scene and released its caches) before `next` refused...
    bool previousShutDown = false;
    // ...and was then initialized and bound again.
    bool previousRestored = false;
};

struct McpSwapSteps {
    // Runs `step` on Max's main thread and waits for it to finish. If it has
    // not started within `timeout`, it is cancelled and never runs; this
    // returns or throws either way.
    std::function<void(const std::function<void()>& step, std::chrono::milliseconds timeout)> runOnMain;
    // Main thread, inside the step, right after `next` was bound: record it
    // and free `previous` (null when no core was bound).
    std::function<void(const McpCoreApi* previous)> swapped;
    // Main thread, inside the step, when neither `next` nor `previous` would
    // initialize: record that no core is bound and free `previous`.
    std::function<void(const McpCoreApi* previous)> unbound;
};

// A reload's pipe worker, with `next` loaded and past McpCoreMismatch but not
// initialized. Closes the gate and waits up to `wait` for the calls in flight,
// then on Max's main thread: shuts the bound core down, initializes `next`
// and binds it. When the bound core declines to shut down, stops there; when
// `next` will not initialize, re-initializes the previous core so it keeps
// serving. Opens the gate before returning. Unless Swapped, the caller still
// owns `next` and frees it.
inline McpSwapResult McpSwapCore(McpCoreGate& gate, const McpCoreApi* next, const McpHostApi* host,
                                 std::chrono::milliseconds wait, std::chrono::milliseconds mainThreadMargin,
                                 const McpSwapSteps& steps) {
    McpSwapResult result;
    std::chrono::milliseconds waited(0);
    const bool drained = gate.Close(wait, result.inFlight, waited);
    result.drainMs = waited.count();
    if (!drained) return result;

    // Shell code run straight through the executor: not a core call, so the
    // gate neither counts nor refuses it.
    bool ran = false;
    const std::function<void()> step = [&] {
        ran = true;
        result.status = McpSwapResult::Status::InitializeFailed;
        result.error = "the core swap stopped on 3ds Max's main thread";
        const McpCoreApi* previous = gate.Bound();
        if (previous) {
            std::string refusal;
            if (previous->shutdown(McpStringSink(refusal)) != 0) {
                result.status = McpSwapResult::Status::Refused;
                result.error = refusal.empty() ? "the native core declined to shut down" : refusal;
                return;
            }
            result.previousShutDown = true;
        }
        std::string reason;
        if (next->initialize(host, McpStringSink(reason)) == 0) {
            gate.Swap(next);
            result.status = McpSwapResult::Status::Swapped;
            result.error.clear();
            steps.swapped(previous);
            return;
        }
        result.error = reason.empty() ? "the native core failed to initialize" : reason;
        if (!previous) return;
        std::string again;
        result.previousRestored = previous->initialize(host, McpStringSink(again)) == 0;
        if (result.previousRestored) return;
        gate.Swap(nullptr);
        result.error += "; the previous core would not initialize again either";
        if (!again.empty()) result.error += ": " + again;
        steps.unbound(previous);
    };
    const auto remaining = wait > waited ? wait - waited : std::chrono::milliseconds(0);
    std::string failure;
    try {
        steps.runOnMain(step, remaining + mainThreadMargin);
    } catch (const std::exception& e) {
        failure = e.what();
    } catch (...) {
        failure = "unknown exception";
    }
    if (!ran) {
        result.status = McpSwapResult::Status::NotRun;
        result.error = failure.empty() ? "the core swap did not run" : failure;
    }
    gate.Open();
    return result;
}
