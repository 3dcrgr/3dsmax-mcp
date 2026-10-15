#pragma once
#include <max.h>
#include <gup.h>
#include <iparamb2.h>
#include <memory>

#include "main_thread_executor.h"
#include "pipe_server.h"

// "MCPB" + "RIDG" as hex
#define MCP_BRIDGE_CLASS_ID Class_ID(0x4D435042, 0x52494447)

// The Max plugin object, part of the shell: it owns the pipe, the main-thread
// executor and the shell services, and reaches command handling only through
// CoreHost (core_host.h). Core code knows none of this; there, MCPBridgeGUP is
// the handler-facing facade of core_bridge.h.
class MCPBridgeShellGUP : public GUP {
public:
    MCPBridgeShellGUP() = default;
    ~MCPBridgeShellGUP() override = default;

    // GUP interface
    DWORD Start() override;
    void Stop() override;
    void DeleteThis() override;
    DWORD_PTR Control(DWORD parameter) override { return 0; }

    MainThreadExecutor& GetExecutor() { return executor_; }
    bool IsPipeRunning() const;
    const std::string& InstanceId() const { return instance_id_; }
    const std::string& PipeNameUtf8() const { return pipe_name_utf8_; }
    void ClaimInstance();

private:
    void StartPipe();
    void StopPipe();
    void RegisterInstance();
    void UnregisterInstance();

    std::unique_ptr<PipeServer> pipe_server_;
    MainThreadExecutor executor_;
    std::string instance_id_;
    std::string pipe_name_utf8_;
};

extern HINSTANCE hInstance;
ClassDesc2* GetMCPBridgeDesc();
