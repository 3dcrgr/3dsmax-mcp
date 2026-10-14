#pragma once
#include <string>
#include <nlohmann/json.hpp>

class MCPBridgeGUP;

// Direct native routes used by invoke_tool and the native diagnostic harness.
// Python orchestration still runs through the external MCP server.
namespace NativeToolRegistry {
std::string ExecuteTool(const std::string& toolName,
                        const nlohmann::json& input, MCPBridgeGUP* gup);
// Runs one native command directly. Smoke cases use it for commands that sit
// behind a Python tool (query_scene's actions) and so have no registry entry.
std::string ExecuteCommand(const std::string& cmdType, const nlohmann::json& input,
                           MCPBridgeGUP* gup, const std::string& label);
}
