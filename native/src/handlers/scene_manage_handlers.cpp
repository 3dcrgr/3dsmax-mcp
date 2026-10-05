#include "mcp_bridge/native_handlers.h"
#include "mcp_bridge/handler_helpers.h"
#include "mcp_bridge/bridge_gup.h"

#include <triobj.h>

using json = nlohmann::json;
using namespace HandlerHelpers;

// ── native:set_parent ───────────────────────────────────────
std::string NativeHandlers::SetParent(const std::string& params, MCPBridgeGUP* gup) {
    return gup->GetExecutor().ExecuteSync([&params]() -> std::string {
        json p = json::parse(params, nullptr, false);
        auto children = p.value("children", std::vector<std::string>{});
        std::string parentName = p.value("parent", "");

        if (children.empty()) throw std::runtime_error("children is required");

        Interface* ip = GetCOREInterface();

        // Find parent node (nullptr = unparent to scene root)
        INode* parentNode = nullptr;
        if (!parentName.empty()) {
            parentNode = FindNodeByName(parentName);
            if (!parentNode) throw std::runtime_error("Parent not found: " + parentName);
        }

        json done = json::array();
        json notFound = json::array();

        for (const auto& childName : children) {
            INode* child = FindNodeByName(childName);
            if (!child) {
                notFound.push_back(childName);
                continue;
            }
            if (parentNode) {
                parentNode->AttachChild(child, TRUE);
            } else {
                // Unparent: attach to scene root
                ip->GetRootNode()->AttachChild(child, TRUE);
            }
            done.push_back(childName);
        }

        ip->RedrawViews(ip->GetTime());

        std::string msg;
        if (parentNode) {
            msg = "Parented " + std::to_string(done.size()) + " objects under " + WideToUtf8(parentNode->GetName());
        } else {
            msg = "Unparented " + std::to_string(done.size()) + " objects";
        }
        if (!notFound.empty()) {
            msg += " | Not found: " + std::to_string(notFound.size());
        }
        return msg;
    });
}

// ── native:batch_rename_objects ──────────────────────────────
std::string NativeHandlers::BatchRenameObjects(const std::string& params, MCPBridgeGUP* gup) {
    return gup->GetExecutor().ExecuteSync([&params]() -> std::string {
        json p = json::parse(params, nullptr, false);
        auto renames = p.value("renames", std::vector<json>{});

        if (renames.empty()) throw std::runtime_error("renames is required");

        json renamed = json::array();
        json notFound = json::array();

        for (const auto& r : renames) {
            std::string oldName = r.value("old_name", "");
            std::string newName = r.value("new_name", "");
            if (oldName.empty() || newName.empty()) continue;

            INode* node = FindNodeByName(oldName);
            if (!node) {
                notFound.push_back(oldName);
                continue;
            }

            std::wstring wNewName = Utf8ToWide(newName);
            node->SetName(wNewName.c_str());
            renamed.push_back(oldName + " -> " + newName);
        }

        std::string msg = "Renamed: " + std::to_string(renamed.size());
        if (!notFound.empty()) {
            msg += " | Not found:";
            for (const auto& nf : notFound) msg += " " + nf.get<std::string>();
        }
        return msg;
    });
}

// ── native:manage_scene ─────────────────────────────────────
std::string NativeHandlers::ManageScene(const std::string& params, MCPBridgeGUP* gup) {
    return gup->GetExecutor().ExecuteSync([&params]() -> std::string {
        json p = json::parse(params, nullptr, false);
        std::string action = p.value("action", "");
        std::transform(action.begin(), action.end(), action.begin(), ::tolower);

        Interface* ip = GetCOREInterface();

        if (action == "hold") {
            ip->FileHold();
            return "Hold saved successfully";
        }
        if (action == "fetch") {
            // Fetch replaces the scene with the Hold buffer after a
            // confirmation; keep it visible even if something left Max quiet.
            TempQuietMode prompts(FALSE);
            ip->FileFetch();
            NativeHandlers::ResetSceneDeltaSessions();
            return "Fetched (restored) held state";
        }
        if (action == "reset") {
            // noPrompt=FALSE: Max asks to confirm (and to save unsaved changes)
            // unless it is in quiet mode, which is forced off here, so unsaved
            // work is never discarded silently; the prompt reaches the agent
            // as BLOCKED_BY_DIALOG.
            TempQuietMode prompts(FALSE);
            ip->FileReset(FALSE);
            NativeHandlers::ResetSceneDeltaSessions();
            return "Scene reset to empty";
        }
        if (action == "save") {
            const MCHAR* filePath = ip->GetCurFilePath().data();
            if (!filePath || !*filePath) {
                return "No file path set — use File > Save As first";
            }
            ip->SaveToFile(filePath);
            return "Saved: " + WideToUtf8(filePath);
        }
        if (action == "info") {
            // File path
            std::string fp = WideToUtf8(ip->GetCurFilePath().data());
            if (fp.empty()) fp = "(unsaved)";

            // Object count + poly count
            INode* root = ip->GetRootNode();
            std::vector<INode*> allNodes;
            CollectNodes(root, allNodes);
            int objCount = (int)allNodes.size();

            TimeValue t = ip->GetTime();
            int polyCount = 0;
            for (INode* node : allNodes) {
                ObjectState os = node->EvalWorldState(t);
                if (!os.obj) continue;
                if (os.obj->CanConvertToType(triObjectClassID)) {
                    TriObject* tri = (TriObject*)os.obj->ConvertToType(t, triObjectClassID);
                    if (tri) {
                        polyCount += tri->GetMesh().getNumFaces();
                        if (tri != os.obj) tri->MaybeAutoDelete();
                    }
                }
            }

            json result;
            result["filePath"] = fp;
            result["objectCount"] = objCount;
            result["polyCount"] = polyCount;
            return result.dump();
        }

        throw std::runtime_error("Unknown action: " + action + ". Use hold, fetch, reset, save, or info.");
    });
}

// ── native:undo_last ────────────────────────────────────────────
std::string NativeHandlers::UndoLast(const std::string& params, MCPBridgeGUP* gup) {
    return gup->GetExecutor().ExecuteSync([]() -> std::string {
        RunMAXScript("max undo");
        json result;
        result["undone"] = true;
        result["message"] = "Undid last scene operation";
        return result.dump();
    });
}
