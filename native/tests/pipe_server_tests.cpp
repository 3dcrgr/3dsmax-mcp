// Exercise the real server with connected Windows pipes, without the Max SDK.
#include "mcp_bridge/pipe_server.h"
#include "mcp_bridge/bridge_health.h"
#include <iostream>
#include <stdexcept>
#include <chrono>

std::atomic<int> calls{0};
namespace CommandDispatcher {
std::string Dispatch(const std::string& request, MCPBridgeGUP*, const std::string&) {
    ++calls;
    if (request == "large") return std::string(2 * 1024 * 1024, 'x');
    if (request == "quit") return "MAXSCRIPT_INTERRUPTED";
    return request;
}
}
namespace NativeHandlers { void ReleaseSceneDeltaSession(const std::string&) {} }

void require(bool ok, const char* why) { if (!ok) throw std::runtime_error(why); }
template<class F> void await(F predicate) {
    for (int i = 0; i < 2000; ++i) {
        if (predicate()) return;
        Sleep(1);
    }
    throw std::runtime_error("condition timed out");
}
HANDLE connect(const std::wstring& name) {
    HANDLE client = INVALID_HANDLE_VALUE;
    await([&] {
        client = CreateFileW(name.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING, 0, nullptr);
        return client != INVALID_HANDLE_VALUE;
    });
    await([] { return BridgeHealth::Snapshot().connected > 0; });
    return client;
}
void send(HANDLE client, const std::string& request) {
    DWORD written = 0;
    require(WriteFile(client, request.data(), static_cast<DWORD>(request.size()), &written, nullptr) &&
            written == request.size(), "write failed");
}
std::string read(HANDLE client) {
    char buffer[128]; DWORD count = 0;
    require(ReadFile(client, buffer, sizeof(buffer), &count, nullptr) != FALSE, "read failed");
    return std::string(buffer, count);
}
void stop(PipeServer& server) {
    auto start = std::chrono::steady_clock::now();
    server.Stop();
    require(std::chrono::steady_clock::now() - start < std::chrono::seconds(2), "Stop exceeded 2 seconds");
    require(BridgeHealth::Snapshot().connected == 0, "client bookkeeping leaked");
    server.Stop(); // idempotent
}
int main() {
    try {
        auto name = L"\\\\.\\pipe\\mcp-shutdown-test-" + std::to_wstring(GetCurrentProcessId());
        {
            PipeServer server(nullptr, name); server.Start(); stop(server); // pending accept
        }
        for (int iteration = 0; iteration < 10; ++iteration) {
            PipeServer server(nullptr, name); server.Start();
            HANDLE client = connect(name);
            send(client, "hello\n"); require(read(client) == "hello\n", "normal round trip failed");
            stop(server); // idle client still open, waiting in ReadRequest
            CloseHandle(client);
        }
        {
            PipeServer server(nullptr, name); server.Start(); HANDLE client = connect(name);
            int before = calls.load(); send(client, "partial request without delimiter"); Sleep(25);
            stop(server); require(calls.load() == before, "partial request was dispatched"); CloseHandle(client);
        }
        {
            PipeServer server(nullptr, name); server.Start(); HANDLE client = connect(name);
            send(client, "large\n");
            await([&] { DWORD available = 0; return PeekNamedPipe(client, nullptr, 0, nullptr, &available, nullptr) && available > 0; });
            stop(server); // client never reads: WriteResponse must cancel too
            CloseHandle(client);
        }
        {
            PipeServer server(nullptr, name); server.Start(); HANDLE client = connect(name);
            send(client, "quit\n"); require(read(client) == "MAXSCRIPT_INTERRUPTED\n", "interruption reply missing");
            stop(server); // shutdown after the quit reply, with the connection retained
            CloseHandle(client);
        }
        std::cout << "pipe shutdown regressions passed\n";
        return 0;
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
