#pragma once
#include <windows.h>
#include <string>
#include <thread>
#include <atomic>
#include <vector>
#include <mutex>
#include <unordered_set>

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
    bool Await(HANDLE pipe, OVERLAPPED& io, DWORD& bytes);
    std::string ReadRequest(HANDLE pipe, OVERLAPPED& io);
    bool WriteResponse(HANDLE pipe, OVERLAPPED& io, const std::string& response);
    void CleanupFinishedThreads();
    void Track(HANDLE pipe);
    void Untrack(HANDLE pipe);

    std::wstring pipe_name_;
    std::thread accept_thread_;
    std::atomic<bool> running_{false};

    // Every pipe instance this server owns, listening or connected. Stop()
    // cancels their I/O, so no thread is left waiting on a pipe.
    std::mutex instances_mutex_;
    std::unordered_set<HANDLE> instances_;

    // Track client threads for cleanup
    std::mutex threads_mutex_;
    struct ClientThread {
        std::thread thread;
        std::atomic<bool> done{false};
    };
    std::vector<std::unique_ptr<ClientThread>> client_threads_;

    static constexpr DWORD BUFFER_SIZE = 64 * 1024;
};
