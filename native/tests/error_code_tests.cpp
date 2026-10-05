// SDK-independent tests for NativeErrors::CodeForMessage, the code the
// dispatcher gives an unstructured native error.
#include "mcp_bridge/native_error_code.h"
#include <iostream>
#include <stdexcept>
#include <string>

void require(bool ok, const std::string& message) { if (!ok) throw std::runtime_error(message); }

void expect(const std::string& message, const std::string& code, bool retryable) {
    const std::string got = NativeErrors::CodeForMessage(message);
    require(got == code, "'" + message + "' -> " + got + ", expected " + code);
    require(NativeErrors::IsRetryable(got) == retryable, "'" + message + "' retryable mismatch");
}

int main() {
    try {
        // The executor refused or drained the request before it started:
        // bridge down, retryable (the work never ran).
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
