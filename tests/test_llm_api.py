import pytest
import asyncio
from agents.core.llm import make_llm_api_call
import logging
logging.basicConfig(level=logging.ERROR)

logging.getLogger("litellm").disabled = True
logging.getLogger("langfuse").disabled = True

@pytest.mark.asyncio
async def test_llm_api_with_all_params():
    print("\n🔍 Testing API call with all parameters...")
    
    messages = [
        {"role": "system", "content": "You are a creative and concise assistant."},
        {"role": "user", "content": "Write a short poem about quantum computing."}
    ]
    model_name = "openrouter/qwen/qwq-32b-preview"
    
    response = await make_llm_api_call(
        messages, 
        model_name,
        max_tokens=100,
        temperature=0.7,
    )
    
    print("\n🤖 Response:")
    print(response['choices'][0]['message']['content'])
    print("\n✨ Test completed.\n")
    
    # Assertions
    assert response is not None
    assert 'choices' in response
    content = response['choices'][0]['message']['content']
    assert content
