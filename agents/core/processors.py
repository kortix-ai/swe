from typing import Dict, Any, List, Optional, Callable, Union, AsyncGenerator, Set, Tuple
import asyncio, json, logging, re
from abc import ABC, abstractmethod
from agentpress.tools import ToolResult, ToolRegistry


class ToolParserBase(ABC):
    @abstractmethod
    async def parse_response(self, response: Any) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def parse_stream(self, response_chunk: Any, tool_calls_buffer: Dict[int, Dict]):
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

    def _extract_tag_content(self, xml: str, tag: str) -> Tuple[Optional[str], Optional[str]]:
        try:
            start = xml.find(f'<{tag}')
            if start == -1:
                return None, xml
            end = xml.find(f'</{tag}>', start)
            if end == -1:
                return None, xml
            content = xml[start:end + len(f'</{tag}>')]
            remaining = xml[end + len(f'</{tag}>'):]
            return content, remaining
        except Exception as e:
            logging.error(f"XMLToolParser extraction error: {e}")
            return None, xml

    def _extract_attribute(self, tag: str, attr: str) -> Optional[str]:
        match = re.search(fr'{attr}="([^"]*)"', tag)
        return match.group(1) if match else None

    async def _parse_xml_to_tool_call(self, xml: str) -> Optional[Dict[str, Any]]:
        try:
            tag_match = re.match(r'<([^\s>]+)', xml)
            if not tag_match:
                return None
            tag = tag_match.group(1)
            tool_info = self.tool_registry.get_xml_tool(tag)
            if not tool_info or not tool_info['schema'].xml_schema:
                return None
            schema = tool_info['schema'].xml_schema
            params = {}
            for m in schema.mappings:
                if m.node_type == "attribute":
                    val = self._extract_attribute(xml, m.path)
                    if val:
                        params[m.param_name] = val
                elif m.node_type == "element":
                    content, _ = self._extract_tag_content(xml, m.path)
                    if content:
                        inner_text = re.sub(r'<[^>]+>', '', content).strip()
                        params[m.param_name] = inner_text
                elif m.node_type == "content" and m.path == ".":
                    content, _ = self._extract_tag_content(xml, tag)
                    if content:
                        inner_text = re.sub(r'<[^>]+>', '', content).strip()
                        params[m.param_name] = inner_text
            # Check all required params
            for mapping in schema.mappings:
                if mapping.param_name not in params:
                    return None
            return {"id": f"tool_{hash(xml)}", "type": "function", "function": {"name": tool_info['method'], "arguments": json.dumps(params)}}
        except Exception as e:
            logging.error(f"XMLToolParser parse error: {e}")
            return None

    async def parse_response(self, response: Any) -> Dict[str, Any]:
        content = response.choices[0].message.get('content', "")
        message = {"role": "assistant", "content": content}
        tool_calls = []
        try:
            # Extract all registered tags
            search_content = content
            for tag in self.tool_registry.xml_tools.keys():
                # Keep extracting until no more tags found
                while f'<{tag}' in search_content:
                    xml_chunk, remaining = self._extract_tag_content(search_content, tag)
                    if xml_chunk:
                        tc = await self._parse_xml_to_tool_call(xml_chunk)
                        if tc:
                            tool_calls.append(tc)
                        search_content = remaining
                    else:
                        break
            if tool_calls:
                message["tool_calls"] = tool_calls
        except Exception as e:
            logging.error(f"XMLToolParser response parse error: {e}")
        return message

    async def parse_stream(self, response_chunk: Any, tool_calls_buffer: Dict[int, Dict]) -> (Optional[Dict[str, Any]], bool):
        if 'xml_buffer' not in tool_calls_buffer:
            tool_calls_buffer['xml_buffer'] = ''
        content = getattr(response_chunk.choices[0].delta, 'content', '')
        is_complete = hasattr(response_chunk.choices[0], 'finish_reason') and bool(response_chunk.choices[0].finish_reason)
        if content:
            tool_calls_buffer['xml_buffer'] += content
            tool_calls = []
            # Attempt extracting all tags repeatedly until no more found
            updated = True
            while updated:
                updated = False
                for tag in list(self.tool_registry.xml_tools.keys()):
                    if f'<{tag}' in tool_calls_buffer['xml_buffer']:
                        xml_chunk, remaining = self._extract_tag_content(tool_calls_buffer['xml_buffer'], tag)
                        if xml_chunk:
                            tc = await self._parse_xml_to_tool_call(xml_chunk)
                            if tc:
                                tool_calls.append(tc)
                            tool_calls_buffer['xml_buffer'] = remaining
                            updated = True
            if tool_calls:
                return {"role": "assistant", "content": content, "tool_calls": tool_calls}, is_complete
        return None, is_complete


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
        self.tool_calls_buffer = {}
        self.processed_tool_calls = set()
        self.content_buffer = ""

    async def process_stream(self, response_stream: AsyncGenerator, execute_tools: bool = True, execute_tools_on_stream: bool = False) -> AsyncGenerator:
        async for chunk in response_stream:
            try:
                if hasattr(chunk.choices[0].delta, 'content') and chunk.choices[0].delta.content:
                    self.content_buffer += chunk.choices[0].delta.content
                if hasattr(chunk.choices[0].delta, 'tool_calls'):
                    parsed_message, is_complete = await self.tool_parser.parse_stream(chunk, self.tool_calls_buffer)
                    if parsed_message and 'tool_calls' in parsed_message:
                        self.tool_calls_accumulated = parsed_message['tool_calls']
                if execute_tools and hasattr(self, 'tool_calls_accumulated'):
                    new_tool_calls = [tc for tc in self.tool_calls_accumulated if tc['id'] not in self.processed_tool_calls]
                    if new_tool_calls:
                        if execute_tools_on_stream:
                            results = await self.tool_executor.execute_tool_calls(new_tool_calls, self.available_functions, self.thread_id, self.processed_tool_calls)
                            for result in results:
                                await self.results_adder.add_tool_result(self.thread_id, result)
                                self.processed_tool_calls.add(result['tool_call_id'])

                msg_tool_calls = getattr(self, 'tool_calls_accumulated', None)
                if not hasattr(self, '_message_added'):
                    await self.results_adder.add_initial_response(self.thread_id, self.content_buffer, msg_tool_calls)
                    self._message_added = True
                else:
                    await self.results_adder.update_response(self.thread_id, self.content_buffer, msg_tool_calls)

                yield chunk
            except Exception as e:
                logging.error(f"Stream processing error: {e}")

        if not execute_tools_on_stream and hasattr(self, 'tool_calls_accumulated'):
            remaining_tool_calls = [tc for tc in self.tool_calls_accumulated if tc['id'] not in self.processed_tool_calls]
            if remaining_tool_calls:
                results = await self.tool_executor.execute_tool_calls(remaining_tool_calls, self.available_functions, self.thread_id, self.processed_tool_calls)
                for result in results:
                    await self.results_adder.add_tool_result(self.thread_id, result)
                    self.processed_tool_calls.add(result['tool_call_id'])

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
