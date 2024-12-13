import pytest
import asyncio
from agents.core.llm import make_llm_api_call
import logging
logging.getLogger("litellm").disabled = True
logging.getLogger("langfuse").disabled = True

@pytest.mark.asyncio
async def test_llm_api_basic():
    print("\n🔍 Testing basic API call...")
    
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Complex essay on economics"}
    ]
    model_name = "openrouter/qwen/qwq-32b-preview"
    
    response = await make_llm_api_call(messages, model_name)
    
    print("\n🤖 Response:")
    print(response['choices'][0]['message']['content'])
    print("\n✨ Test completed.\n")
    
    assert response is not None
    assert 'choices' in response
    assert response['choices'][0]['message']['content']
