#pragma once
#include <windows.h>
#include <string>
#include <thread>
#include <atomic>
#include <vector>
#include <mutex>

// Serves the pipe; every request goes to the core through CoreHost.
class PipeServer {
public:
    explicit PipeServer(std::wstring pipe_name);
    ~PipeServer();

    void Start();
    void Stop();
    bool IsRunning() const { return running_.load(); }
    const std::wstring& PipeName() const { return pipe_name_; }

private:
    void AcceptLoop();
    void HandleClient(HANDLE pipe);
    std::string ReadRequest(HANDLE pipe);
    bool WriteResponse(HANDLE pipe, const std::string& response);
    void CleanupFinishedThreads();

    std::wstring pipe_name_;
    std::thread accept_thread_;
    std::atomic<bool> running_{false};
    HANDLE shutdown_event_ = nullptr;

    // Track client threads for cleanup
    std::mutex threads_mutex_;
    struct ClientThread {
        std::thread thread;
        std::atomic<bool> done{false};
    };
    std::vector<std::unique_ptr<ClientThread>> client_threads_;

    static constexpr DWORD BUFFER_SIZE = 64 * 1024;
};
