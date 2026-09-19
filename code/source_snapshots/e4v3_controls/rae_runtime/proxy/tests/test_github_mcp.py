"""Integration test: spawn github_mcp_server.py and call its tools over the
real MCP protocol (stdio), the same way Claude/LiteLLM would.
"""

import asyncio
import json
from pathlib import Path
from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport

transport = PythonStdioTransport(
    script_path=str(Path(__file__).resolve().parents[1] / "github_mcp_server.py"),
    args=["--serve"],
)


def _to_dict(result) -> dict:
    """Same pattern as Ben's mcp_client.py — prefer structured .data,
    fall back to text JSON."""
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    content = getattr(result, "content", None)
    if content:
        text = getattr(content[0], "text", None)
        if text:
            return json.loads(text)
    raise RuntimeError(f"unexpected MCP tool result: {result!r}")


async def main():
    async with Client(transport) as client:
        print("=== Test 1: create_or_reuse_branch ===")
        branch_result = await client.call_tool(
            "create_or_reuse_branch",
            {"branch_name": "quant/MCP-TEST-1", "base_branch": "main"},
        )
        print(json.dumps(_to_dict(branch_result), indent=2))

        print("\n=== Test 2: read_file ===")
        read_result = await client.call_tool(
            "read_file",
            {"branch": "main", "path": "rae_runtime/proxy/strategy.py"},
        )
        print(json.dumps(_to_dict(read_result), indent=2))

        print("\n=== Test 3: create_or_reuse_branch (idempotency check) ===")
        branch_result_2 = await client.call_tool(
            "create_or_reuse_branch",
            {"branch_name": "quant/MCP-TEST-1", "base_branch": "main"},
        )
        print(json.dumps(_to_dict(branch_result_2), indent=2))

        print("\n=== Test 4: list_files ===")
        list_result = await client.call_tool(
            "list_files",
            {"branch": "main", "directory": "rae_runtime/proxy"},
        )
        print(json.dumps(_to_dict(list_result), indent=2))

        print("\n=== Test 5: list_files on nonexistent path ===")
        bad_result = await client.call_tool(
            "list_files",
            {"branch": "main", "directory": "rae_runtime/nonexistent"},
        )
        print(json.dumps(_to_dict(bad_result), indent=2))

        print("\n=== Test 6: commit_and_push ===")
        commit_result = await client.call_tool(
            "commit_and_push",
            {
                "branch": "quant/MCP-TEST-1",
                "path": "rae_runtime/proxy/strategy.py",
                "content": "# strategy.py - modified via MCP commit_and_push test\ndef generate_signals(prices):\n    return prices.rolling(10).mean()",
                "commit_message": "Test: commit_and_push via MCP",
            },
        )
        print(json.dumps(_to_dict(commit_result), indent=2))

        print("\n=== Test 7: commit_and_push on nonexistent branch ===")
        bad_commit = await client.call_tool(
            "commit_and_push",
            {
                "branch": "quant/DOES-NOT-EXIST",
                "path": "rae_runtime/proxy/strategy.py",
                "content": "# should fail",
                "commit_message": "Should fail",
            },
        )
        print(json.dumps(_to_dict(bad_commit), indent=2))

        print("\n=== Test 8: commit_and_push directly to main (should be blocked) ===")
        blocked_result = await client.call_tool(
            "commit_and_push",
            {
                "branch": "main",
                "path": "rae_runtime/proxy/strategy.py",
                "content": "# this should never land",
                "commit_message": "Should be blocked",
            },
        )
        print(json.dumps(_to_dict(blocked_result), indent=2))


if __name__ == "__main__":
    asyncio.run(main())