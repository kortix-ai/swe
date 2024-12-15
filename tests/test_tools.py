import pytest
import asyncio
import logging
from typing import Dict, Any

from agents.core.tools import Tool, ToolRegistry, xml_schema, ToolResult


class MockTool(Tool):
    @xml_schema(
        tag_name="test_tool",
        mappings=[{"param_name": "param1", "node_type": "element", "path": "param1"}],
        example="<test_tool><param1>value</param1></test_tool>"
    )
    def test_tool(self, param1: str) -> ToolResult:
        return self.success_response(f"Received {param1}")


@pytest.mark.asyncio
async def test_tool_registry():
    logging.basicConfig(level=logging.INFO)
    
    registry = ToolRegistry()
    await registry.register_tool(MockTool)
    
    # Check if the XML tool is registered
    xml_tools = registry.xml_tools
    assert "test_tool" in xml_tools
    tool_info = xml_tools["test_tool"]
    assert tool_info["method"] == "test_tool"
    assert callable(getattr(tool_info["instance"], "test_tool"))

    # Test direct call
    result = getattr(tool_info["instance"], "test_tool")(param1="hello")
    assert result.success
    assert "hello" in result.output


@pytest.mark.asyncio
async def test_tool_result_methods():
    tool = MockTool()
    success = tool.success_response("All good")
    fail = tool.fail_response("Something went wrong")

    assert success.success is True
    assert "All good" in success.output

    assert fail.success is False
    assert "Something went wrong" in fail.output


@pytest.mark.asyncio
async def test_registry_get_available_functions():
    registry = ToolRegistry()
    await registry.register_tool(MockTool)
    funcs = registry.get_available_functions()
    assert "test_tool" in funcs
    assert callable(funcs["test_tool"])

    # Call the function synchronously since it's not async
    result = funcs["test_tool"](param1="test")
    assert result.success
    assert "test" in result.output

