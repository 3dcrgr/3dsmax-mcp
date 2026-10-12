#include "mcp_bridge/pipe_server.h"
#include "mcp_bridge/bridge_health.h"
#include "mcp_bridge/command_dispatcher.h"
#include "mcp_bridge/native_handlers.h"

PipeServer::PipeServer(MCPBridgeGUP* gup, std::wstring pipe_name)
    : gup_(gup), pipe_name_(std::move(pipe_name)) {
    shutdown_event_ = CreateEvent(nullptr, TRUE, FALSE, nullptr);
}

PipeServer::~PipeServer() {
    Stop();
    if (shutdown_event_) {
        CloseHandle(shutdown_event_);
        shutdown_event_ = nullptr;
    }
}

void PipeServer::Start() {
    if (running_.load()) return;
    running_ = true;
    ResetEvent(shutdown_event_);
    accept_thread_ = std::thread(&PipeServer::AcceptLoop, this);
}

void PipeServer::Stop() {
    if (!running_.load()) return;
    running_ = false;
    SetEvent(shutdown_event_);

    if (accept_thread_.joinable()) {
        accept_thread_.join();
    }

    // AcceptLoop has joined, so no more clients can be added. Client I/O
    // observes shutdown_event_ and cancels/drains its OVERLAPPED before exiting.
    for (auto& ct : client_threads_) {
        if (ct->thread.joinable()) {
            ct->thread.join();
        }
    }
    client_threads_.clear();
}

void PipeServer::CleanupFinishedThreads() {
    std::lock_guard<std::mutex> lock(threads_mutex_);
    auto it = client_threads_.begin();
    while (it != client_threads_.end()) {
        if ((*it)->done.load()) {
            if ((*it)->thread.joinable()) {
                (*it)->thread.join();
            }
            it = client_threads_.erase(it);
        } else {
            ++it;
        }
    }
}

void PipeServer::AcceptLoop() {
    while (running_.load()) {
        // Cleanup finished client threads periodically
        CleanupFinishedThreads();

        HANDLE pipe = CreateNamedPipeW(
            pipe_name_.c_str(),
            PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            PIPE_UNLIMITED_INSTANCES,
            BUFFER_SIZE,
            BUFFER_SIZE,
            0,              // default timeout (use system default, fastest)
            nullptr         // default security (local machine only)
        );

        if (pipe == INVALID_HANDLE_VALUE) {
            Sleep(100);
            continue;
        }

        // Wait for client with overlapped I/O so we can detect shutdown
        OVERLAPPED overlapped = {};
        overlapped.hEvent = CreateEvent(nullptr, TRUE, FALSE, nullptr);

        BOOL connected = ConnectNamedPipe(pipe, &overlapped);
        if (!connected) {
            DWORD err = GetLastError();
            if (err == ERROR_IO_PENDING) {
                HANDLE events[2] = { overlapped.hEvent, shutdown_event_ };
                DWORD wait = WaitForMultipleObjects(2, events, FALSE, INFINITE);

                if (wait == WAIT_OBJECT_0 + 1) {
                    // Shutdown
                    CancelIoEx(pipe, &overlapped);
                    DWORD ignored = 0;
                    GetOverlappedResult(pipe, &overlapped, &ignored, TRUE);
                    CloseHandle(overlapped.hEvent);
                    CloseHandle(pipe);
                    break;
                }
            } else if (err != ERROR_PIPE_CONNECTED) {
                CloseHandle(overlapped.hEvent);
                CloseHandle(pipe);
                continue;
            }
        }

        CloseHandle(overlapped.hEvent);

        // Spawn a thread for this client — accept loop immediately creates
        // the next pipe instance so another client can connect with zero delay
        auto ct = std::make_unique<ClientThread>();
        ClientThread* ctPtr = ct.get();
        ct->thread = std::thread([this, pipe, ctPtr]() {
            HandleClient(pipe);
            DisconnectNamedPipe(pipe);
            CloseHandle(pipe);
            ctPtr->done.store(true);
        });

        std::lock_guard<std::mutex> lock(threads_mutex_);
        client_threads_.push_back(std::move(ct));
    }
}

void PipeServer::HandleClient(HANDLE pipe) {
    static std::atomic<unsigned long long> next_client_id{1};
    const std::string client_id =
        "pipe-" + std::to_string(next_client_id.fetch_add(1));
    BridgeHealth::ClientConnected(client_id);

    while (running_.load()) {
        std::string request = ReadRequest(pipe);
        if (request.empty() || !running_.load()) break;

        std::string response;
        try {
            response = CommandDispatcher::Dispatch(request, gup_, client_id);
        } catch (const std::exception& e) {
            response = "{\"success\":false,\"error\":\"Internal bridge error: ";
            std::string msg = e.what();
            for (auto& c : msg) {
                if (c == '"') response += "\\\"";
                else if (c == '\\') response += "\\\\";
                else if (c == '\n') response += "\\n";
                else response += c;
            }
            response += "\",\"meta\":{\"transport\":\"namedpipe\"}}";
        }

        if (!WriteResponse(pipe, response)) {
            break;
        }
    }

    NativeHandlers::ReleaseSceneDeltaSession(client_id);
    BridgeHealth::ClientDisconnected(client_id);
}

std::string PipeServer::ReadRequest(HANDLE pipe) {
    std::string data;
    char buf[4096];
    DWORD bytes_read = 0;

    while (true) {
        bool ok = Transfer(pipe, buf, sizeof(buf), bytes_read, false);
        if (bytes_read > 0) {
            data.append(buf, bytes_read);
        }
        if (data.find('\n') != std::string::npos) {
            break;
        }
        if (!ok) {
            return {}; // never dispatch a partial request after cancellation/disconnect
        }
        if (bytes_read == 0) {
            break;
        }
    }

    while (!data.empty() && (data.back() == '\n' || data.back() == '\r')) {
        data.pop_back();
    }
    return data;
}

bool PipeServer::WriteResponse(HANDLE pipe, const std::string& response) {
    std::string out = response + "\n";
    DWORD written = 0;
    DWORD total = static_cast<DWORD>(out.size());
    const char* ptr = out.c_str();

    while (total > 0) {
        bool ok = Transfer(pipe, const_cast<char*>(ptr), total, written, true);
        if (!ok || written == 0) return false;
        ptr += written;
        total -= written;
    }
    return true;
}

bool PipeServer::Transfer(HANDLE pipe, void* buffer, DWORD size, DWORD& transferred, bool write) {
    transferred = 0;
    if (!running_.load()) return false;
    OVERLAPPED op = {};
    op.hEvent = CreateEvent(nullptr, TRUE, FALSE, nullptr);
    if (!op.hEvent) return false;
    BOOL ok = write ? WriteFile(pipe, buffer, size, &transferred, &op)
                    : ReadFile(pipe, buffer, size, &transferred, &op);
    if (!ok && GetLastError() == ERROR_IO_PENDING) {
        HANDLE events[] = {shutdown_event_, op.hEvent};
        DWORD wait = WaitForMultipleObjects(2, events, FALSE, INFINITE);
        if (wait != WAIT_OBJECT_0 + 1) {
            // Cancellation is asynchronous: keep op, its event and buffer alive
            // until the kernel has finished using them, then let Stop join us.
            CancelIoEx(pipe, &op);
            DWORD ignored = 0;
            GetOverlappedResult(pipe, &op, &ignored, TRUE);
            ok = FALSE;
            transferred = 0;
        } else {
            ok = GetOverlappedResult(pipe, &op, &transferred, FALSE);
        }
    }
    CloseHandle(op.hEvent);
    return ok != FALSE && running_.load();
}
