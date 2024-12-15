import asyncio
import json
import logging
from typing import Dict, Any

import pytest

from agents.core.processors import XMLToolParser, XMLToolExecutor, XMLResultsAdder, LLMResponseProcessor
from agents.core.tools import Tool, ToolRegistry, ToolResult, xml_schema


class MockResponse:
    def __init__(self, content: str):
        self.choices = [type('Choice', (), {'message': {'content': content}})]


class SimpleThreadManager:
    def __init__(self):
        self.messages = []
        
    async def add_message(self, thread_id: str, message: Dict[str, Any]):
        print(f"Adding message to thread {thread_id}:", message)
        self.messages.append(message)
        
    async def list_messages(self, thread_id: str):
        return self.messages


class WeatherTool(Tool):
    @xml_schema(
        tag_name="get_weather",
        mappings=[{"param_name": "city", "node_type": "element", "path": "city"}],
        example="<get_weather><city>London</city></get_weather>"
    )
    async def get_weather(self, city: str) -> ToolResult:
        return self.success_response(f"Weather in {city} is sunny!")


@pytest.mark.asyncio
async def test_processor_with_weather_tool():
    logging.basicConfig(level=logging.INFO)
    
    # Set up test environment
    registry = ToolRegistry()
    await registry.register_tool(WeatherTool)
    
    # Create components
    tool_parser = XMLToolParser(registry)
    tool_executor = XMLToolExecutor(parallel=True, tool_registry=registry)
    thread_manager = SimpleThreadManager()
    results_adder = XMLResultsAdder(thread_manager)
    
    processor = LLMResponseProcessor(
        thread_id="test_thread",
        available_functions=registry.get_available_functions(),
        thread_manager=thread_manager,
        tool_parser=tool_parser,
        tool_executor=tool_executor,
        results_adder=results_adder
    )
    
    test_cases = [
        {
            "name": "Single weather query with context",
            "input": """Let me check the weather for you.
            <get_weather><city>London</city></get_weather>
            I'll get that information right away.""",
            "expected_tool_calls": 1,
            "expected_city": "London"
        },
        {
            "name": "Single weather query",
            "input": "<get_weather><city>Paris</city></get_weather>",
            "expected_tool_calls": 1,
            "expected_city": "Paris"
        },
        {
            "name": "Invalid weather query",
            "input": "<get_weather>No city</get_weather>",
            "expected_tool_calls": 0,
            "expected_city": None
        },
        {
            "name": "Multiple weather queries",
            "input": """Multiple tools test:
            <get_weather><city>Tokyo</city></get_weather>
            <get_weather><city>Berlin</city></get_weather>""",
            "expected_tool_calls": 2,
            "expected_cities": ["Tokyo", "Berlin"]
        }
    ]
    
    for test_case in test_cases:
        test_response = MockResponse(test_case["input"])
        parsed = await tool_parser.parse_response(test_response)
        
        if test_case["expected_tool_calls"] > 0:
            assert "tool_calls" in parsed, "Expected tool calls in parsed response"
            assert len(parsed["tool_calls"]) == test_case["expected_tool_calls"]
            
            if "expected_city" in test_case:
                tool_args = json.loads(parsed["tool_calls"][0]["function"]["arguments"])
                assert tool_args["city"] == test_case["expected_city"]
            
            if "expected_cities" in test_case:
                cities = [json.loads(tc["function"]["arguments"])["city"] for tc in parsed["tool_calls"]]
                assert cities == test_case["expected_cities"]
        else:
            assert "tool_calls" not in parsed or not parsed["tool_calls"]
        
        await processor.process_response(test_response)
        messages = await thread_manager.list_messages("test_thread")
        assert len(messages) > 0, "Expected messages after processing"
