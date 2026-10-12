#pragma once
#include <string>

// render_start's armed job and its Max notifications (NOTIFY_PRE_RENDER,
// NOTIFY_POST_RENDERFRAME, NOTIFY_POST_RENDER), owned by the shell. The core's
// render handlers reach it through McpHostApi.
namespace RenderHost {
// GUP Start/Stop.
void RegisterNotifications();
void UnregisterNotifications();

// True with the id while a job is armed.
bool ActiveJob(std::string& jobId);
// Arms the next render's done-signal.
void Arm(const std::string& jobId, const std::string& output, const std::string& signalPath,
         int framesExpected);
// render_cancel's locked section; returns kMcpRenderCancel* (host_api.h).
int Cancel(const std::string& expectedJobId, bool& hadActiveJob, std::string& jobId, int& framesDone,
           int& framesExpected);
}
