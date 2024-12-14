from typing import Union, Dict, Any
import litellm, os, json, openai, asyncio, logging
from openai import OpenAIError
from langfuse.decorators import langfuse_context
from agents.core.tools import ToolRegistry

OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', None)

LANGFUSE_PUBLIC_KEY = os.getenv('LANGFUSE_PUBLIC_KEY', '')
LANGFUSE_SECRET_KEY = os.getenv('LANGFUSE_SECRET_KEY', '')
LANGFUSE_HOST = os.getenv('LANGFUSE_HOST', 'https://cloud.langfuse.com')

os.environ['LANGFUSE_PUBLIC_KEY'] = LANGFUSE_PUBLIC_KEY
os.environ['LANGFUSE_SECRET_KEY'] = LANGFUSE_SECRET_KEY
os.environ['LANGFUSE_HOST'] = LANGFUSE_HOST
os.environ['LITELLM_LOG'] = 'ERROR'
os.environ['LANGFUSE_LOG_LEVEL'] = 'ERROR'

litellm.success_callback = ["langfuse"]
litellm.failure_callback = ["langfuse"]


registry = ToolRegistry()

async def make_llm_api_call(messages: list, model_name: str, response_format: Any = None, temperature: float = 0, max_tokens: int = None, tools: list = None, tool_choice: str = "auto", api_key: str = None, api_base: str = None, agentops_session: Any = None, top_p: float = None, stop_sequences: list = None) -> Union[Dict[str, Any], Any]:
    litellm.set_verbose = False

    async def attempt_api_call(api_call_func, max_attempts=3):
        for attempt in range(max_attempts):
            try:
                return await api_call_func()
            except litellm.exceptions.RateLimitError:
                logging.warning("Rate limit exceeded. Waiting 30s before retry...")
                await asyncio.sleep(30)
            except OpenAIError as e:
                logging.info(f"API call failed, retry {attempt+1}. Error: {e}")
                await asyncio.sleep(5)
            except json.JSONDecodeError:
                logging.error("JSON decoding failed, retrying...")
                await asyncio.sleep(5)
        raise Exception("API call failed after retries.")

    async def api_call():
        trace_id = langfuse_context.get_current_trace_id()
        metadata = {"trace_id": trace_id} if trace_id else {}
        params = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
            "response_format": response_format,
            "top_p": top_p,
            "metadata": metadata
        }
        if stop_sequences:
            params["stop_sequences"] = stop_sequences
        if api_key:
            params["api_key"] = api_key
        if api_base:
            params["api_base"] = api_base
        if 'o1' in model_name and max_tokens:
            params["max_completion_tokens"] = max_tokens
        elif max_tokens:
            params["max_tokens"] = max_tokens
        if tools:
            params["tools"] = tools
            params["tool_choice"] = tool_choice
        if "claude" in model_name.lower() or "anthropic" in model_name.lower():
            params["extra_headers"] = {"anthropic-beta": "max-tokens-3-5-sonnet-2024-07-15"}
            processed = []
            for m in messages:
                m_copy = m.copy()
                m_copy["content"] = [{"type": "text", "text": m["content"], "cache_control": {"type": "ephemeral"}}] if isinstance(m["content"], str) else m["content"]
                processed.append(m_copy)
            params["messages"] = processed
        return await (agentops_session.patch(litellm.acompletion)(**params) if agentops_session else litellm.acompletion(**params))

    return await attempt_api_call(api_call)
