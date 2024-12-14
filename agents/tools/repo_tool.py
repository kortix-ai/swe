import asyncio, os, fnmatch, json, shlex
from typing import Optional, List, Dict, Any
from agents.core.tools import Tool, ToolResult, xml_schema
from agents.core.thread_manager import ThreadManager

class BashExecutor:
    def __init__(self, container: str):
        self.container = container

    async def execute(self, cmd: str, inp: Optional[bytes] = None):
        p = await asyncio.create_subprocess_exec(
            'docker', 'exec', '-i', self.container, '/bin/bash', '-c', cmd,
            stdin=asyncio.subprocess.PIPE if inp else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(p.communicate(inp), timeout=120)
        except asyncio.TimeoutError:
            p.kill()
            await p.wait()
            return '', 'Command timed out', 1
        return stdout.decode('utf-8','replace'), stderr.decode('utf-8','replace'), p.returncode

class RepositoryTools(Tool):
    def __init__(self, container: str, thread_manager: ThreadManager):
        super().__init__()
        self.container = container
        self.thread_manager = thread_manager
        self.executor = BashExecutor(container)
        self.workspace_lock = asyncio.Lock()

    async def get_workspace(self) -> Dict[str, Any]:
        async with self.workspace_lock:
            w = await self.thread_manager.get_state("workspace")
            return w if w else {}

    async def set_workspace(self, w: Dict[str, Any]) -> None:
        async with self.workspace_lock:
            await self.thread_manager.set_state("workspace", w)

    def list_directory(self, path: str, depth: int, exclusions: List[str], current_depth: int = 1) -> List[str]:
        if current_depth > depth:
            return []
        entries = []
        try:
            for entry in sorted(os.listdir(path)):
                if entry.startswith('.') or any(fnmatch.fnmatch(os.path.join(path, entry), f"*{pat}") for pat in exclusions):
                    continue
                full_path = os.path.join(path, entry)
                entries.append(full_path)
                if os.path.isdir(full_path):
                    entries.extend(self.list_directory(full_path, depth, exclusions, current_depth + 1))
        except Exception as e:
            # Log and return partial result
            return entries
        return entries

    async def fetch_folder(self, path: str, depth: int, exclusions: Optional[List[str]] = None) -> ToolResult:
        exclusions = exclusions or ['.rst', '.pyc']
        try:
            directory_structure = self.list_directory(path, depth, exclusions)
            xml_output = f'<directory path="{path}">\n' + '\n'.join(directory_structure) + '\n</directory>'
            return self.success_response(xml_output)
        except Exception as e:
            return self.fail_response(str(e))

    @xml_schema(
        tag_name="view_folder",
        mappings=[
            {"param_name": "path", "node_type": "attribute", "path": "path"},
            {"param_name": "depth", "node_type": "attribute", "path": "depth"}
        ]
    )
    async def view_folder(self, path: str, depth: Optional[int]=2) -> ToolResult:
        async with self.workspace_lock:
            w = await self.get_workspace()
            w.setdefault("open_folders", {})
            if path not in w["open_folders"]:
                w["open_folders"][path] = depth or 2
                await self.set_workspace(w)
                return self.success_response(f"Folder {path} added.")
            return self.success_response(f"Folder {path} already open.")

    @xml_schema(tag_name="submit_final_solution_only_if_all_tests_pass", mappings=[])
    async def submit_final_solution_only_if_all_tests_pass(self) -> ToolResult:
        return self.success_response("Task terminated, Agent stopped!")

    @xml_schema(
        tag_name="create_file",
        mappings=[
            {"param_name": "path", "node_type": "attribute", "path": "path"},
            {"param_name": "content", "node_type": "content", "path": "."}
        ]
    )
    async def create_file(self, path: str, content: str) -> ToolResult:
        cmd = f"mkdir -p $(dirname {shlex.quote(path)}) && echo {shlex.quote(content)} > {shlex.quote(path)}"
        stdout, stderr, c = await self.executor.execute(cmd)
        if c==0:
            w=await self.get_workspace()
            w.setdefault("open_files",[])
            if path not in w["open_files"]:
                w["open_files"].append(path)
            await self.set_workspace(w)
            return self.success_response(f"File {path} created.")
        return self.fail_response(stderr)

    @xml_schema(
        tag_name="edit_file",
        mappings=[
            {"param_name": "path", "node_type": "attribute", "path": "."},
            {"param_name": "replacements", "node_type": "element", "path": "replacements"}
        ]
    )
    async def edit_file(self, path: str, replacements) -> ToolResult:
        w=await self.get_workspace()
        if path not in w.get("open_files",[]):
            return self.fail_response("File not open.")
        stdout, stderr, c=await self.executor.execute(f"cat {shlex.quote(path)}")
        if c!=0:return self.fail_response(stderr)
        content=stdout
        rep_val=replacements.get("replacement",[])
        if isinstance(rep_val,dict):rep_val=[rep_val]
        for r in rep_val:
            old,new=r.get("old_string"),r.get("new_string")
            if not old or not new or old not in content:
                return self.fail_response("Invalid replacements.")
            content=content.replace(old,new)
        stdout, stderr, c=await self.executor.execute(f"cat > {shlex.quote(path)}",inp=content.encode())
        if c==0:
            return self.success_response(f"File {path} edited.")
        return self.fail_response(stderr)

    @xml_schema(tag_name="run_bash", mappings=[{"param_name":"command","node_type":"attribute","path":"command"}])
    async def run_bash(self, command: str) -> ToolResult:
        stdout, stderr, c=await self.executor.execute(command)
        out=(stdout+stderr)or"No output."
        if len(out)>15000:
            out=out[:5000]+'\n\n...OUTPUT TRUNCATED...\n\n'+out[-10000:]
        w=await self.get_workspace()
        w.setdefault("last_terminal_session",[]).append({"command":command,"output":out,"success":c==0})
        await self.set_workspace(w)
        return self.success_response(f"Executed:\n{out}")

    @xml_schema(tag_name="open_file", mappings=[{"param_name":"path","node_type":"attribute","path":"."}])
    async def open_file(self, path: str) -> ToolResult:
        w=await self.get_workspace()
        w.setdefault("open_files",[])
        if path not in w["open_files"]:
            w["open_files"].append(path)
            await self.set_workspace(w)
            return self.success_response(f"File {path} opened.")
        return self.success_response(f"File {path} already open.")

    @xml_schema(
        tag_name="track_implementation",
        mappings=[
            {"param_name":"id","node_type":"attribute","path":"id"},
            {"param_name":"status","node_type":"attribute","path":"status"},
            {"param_name":"note","node_type":"content","path":"."}
        ]
    )
    async def track_implementation(self, id: str, status: str, note: Optional[str]=None) -> ToolResult:
        w=await self.get_workspace()
        w.setdefault("implementation_trials",{})
        w["implementation_trials"][id]={"status":status,"note":note or ""}
        await self.set_workspace(w)
        return self.success_response(f"Trial {id} updated to {status}.")
