from typing import Dict, Any, List, Optional, Callable, Union, AsyncGenerator, Set, Tuple
import asyncio, json, logging
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from agents.core.tools import ToolResult, ToolRegistry, Tool, xml_schema


class ToolParserBase(ABC):
    @abstractmethod
    async def parse_response(self, response: Any) -> Dict[str, Any]:
        pass


class ToolExecutorBase(ABC):
    @abstractmethod
    async def execute_tool_calls(self, tool_calls: List[Dict[str, Any]], available_functions: Dict[str, Callable], thread_id: str, executed_tool_calls: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
        pass


class ResultsAdderBase(ABC):
    @abstractmethod
    async def add_initial_response(self, thread_id: str, content: str, tool_calls: Optional[List[Dict[str, Any]]] = None):
        pass

    @abstractmethod
    async def update_response(self, thread_id: str, content: str, tool_calls: Optional[List[Dict[str, Any]]] = None):
        pass

    @abstractmethod
    async def add_tool_result(self, thread_id: str, result: Dict[str, Any]):
        pass


class XMLToolParser(ToolParserBase):
    def __init__(self, tool_registry: Optional[ToolRegistry] = None):
        self.tool_registry = tool_registry or ToolRegistry()


    async def _parse_xml_to_tool_call(self, xml: str) -> Optional[Dict[str, Any]]:
        try:
            root = ET.fromstring(xml)
            tag = root.tag
            tool_info = self.tool_registry.get_xml_tool(tag)
            if not tool_info or not tool_info['schema'].xml_schema:
                return None
            schema = tool_info['schema'].xml_schema
            params = {}
            for m in schema.mappings:
                if m.node_type == "attribute":
                    value = root.attrib.get(m.path)
                    if value:
                        params[m.param_name] = value
                elif m.node_type == "element":
                    element = root.find(m.path)
                    if element is not None and element.text:
                        params[m.param_name] = element.text.strip()
                elif m.node_type == "content" and m.path == ".":
                    params[m.param_name] = ''.join(root.itertext()).strip()
            # Check all required params
                if m.param_name not in params:
                    logging.warning(f"Missing required parameter: {m.param_name}")
                    return None
            return {"id": f"tool_{hash(xml)}", "type": "function", "function": {"name": tool_info['method'], "arguments": json.dumps(params)}}
        except ET.ParseError as e:
            logging.error(f"XML parsing error: {e}")
            return None

    async def parse_response(self, response: Any) -> Dict[str, Any]:
        content = response.choices[0].message.get('content', "")
        message = {"role": "assistant", "content": content}
        tool_calls = []
        try:
            # Extract all registered tags
            search_content = content
            search_content = content
            for tag in self.tool_registry.xml_tools.keys():
                while f'<{tag}' in search_content:
                    xml_chunk_start = search_content.find(f'<{tag}')
                    if xml_chunk_start == -1:
                        break
                    close_tag = f'</{tag}>'
                    xml_chunk_end = search_content.find(close_tag, xml_chunk_start)
                    if xml_chunk_end == -1:
                        break
                    xml_chunk = search_content[xml_chunk_start:xml_chunk_end+len(close_tag)]
                    tc = await self._parse_xml_to_tool_call(xml_chunk)
                    if tc:
                        tool_calls.append(tc)
                    search_content = search_content[xml_chunk_end+len(close_tag):]
            if tool_calls:
                message["tool_calls"] = tool_calls
        except Exception as e:
            logging.error(f"XMLToolParser response parse error: {e}")
        return message


class XMLToolExecutor(ToolExecutorBase):
    def __init__(self, parallel: bool = True, tool_registry: Optional[ToolRegistry] = None):
        self.parallel = parallel
        self.tool_registry = tool_registry or ToolRegistry()

    async def execute_tool_calls(self, tool_calls: List[Dict[str, Any]], available_functions: Dict[str, Callable], thread_id: str, executed_tool_calls: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
        if executed_tool_calls is None:
            executed_tool_calls = set()

        async def exec_tool(tc):
            if tc['id'] in executed_tool_calls:
                return None
            try:
                fname = tc['function']['name']
                fargs = tc['function']['arguments']
                if isinstance(fargs, str):
                    fargs = json.loads(fargs)
                tool_info = self.tool_registry.get_tool(fname)
                if not tool_info:
                    return {"role": "tool", "tool_call_id": tc['id'], "name": fname, "content": str(ToolResult(False, "Function not found"))}
                func = getattr(tool_info['instance'], fname, None)
                if not func:
                    return {"role": "tool", "tool_call_id": tc['id'], "name": fname, "content": str(ToolResult(False, "Function not found on instance"))}
                res = await func(**fargs)
                executed_tool_calls.add(tc['id'])
                return {"role": "tool", "tool_call_id": tc['id'], "name": fname, "content": str(res)}
            except Exception as e:
                logging.error(f"XMLToolExecutor error: {e}")
                return {"role": "tool", "tool_call_id": tc['id'], "name": tc['function']['name'], "content": str(ToolResult(False, str(e)))}

        if self.parallel:
            tasks = [exec_tool(tc) for tc in tool_calls]
            results = await asyncio.gather(*tasks)
            return [r for r in results if r]
        else:
            results = []
            for tc in tool_calls:
                res = await exec_tool(tc)
                if res:
                    results.append(res)
            return results


class XMLResultsAdder(ResultsAdderBase):
    def __init__(self, thread_manager):
        self.thread_manager = thread_manager
        self.add_message = thread_manager.add_message
        self.update_message = getattr(thread_manager, 'update_message', None)
        self.get_messages = thread_manager.list_messages
        self.message_added = False

    async def add_initial_response(self, thread_id: str, content: str, tool_calls: Optional[List[Dict[str, Any]]] = None):
        await self.add_message(thread_id, {"role": "assistant", "content": content})
        self.message_added = True

    async def update_response(self, thread_id: str, content: str, tool_calls: Optional[List[Dict[str, Any]]] = None):
        if not self.message_added:
            await self.add_initial_response(thread_id, content, tool_calls)
            return
        if self.update_message is not None:
            await self.update_message(thread_id, {"role": "assistant", "content": content})
        else:
            # If update_message not available, add another assistant message
            await self.add_message(thread_id, {"role": "assistant", "content": content})

    async def add_tool_result(self, thread_id: str, result: Dict[str, Any]):
        try:
            messages = await self.get_messages(thread_id)
            assistant_msg = next((m for m in reversed(messages) if m['role'] == 'assistant'), None)
            if assistant_msg:
                content = assistant_msg['content']
                tool_start = content.find(f'<{result["name"]}')
                if tool_start >= 0:
                    tag_end = content.find('>', tool_start)
                    if tag_end >= 0:
                        root_tag = content[tool_start:tag_end + 1]
                        await self.add_message(thread_id, {"role": "user", "content": f"Result for {root_tag}\n{result['content']}"})
                        return
            await self.add_message(thread_id, {"role": "user", "content": f"Result for {result['name']}:\n{result['content']}"})
        except Exception as e:
            logging.error(f"XMLResultsAdder error: {e}")
            await self.add_message(thread_id, {"role": "user", "content": f"Result for {result['name']}:\n{result['content']}"})


class LLMResponseProcessor:
    def __init__(self, thread_id: str, available_functions: Dict, thread_manager, parallel_tool_execution: bool = True, tool_parser: Optional[ToolParserBase] = None, tool_executor: Optional[ToolExecutorBase] = None, results_adder: Optional[ResultsAdderBase] = None):
        self.thread_id = thread_id
        self.available_functions = available_functions or {}
        self.thread_manager = thread_manager
        self.tool_parser = tool_parser or XMLToolParser()
        self.tool_executor = tool_executor or XMLToolExecutor(parallel=parallel_tool_execution)
        self.results_adder = results_adder or XMLResultsAdder(thread_manager)
        self.processed_tool_calls = set()
        self.content_buffer = ""

    async def process_response(self, response: Any, execute_tools: bool = True) -> None:
        try:
            assistant_message = await self.tool_parser.parse_response(response)
            await self.results_adder.add_initial_response(self.thread_id, assistant_message['content'], assistant_message.get('tool_calls'))
            if execute_tools and 'tool_calls' in assistant_message:
                results = await self.tool_executor.execute_tool_calls(assistant_message['tool_calls'], self.available_functions, self.thread_id, self.processed_tool_calls)
                for result in results:
                    await self.results_adder.add_tool_result(self.thread_id, result)
        except Exception as e:
            logging.error(f"Response processing error: {e}")
            response_content = response.choices[0].message.get('content', '')
            await self.results_adder.add_initial_response(self.thread_id, response_content)
