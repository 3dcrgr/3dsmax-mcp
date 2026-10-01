#pragma once
#include <string>
#include <vector>

// Registry of connected pipe clients and the request each one is running.
//
// Pipe threads update it around every request; the "health" command reads it
// on a pipe thread. Nothing here touches the Max main thread or the SDK, so a
// health reply still arrives while Max is hung — which is exactly when an
// agent needs to know whether its own request, another MCP client's request,
// or something outside the bridge is holding the main thread.
namespace BridgeHealth {

struct RequestInfo {
    std::string client_id;
    std::string request_id;
    std::string cmd_type;
    long long elapsed_ms = 0;
    int nested = 0;  // requests this one dispatched in turn (e.g. invoke_tool probes)
};

struct ClientsSnapshot {
    int connected = 0;
    unsigned long long total_connections = 0;
    std::vector<RequestInfo> inflight;  // outermost request per client (pipe requests are sequential)
};

void ClientConnected(const std::string& client_id);
void ClientDisconnected(const std::string& client_id);
void RequestStarted(const std::string& client_id, const std::string& request_id,
                    const std::string& cmd_type);
void RequestFinished(const std::string& client_id);
ClientsSnapshot Snapshot();

// Marks one client's request as in flight for the lifetime of the scope.
class RequestScope {
public:
    RequestScope(const std::string& client_id, const std::string& request_id,
                 const std::string& cmd_type)
        : client_id_(client_id) {
        RequestStarted(client_id_, request_id, cmd_type);
    }
    ~RequestScope() { RequestFinished(client_id_); }
    RequestScope(const RequestScope&) = delete;
    RequestScope& operator=(const RequestScope&) = delete;

private:
    std::string client_id_;
};

}  // namespace BridgeHealth
