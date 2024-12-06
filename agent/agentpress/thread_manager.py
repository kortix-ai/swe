"""
Conversation thread management system for AgentPress.

This module provides comprehensive conversation management, including:
- Thread creation and persistence
- Message handling with support for text and images
- Tool registration and execution
- LLM interaction with streaming support
- Error handling and cleanup
"""

import json
import logging
import os
import uuid
from typing import List, Dict, Any, Optional, Type, Union, AsyncGenerator
import uuid
from agentpress.llm import make_llm_api_call
from agentpress.tool import Tool, ToolResult
from agentpress.tool_registry import ToolRegistry
from agentpress.llm_response_processor import LLMResponseProcessor
from agentpress.base_processors import ToolParserBase, ToolExecutorBase, ResultsAdderBase

from agentpress.xml_tool_parser import XMLToolParser
from agentpress.xml_tool_executor import XMLToolExecutor
from agentpress.xml_results_adder import XMLResultsAdder
from agentpress.standard_tool_parser import StandardToolParser
from agentpress.standard_tool_executor import StandardToolExecutor
from agentpress.standard_results_adder import StandardResultsAdder

class ThreadManager:
    """Manages conversation threads with LLM models and tool execution.
    
    Provides comprehensive conversation management, handling message threading,
    tool registration, and LLM interactions with support for both standard and
    XML-based tool execution patterns.
    
    Attributes:
        threads_dir (str): Directory for storing thread files
        tool_registry (ToolRegistry): Registry for managing available tools
        
    Methods:
        add_tool: Register a tool with optional function filtering
        create_thread: Create a new conversation thread
        add_message: Add a message to a thread
        list_messages: Retrieve messages from a thread
        run_thread: Execute a conversation thread with LLM
    """

    def __init__(self, threads_dir: str = "/home/nightfury/projects/test/new_agent_version/threads"):
        """Initialize ThreadManager.
        
        Args:
            threads_dir: Directory to store thread files
            
        Notes:
            Creates the threads directory if it doesn't exist
        """
        self.threads_dir = threads_dir
        self.tool_registry = ToolRegistry()
        os.makedirs(self.threads_dir, exist_ok=True)
        self.tool_executor = StandardToolExecutor(parallel=False)

    def add_tool(self, tool_class: Type[Tool], function_names: Optional[List[str]] = None, **kwargs):
        """Add a tool to the ThreadManager.
        
        Args:
            tool_class: The tool class to register
            function_names: Optional list of specific functions to register
            **kwargs: Additional arguments passed to tool initialization
            
        Notes:
            - If function_names is None, all functions are registered
            - Tool instances are created with provided kwargs
        """
        self.tool_registry.register_tool(tool_class, function_names, **kwargs)

    async def create_thread(self) -> str:
        """Create a new conversation thread.
        
        Returns:
            str: Unique thread ID for the created thread
            
        Raises:
            IOError: If thread file creation fails
            
        Notes:
            Creates new thread file and history file with empty messages lists
        """
        thread_id = str(uuid.uuid4())
        thread_path = os.path.join(self.threads_dir, f"{thread_id}.json")
        history_path = os.path.join(self.threads_dir, f"{thread_id}_history.json")
        
        empty_data = {"messages": []}
        
        # Create both files with empty message lists
        with open(thread_path, 'w') as f:
            json.dump(empty_data, f)
        with open(history_path, 'w') as f:
            json.dump(empty_data, f)
            
        return thread_id

    async def add_message(self, thread_id: str, message_data: Dict[str, Any], images: Optional[List[Dict[str, Any]]] = None):
        """Add a message to an existing thread.
        
        Args:
            thread_id: ID of the target thread
            message_data: Message content and metadata
            images: Optional list of image data dictionaries
            
        Raises:
            FileNotFoundError: If thread doesn't exist
            Exception: For other operation failures
            
        Notes:
            - Handles cleanup of incomplete tool calls
            - Supports both text and image content
            - Converts ToolResult instances to strings
        """
        logging.info(f"Adding message to thread {thread_id} with images: {images}")
        thread_path = os.path.join(self.threads_dir, f"{thread_id}.json")
        history_path = os.path.join(self.threads_dir, f"{thread_id}_history.json")
        
        try:
            with open(thread_path, 'r') as f:
                thread_data = json.load(f)
            
            messages = thread_data["messages"]
            
            # Handle cleanup of incomplete tool calls
            if message_data['role'] == 'user':
                last_assistant_index = next((i for i in reversed(range(len(messages))) 
                    if messages[i]['role'] == 'assistant' and 'tool_calls' in messages[i]), None)
                
                if last_assistant_index is not None:
                    tool_call_count = len(messages[last_assistant_index]['tool_calls'])
                    tool_response_count = sum(1 for msg in messages[last_assistant_index+1:] 
                                           if msg['role'] == 'tool')
                    
                    if tool_call_count != tool_response_count:
                        await self.cleanup_incomplete_tool_calls(thread_id)

            # Convert ToolResult instances to strings
            for key, value in message_data.items():
                if isinstance(value, ToolResult):
                    message_data[key] = str(value)

            # Handle image attachments
            if images:
                if isinstance(message_data['content'], str):
                    message_data['content'] = [{"type": "text", "text": message_data['content']}]
                elif not isinstance(message_data['content'], list):
                    message_data['content'] = []

                for image in images:
                    image_content = {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{image['content_type']};base64,{image['base64']}",
                            "detail": "high"
                        }
                    }
                    message_data['content'].append(image_content)

            messages.append(message_data)
            thread_data["messages"] = messages
            
            with open(thread_path, 'w') as f:
                json.dump(thread_data, f)

            # Write to history file
            try:
                with open(history_path, 'r') as f:
                    history_data = json.load(f)
            except FileNotFoundError:
                history_data = {"messages": []}
            history_data["messages"].append(message_data)
            with open(history_path, 'w') as f:
                json.dump(history_data, f)
            
            logging.info(f"Message added to thread {thread_id} and history: {message_data}")
        except Exception as e:
            logging.error(f"Failed to add message to thread {thread_id}: {e}")
            raise e

    async def list_messages(
        self, 
        thread_id: str, 
        hide_tool_msgs: bool = False, 
        only_latest_assistant: bool = False, 
        regular_list: bool = True
    ) -> List[Dict[str, Any]]:
        """Retrieve messages from a thread with optional filtering.
        
        Args:
            thread_id: ID of the thread to retrieve messages from
            hide_tool_msgs: If True, excludes tool messages and tool calls
            only_latest_assistant: If True, returns only the most recent assistant message
            regular_list: If True, only includes standard message types
            
        Returns:
            List of messages matching the filter criteria
            
        Notes:
            - Returns empty list if thread doesn't exist
            - Filters can be combined for different views of the conversation
        """
        thread_path = os.path.join(self.threads_dir, f"{thread_id}.json")
        
        try:
            with open(thread_path, 'r') as f:
                thread_data = json.load(f)
            messages = thread_data["messages"]
            
            if only_latest_assistant:
                for msg in reversed(messages):
                    if msg.get('role') == 'assistant':
                        return [msg]
                return []
            
            filtered_messages = messages
            
            if hide_tool_msgs:
                filtered_messages = [
                    {k: v for k, v in msg.items() if k != 'tool_calls'}
                    for msg in filtered_messages
                    if msg.get('role') != 'tool'
                ]
        
            if regular_list:
                filtered_messages = [
                    msg for msg in filtered_messages
                    if msg.get('role') in ['system', 'assistant', 'tool', 'user']
                ]
            
            return filtered_messages
        except FileNotFoundError:
            return []

    async def cleanup_incomplete_tool_calls(self, thread_id: str):
        """Clean up incomplete tool calls in a thread.
        
        Args:
            thread_id: ID of the thread to clean up
            
        Returns:
            bool: True if cleanup was performed, False otherwise
            
        Notes:
            - Adds failure results for incomplete tool calls
            - Maintains thread consistency after interruptions
        """
        messages = await self.list_messages(thread_id)
        last_assistant_message = next((m for m in reversed(messages) 
            if m['role'] == 'assistant' and 'tool_calls' in m), None)

        if last_assistant_message:
            tool_calls = last_assistant_message.get('tool_calls', [])
            tool_responses = [m for m in messages[messages.index(last_assistant_message)+1:] 
                            if m['role'] == 'tool']

            if len(tool_calls) != len(tool_responses):
                failed_tool_results = []
                for tool_call in tool_calls[len(tool_responses):]:
                    failed_tool_result = {
                        "role": "tool",
                        "tool_call_id": tool_call['id'],
                        "name": tool_call['function']['name'],
                        "content": "ToolResult(success=False, output='Execution interrupted. Session was stopped.')"
                    }
                    failed_tool_results.append(failed_tool_result)

                assistant_index = messages.index(last_assistant_message)
                messages[assistant_index+1:assistant_index+1] = failed_tool_results

                thread_path = os.path.join(self.threads_dir, f"{thread_id}.json")
                with open(thread_path, 'w') as f:
                    json.dump({"messages": messages}, f)

                return True
        return False

    async def run_thread(
        self,
        thread_id: str,
        system_message: Dict[str, Any],
        model_name: str,
        temperature: float = 0,
        max_tokens: Optional[int] = None,
        tool_choice: str = "auto",
        temporary_message: Optional[Dict[str, Any]] = None,
        native_tool_calling: bool = False,
        xml_tool_calling: bool = False,
        execute_tools: bool = True,
        stream: bool = False,
        execute_tools_on_stream: bool = False,
        parallel_tool_execution: bool = False,
        tool_parser: Optional[ToolParserBase] = None,
        tool_executor: Optional[ToolExecutorBase] = None,
        results_adder: Optional[ResultsAdderBase] = None,
        agentops_session: Any = None,  # Add agentops_session parameter
        stop_sequences: List[str] = None  # Updated parameter
    ) -> Union[Dict[str, Any], AsyncGenerator]:
        """Run a conversation thread with specified parameters.
        
        Args:
            thread_id: ID of the thread to run
            system_message: System message for the conversation
            model_name: Name of the LLM model to use
            temperature: Model temperature (0-1)
            max_tokens: Maximum tokens in response
            tool_choice: Tool selection strategy ("auto" or "none")
            temporary_message: Optional message to include temporarily
            native_tool_calling: Whether to use native LLM function calling
            xml_tool_calling: Whether to use XML-based tool calling
            execute_tools: Whether to execute tool calls
            stream: Whether to stream the response
            execute_tools_on_stream: Whether to execute tools during streaming
            parallel_tool_execution: Whether to execute tools in parallel
            tool_parser: Custom tool parser implementation
            tool_executor: Custom tool executor implementation
            results_adder: Custom results adder implementation
            
        Returns:
            Union[Dict[str, Any], AsyncGenerator]: Response or stream
            
        Raises:
            ValueError: If incompatible tool calling options are specified
            Exception: For other execution failures
            
        Notes:
            - Cannot use both native and XML tool calling simultaneously
            - Streaming responses include both content and tool results
        """
        # Validate tool calling configuration
        if native_tool_calling and xml_tool_calling:
            raise ValueError("Cannot use both native LLM tool calling and XML tool calling simultaneously")

        # Initialize tool components if any tool calling is enabled
        if native_tool_calling or xml_tool_calling:
            if tool_parser is None:
                tool_parser = XMLToolParser(tool_registry=self.tool_registry) if xml_tool_calling else StandardToolParser()
            
            if tool_executor is None:
                tool_executor = XMLToolExecutor(parallel=parallel_tool_execution, tool_registry=self.tool_registry) if xml_tool_calling else StandardToolExecutor(parallel=parallel_tool_execution)
            
            if results_adder is None:
                results_adder = XMLResultsAdder(self) if xml_tool_calling else StandardResultsAdder(self)

        try:
            messages = await self.list_messages(thread_id)
            
            # temporary fix
            if messages and messages[-1].get('role') == 'assistant':
                messages.append({
                    "role": "user",
                    "content": "Continue! You must always use a tool."
                })
                
            prepared_messages = [system_message] + messages
            if temporary_message:
                prepared_messages.append(temporary_message)

            openapi_tool_schemas = None
            if native_tool_calling:
                openapi_tool_schemas = self.tool_registry.get_openapi_schemas()
                available_functions = self.tool_registry.get_available_functions()
            elif xml_tool_calling:
                available_functions = self.tool_registry.get_available_functions()
            else:
                available_functions = {}

            response_processor = LLMResponseProcessor(
                thread_id=thread_id,
                available_functions=available_functions,
                add_message_callback=self.add_message,
                update_message_callback=self._update_message,
                list_messages_callback=self.list_messages,
                parallel_tool_execution=parallel_tool_execution,
                threads_dir=self.threads_dir,
                tool_parser=tool_parser,
                tool_executor=tool_executor,
                results_adder=results_adder
            )

            llm_response = await self._run_thread_completion(
                messages=prepared_messages,
                model_name=model_name,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=openapi_tool_schemas,
                tool_choice=tool_choice if native_tool_calling else None,
                stream=stream,
                agentops_session=agentops_session,
                stop_sequences=stop_sequences  # Pass stop_sequences
            )

            if stream:
                return response_processor.process_stream(
                    response_stream=llm_response,
                    execute_tools=execute_tools,
                    execute_tools_on_stream=execute_tools_on_stream
                )

            await response_processor.process_response(
                response=llm_response,
                execute_tools=execute_tools
            )

            return llm_response

        except Exception as e:
            logging.error(f"Error in run_thread: {str(e)}")
            return {
                "status": "error",
                "message": str(e)
            }

    async def _run_thread_completion(
        self,
        messages: List[Dict[str, Any]],
        model_name: str,
        temperature: float,
        max_tokens: Optional[int],
        tools: Optional[List[Dict[str, Any]]],
        tool_choice: Optional[str],
        stream: bool,
        agentops_session: Any = None,
        stop_sequences: List[str] = None  
    ) -> Union[Any, AsyncGenerator]:
        """Get completion from LLM API."""
        return await make_llm_api_call(
            messages,
            model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            stream=stream,
            agentops_session=agentops_session,
            stop_sequences=stop_sequences 
        )

    async def _update_message(self, thread_id: str, message: Dict[str, Any]):
        """Update an existing message in the thread."""

    async def modify_message(self, thread_id: str, message_index: int, new_message: Dict[str, Any]):
        """Modify a specific message in the thread by its index.

        Args:
            thread_id: The ID of the thread
            message_index: The index of the message to modify
            new_message: The new message data
        """
        thread_path = os.path.join(self.threads_dir, f"{thread_id}.json")
        try:
            with open(thread_path, 'r') as f:
                thread_data = json.load(f)

            messages = thread_data["messages"]
            if 0 <= message_index < len(messages):
                messages[message_index] = new_message
                thread_data["messages"] = messages

                with open(thread_path, 'w') as f:
                    json.dump(thread_data, f)

                logging.info(f"Modified message at index {message_index} in thread {thread_id}")
            else:
                logging.error(f"Message index {message_index} out of range for thread {thread_id}")

        except Exception as e:
            logging.error(f"Failed to modify message in thread {thread_id}: {e}")
            raise e

    async def remove_message(self, thread_id: str, message_index: int):
        """Remove a specific message from the thread by its index.

        Args:
            thread_id: The ID of the thread
            message_index: The index of the message to remove
        """
        thread_path = os.path.join(self.threads_dir, f"{thread_id}.json")
        try:
            with open(thread_path, 'r') as f:
                thread_data = json.load(f)

            messages = thread_data["messages"]
            if 0 <= message_index < len(messages):
                messages.pop(message_index)
                thread_data["messages"] = messages

                with open(thread_path, 'w') as f:
                    json.dump(thread_data, f)

                logging.info(f"Removed message at index {message_index} from thread {thread_id}")
            else:
                logging.error(f"Message index {message_index} out of range for thread {thread_id}")

        except Exception as e:
            logging.error(f"Failed to remove message from thread {thread_id}: {e}")
            raise e

    async def add_message_and_run_tools(self, thread_id: str, message_data: Dict[str, Any]) -> None:
        """Add a message to the thread and execute its tool calls immediately.

        For user messages, append tool results to the message content.
        For other roles, add tool results as separate messages.

        Args:
            thread_id: The ID of the thread
            message_data: The message data containing tool calls to execute
        """
        if message_data.get('role') == 'user' and 'tool_calls' in message_data:
            # Execute tools first
            available_functions = self.tool_registry.get_available_functions()
            tool_results = await self.tool_executor.execute_tool_calls(
                tool_calls=message_data['tool_calls'],
                available_functions=available_functions,
                thread_id=thread_id,
                executed_tool_calls=set()
            )

            # Append tool results to user message content
            original_content = message_data['content']
            tool_outputs = "\n".join([
                f"\nTool {result['name']} output: {result['content']}"
                for result in tool_results
            ])
            message_data['content'] = f"{original_content}{tool_outputs}"

            # Remove tool_calls since they're now part of content
            del message_data['tool_calls']

            # Add the modified user message
            await self.add_message(thread_id, message_data)
        else:
            # For non-user messages, keep original behavior
            await self.add_message(thread_id, message_data)

            if 'tool_calls' in message_data:
                available_functions = self.tool_registry.get_available_functions()
                tool_results = await self.tool_executor.execute_tool_calls(
                    tool_calls=message_data['tool_calls'],
                    available_functions=available_functions,
                    thread_id=thread_id,
                    executed_tool_calls=set()
                )

                for result in tool_results:
                    await self.add_message(thread_id, result)

    async def add_to_history_only(self, thread_id: str, message_data: Dict[str, Any]):
        """Add a message only to the history file without affecting the main thread.

        Args:
            thread_id: The ID of the thread
            message_data: The message data to add to history
        """
        history_path = os.path.join(self.threads_dir, f"{thread_id}_history.json")

        try:
            try:
                with open(history_path, 'r') as f:
                    history_data = json.load(f)
            except FileNotFoundError:
                history_data = {"messages": []}

            # Process ToolResult instances
            for key, value in message_data.items():
                if isinstance(value, ToolResult):
                    message_data[key] = str(value)

            history_data["messages"].append(message_data)

            with open(history_path, 'w') as f:
                json.dump(history_data, f)

            logging.info(f"Message added to history of thread {thread_id}")

        except Exception as e:
            logging.error(f"Failed to add message to history of thread {thread_id}: {e}")
            raise e

    async def execute_tool_and_add_message(self, thread_id: str, role: str, tool_name: str, arguments: Dict[str, Any]):
        """Execute a tool and add its output as a message with role 'tool'."""
        available_functions = self.tool_registry.get_available_functions()
        if tool_name not in available_functions:
            raise ValueError(f"Tool {tool_name} is not registered.")

        # Execute the tool
        tool_function = available_functions[tool_name]
        tool_result = await tool_function(**arguments)

        # Prepare the tool message
        tool_message = {
            "role": role,
            # "name": tool_name,
            "content": str(tool_result)
        }

        # Add the tool message to the thread
        await self.add_message(thread_id, tool_message)

    async def reset_messages(self, thread_id: str):
        """Reset (empty) the messages list for a thread while preserving history.

        Args:
            thread_id: The ID of the thread to reset

        Raises:
            FileNotFoundError: If thread doesn't exist
            Exception: For other operation failures
        """
        thread_path = os.path.join(self.threads_dir, f"{thread_id}.json")

        empty_data = {"messages": []}

        try:
            # Reset only main thread file, preserving history
            with open(thread_path, 'w') as f:
                json.dump(empty_data, f)

            logging.info(f"Reset messages for thread {thread_id} (history preserved)")

        except FileNotFoundError:
            logging.error(f"Thread {thread_id} not found")
            raise
        except Exception as e:
            logging.error(f"Failed to reset messages for thread {thread_id}: {e}")
            raise

