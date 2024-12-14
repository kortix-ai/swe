from typing import Any, Dict, List, Optional, Union
from asyncio import Lock
from contextlib import asynccontextmanager
import os, json, logging, uuid
import aiofiles
from agentpress.tools import ToolRegistry
from agentpress.processors import XMLToolParser, XMLToolExecutor, XMLResultsAdder, LLMResponseProcessor
from agentpress.llm import make_llm_api_call

class ThreadManager:
    def __init__(self, store_file="state.json", threads_dir="threads"):
        self.lock = Lock()
        self.store_file = store_file
        self.threads_dir = threads_dir
        os.makedirs(self.threads_dir, exist_ok=True)
        logging.info(f"Initialized with store {store_file} and threads dir {threads_dir}")
        self.tool_registry = ToolRegistry()

    @asynccontextmanager
    async def store_scope(self):
        async with self.lock:
            if os.path.exists(self.store_file):
                async with aiofiles.open(self.store_file, 'r') as f:
                    content = await f.read()
                    store = json.loads(content) if content.strip() else {}
            else:
                store = {}
            yield store
            async with aiofiles.open(self.store_file, 'w') as f:
                await f.write(json.dumps(store, indent=2))
            logging.debug("Store saved")

    async def set_state(self, key: str, data: Any) -> Any:
        async with self.store_scope() as store:
            store[key] = data
            logging.info(f'Set state {key}')
            return data

    async def get_state(self, key: str) -> Any:
        async with self.store_scope() as store:
            data = store.get(key)
            logging.info(f'Get state {key}: {data}')
            return data

    async def delete_state(self, key: str):
        async with self.store_scope() as store:
            store.pop(key, None)
            logging.info(f'Deleted state {key}')

    async def export_store(self) -> dict:
        async with self.store_scope() as store:
            return store

    async def clear_store(self):
        async with self.store_scope() as store:
            store.clear()
            logging.info("Store cleared")

    async def create_thread(self) -> str:
        async with self.lock:
            thread_id = str(uuid.uuid4())
            thread_path = os.path.join(self.threads_dir, f"{thread_id}.json")
            history_path = os.path.join(self.threads_dir, f"{thread_id}_history.json")
            empty = {"messages": []}
            async with aiofiles.open(thread_path, 'w') as f:
                await f.write(json.dumps(empty))
            async with aiofiles.open(history_path, 'w') as f:
                await f.write(json.dumps(empty))
            return thread_id

    async def add_message(self, thread_id: str, message: Dict[str, Any], images: Optional[List[Dict[str, Any]]] = None):
        thread_path = os.path.join(self.threads_dir, f"{thread_id}.json")
        history_path = os.path.join(self.threads_dir, f"{thread_id}_history.json")
        if not os.path.exists(thread_path) or not os.path.exists(history_path):
            logging.error(f"Thread {thread_id} does not exist.")
            raise FileNotFoundError(f"Thread {thread_id} does not exist.")

        async with aiofiles.open(thread_path, 'r') as f:
            thread_content = await f.read()
        thread = json.loads(thread_content) if thread_content.strip() else {"messages": []}

        msg = message.copy()
        if images:
            if isinstance(msg.get('content'), str):
                msg['content'] = [{"type": "text", "text": msg['content']}]
            elif not isinstance(msg.get('content'), list):
                msg['content'] = []
            for img in images:
                msg['content'].append({"type": "image_url", "image_url": {"url": f"data:{img['content_type']};base64,{img['base64']}", "detail": "high"}})

        thread['messages'].append(msg)
        async with aiofiles.open(thread_path, 'w') as f:
            await f.write(json.dumps(thread))

        async with aiofiles.open(history_path, 'r') as f:
            history_content = await f.read()
        history = json.loads(history_content) if history_content.strip() else {"messages": []}
        history['messages'].append(msg)
        async with aiofiles.open(history_path, 'w') as f:
            await f.write(json.dumps(history))
        logging.info(f"Added message to thread {thread_id}")

    async def list_messages(self, thread_id: str, hide_tool_msgs: bool = False, only_latest_assistant: bool = False, regular_list: bool = True) -> List[Dict[str, Any]]:
        thread_path = os.path.join(self.threads_dir, f"{thread_id}.json")
        if not os.path.exists(thread_path):
            logging.warning(f"Thread {thread_id} not found.")
            return []
        async with aiofiles.open(thread_path, 'r') as f:
            content = await f.read()
        thread = json.loads(content) if content.strip() else {"messages": []}
        msgs = thread.get('messages', [])

        if only_latest_assistant:
            for msg in reversed(msgs):
                if msg.get('role') == 'assistant':
                    return [msg]
            return []

        if hide_tool_msgs:
            msgs = [m for m in msgs if m.get('role') != 'tool']

        if regular_list:
            allowed_roles = {'system','assistant','tool','user'}
            msgs = [m for m in msgs if m.get('role') in allowed_roles]

        return msgs

    async def run_thread(
        self, thread_id: str,
        system_message: Dict[str, Any],
        model_name: str,
        temperature: float = 0,
        max_tokens: Optional[int] = None,
        execute_tools: bool = True,
        parallel_tool_execution: bool = True,
        stop_sequences: List[str] = None
    ) -> Union[Dict[str, Any], Any]:
        try:
            msgs = await self.list_messages(thread_id)
            if msgs and msgs[-1].get('role') == 'assistant':
                msgs.append({"role": "user", "content": "Continue! You must always use a tool."})

            prepared = [system_message] + msgs
            tool_parser = XMLToolParser(self.tool_registry)
            tool_executor = XMLToolExecutor(parallel=parallel_tool_execution, tool_registry=self.tool_registry)
            results_adder = XMLResultsAdder(self)
            processor = LLMResponseProcessor(thread_id, self.tool_registry.get_available_functions(), self, parallel_tool_execution, tool_parser, tool_executor, results_adder)

            llm_resp = await make_llm_api_call(
                prepared, model_name,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=None,
                tool_choice=None,
                stop_sequences=stop_sequences
            )

            await processor.process_response(llm_resp, execute_tools)
            return llm_resp
        except Exception as e:
            logging.error(f"Run thread error: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}