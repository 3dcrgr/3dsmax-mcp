// SDK-independent tests for McpEnvelope::ErrorCodeForMessage, the code the
// dispatcher and the shell give an unstructured native error.
#include "mcp_bridge/response_envelope.h"
#include <iostream>
#include <stdexcept>
#include <string>

void require(bool ok, const std::string& message) { if (!ok) throw std::runtime_error(message); }

void expect(const std::string& message, const std::string& code, bool retryable) {
    const std::string got = McpEnvelope::ErrorCodeForMessage(message);
    require(got == code, "'" + message + "' -> " + got + ", expected " + code);
    require(McpEnvelope::IsRetryableCode(got) == retryable, "'" + message + "' retryable mismatch");
}

int main() {
    try {
        // The executor refused or drained the request before it started:
        // bridge down, retryable (the work never ran).
        expect("3ds Max is exiting; the request did not run", "BRIDGE_DOWN", true);
        expect("MainThreadExecutor is shutting down", "BRIDGE_DOWN", true);
        expect("MainThreadExecutor not initialized", "BRIDGE_DOWN", true);
        expect("Failed to post work to main thread", "BRIDGE_DOWN", true);
        expect("Main thread execution timed out before starting; queued work cancelled", "BRIDGE_DOWN", true);
        // A MAXScript interrupted by Max's own shutdown is not a bridge failure.
        expect("MAXScript did not complete: 3ds Max is shutting down (e.g. after quitMax)", "BAD_PARAM", false);
        // Unchanged rules.
        expect("Object not found: Box001", "NOT_FOUND", false);
        expect("Unknown modifier class: Foo", "PLUGIN_MISSING", false);
        expect("USER_BUSY: an undo hold is open", "USER_BUSY", true);
        expect("Ambiguous name", "AMBIGUOUS", false);
        expect("something else", "BAD_PARAM", false);
        std::cout << "PASS: native error codes\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
