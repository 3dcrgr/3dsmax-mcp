#pragma once
#include <max.h>

// Objects Max keeps after a request ends: undo records on theHold and the
// reference pins inside them. They live in the shell so no vtable Max calls on
// undo, redo, purge or a dependent notification points into the core. Core
// handlers reach them through McpHostApi. Max's main thread, inside the
// caller's hold.
namespace UndoRecords {
// scene_patch set_flags; kMcpNode* bits (host_api.h).
void PutNodeFlagsRestore(INode* node, unsigned before, unsigned after);
// plugin_patch environment binding.
void PutEnvironmentRestore(Texmap* before, BOOL beforeOn, Texmap* after, BOOL afterOn);
// A ReferenceMaker that keeps `target` alive without being a scene consumer.
ReferenceMaker* MakePin(ReferenceTarget* target);
void DestroyPin(ReferenceMaker* pin);
ReferenceTarget* PinTarget(ReferenceMaker* pin);
bool IsPin(ReferenceMaker* maker);
}
