#pragma once
#include <max.h>
#include <nlohmann/json.hpp>

// All entry points run on Max's main thread. No cached ViewExp/INode pointers.
namespace AgentViewport {
nlohmann::json Execute(const nlohmann::json& params);
ViewExp& Get();
bool IsOwned();
bool IsRequested(const nlohmann::json& params);
void Redraw();
nlohmann::json Snapshot();
void Configure(const nlohmann::json& params);
// Save/restore only this private camera, never scene visibility or selection.
struct CameraState {
    Matrix3 tm;
    Point3 target;
    float fov, focal, width;
    bool perspective, grid, edges;
    int style;
};
CameraState SaveCamera();
void RestoreCamera(const CameraState& state);
nlohmann::json Project(const nlohmann::json& points);
void Shutdown(bool processExit=false);
// Optional, from GUP Start: lets reclaim see the first scene load after a
// restart too (otherwise loads are watched from the first agent_viewport call).
void WatchSceneLoads();
}
