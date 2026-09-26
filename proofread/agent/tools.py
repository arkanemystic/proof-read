"""Tool specs exposed to the inner agent. Implementations live in loop.py and go through Sandbox + Verifier."""

from __future__ import annotations

from proofread.contracts import ToolSpec

TOOL_SPECS: dict[str, ToolSpec] = {
    "read_file": ToolSpec(
        name="read_file", description="Read a text file from the workspace.",
        parameters={"type": "object", "properties": {"path": {"type": "string", "description": "Path, relative to /workspace or absolute."}},
                    "required": ["path"], "additionalProperties": False}),
    "write_file": ToolSpec(
        name="write_file", description="Create or overwrite a text file in the workspace with the given content.",
        parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"], "additionalProperties": False}),
    "list_dir": ToolSpec(
        name="list_dir", description="List files and directories under a workspace directory.",
        parameters={"type": "object", "properties": {"path": {"type": "string", "description": "Directory; default /workspace."}},
                    "required": [], "additionalProperties": False}),
    "run": ToolSpec(
        name="run", description="Run a shell command in /workspace and return its exit code and output.",
        parameters={"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"],
                    "additionalProperties": False}),
    "apply_patch": ToolSpec(
        name="apply_patch",
        description="Edit a file by replacing one exact occurrence of old_string with new_string.",
        parameters={"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"},
                                                     "new_string": {"type": "string"}},
                    "required": ["path", "old_string", "new_string"], "additionalProperties": False}),
}
