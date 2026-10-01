#pragma once
#include <windows.h>
#include <functional>
#include <memory>
#include <string>
#include <nlohmann/json.hpp>

// Blocking-dialog monitor and recovery. Reads Win32 dialogs through the window
// manager and Qt dialogs through an optional main-thread backend. No Max SDK,
// MAXScript or scene access.
namespace DialogWatch {
struct Session;

// Both run on Max's main thread, through the dialog lane.
struct QtBackend {
    std::function<nlohmann::json(HWND)> snapshot;  // null when hwnd is not Qt
    std::function<void(HWND, int)> click;
};

// Call on Max's main thread. Creates the main-thread dialog lane and starts the
// process-wide monitor, which tracks every blocking dialog, records it in
// history and closes Script Controller Exception boxes.
void Start(QtBackend qt = {});
void Stop();

// One bridge request, for the lifetime of its dispatch on a pipe worker.
struct RequestContext {
    RequestContext(const std::string& request, const std::string& command);
    ~RequestContext();
    RequestContext(const RequestContext&) = delete;
    RequestContext& operator=(const RequestContext&) = delete;
    std::string previous_request, previous_command;
    bool registered = false;
};
std::string RequestId();
std::string CommandType();
// The current request is waiting for Max's main thread.
void MarkQueued();

// Scope of one bridge operation on its executing thread. New recognized
// acknowledgment-only Max errors on that thread are acknowledged and fail the
// operation. Pre-existing dialogs and every other dialog are left alone.
class Guard {
public:
    Guard(const std::string& request, const std::string& command);
    ~Guard();
    Guard(const Guard&) = delete;
    Guard& operator=(const Guard&) = delete;
    std::string Error(const std::string& cause = "") const;
    std::string Execute(const std::function<std::string()>& work);
private:
    void Finish();
    std::shared_ptr<Session> session_;
    std::shared_ptr<Session> previous_;
};

// Call before committing a transaction, and after returning from MAXScript.
void ThrowIfDismissed();

// Open blocking dialogs from window-manager state only, for response metadata.
nlohmann::json OpenDialogs();

// Must be invoked on a pipe worker, through the independent control channel.
// Actions: status (no window messages), inspect, respond.
std::string Control(const std::string& command);
}
