import pytest
import asyncio
import os
import json
import logging

from agents.core.thread_manager import ThreadManager

@pytest.mark.asyncio
async def test_thread_creation_and_message_handling(tmp_path):
    logging.basicConfig(level=logging.INFO)
    store_file = str(tmp_path / "state.json")
    threads_dir = str(tmp_path / "threads")

    manager = ThreadManager(store_file=store_file, threads_dir=threads_dir)
    thread_id = await manager.create_thread()
    assert os.path.exists(os.path.join(threads_dir, f"{thread_id}.json"))

    # Add a message
    message = {"role": "user", "content": "Hello!"}
    await manager.add_message(thread_id, message)

    # List messages
    msgs = await manager.list_messages(thread_id)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "Hello!"

    # Check state storage
    await manager.set_state("test_key", {"value": 123})
    val = await manager.get_state("test_key")
    assert val == {"value": 123}

    # Export and verify store
    exported = await manager.export_store()
    assert "test_key" in exported
    assert exported["test_key"] == {"value": 123}

    # Delete state and verify
    await manager.delete_state("test_key")
    val = await manager.get_state("test_key")
    assert val is None

    # Clear store
    await manager.clear_store()
    exported = await manager.export_store()
    assert exported == {}


@pytest.mark.asyncio
async def test_list_messages_filters(tmp_path):
    store_file = str(tmp_path / "state.json")
    threads_dir = str(tmp_path / "threads")
    manager = ThreadManager(store_file=store_file, threads_dir=threads_dir)
    thread_id = await manager.create_thread()

    await manager.add_message(thread_id, {"role": "system", "content": "System message"})
    await manager.add_message(thread_id, {"role": "user", "content": "User asks"})
    await manager.add_message(thread_id, {"role": "assistant", "content": "Assistant replies"})
    await manager.add_message(thread_id, {"role": "tool", "content": "Tool message"})

    # Hide tool messages
    msgs = await manager.list_messages(thread_id, hide_tool_msgs=True)
    roles = [m["role"] for m in msgs]
    assert "tool" not in roles

    # only latest assistant
    msgs = await manager.list_messages(thread_id, only_latest_assistant=True)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"
