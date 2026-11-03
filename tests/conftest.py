"""Shared pytest setup.

#50: the MCP command log (maxmcp/helpers/command_log.py) writes to the real
%LOCALAPPDATA%\\3dsmax-mcp\\diagnostics folder. Fake bridge calls from the tests
must never land there, where a crash dump would be matched against them.
tests/test_command_log.py turns it on again with a temporary folder.
"""

import os

os.environ["MCP_COMMAND_LOG"] = "0"
