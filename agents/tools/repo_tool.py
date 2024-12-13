import asyncio, base64, shlex
from typing import Optional
from agents.core.tools import Tool, ToolResult, xml_schema
from agents.core.thread_manager import ThreadManager

class BashExecutor:
    def __init__(self, container: str):
        self.container = container

    async def execute(self, cmd: str, inp=None):
        p = await asyncio.create_subprocess_exec(
            'docker', 'exec', '-i', self.container, '/bin/bash', '-c', cmd,
            stdin=asyncio.subprocess.PIPE if inp else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(p.communicate(inp), timeout=120)
        except asyncio.TimeoutError:
            p.kill()
            return '', 'Command timed out', 1
        return stdout.decode('utf-8','replace'), stderr.decode('utf-8','replace'), p.returncode

class RepositoryTools(Tool):
    def __init__(self, container: str, thread_manager: ThreadManager):
        super().__init__()
        self.container = container
        self.thread_manager = thread_manager
        self.executor = BashExecutor(container)

    async def get_workspace(self):
        w = await self.thread_manager.get_state("workspace")
        return w if w else {}

    async def set_workspace(self, w):
        await self.thread_manager.set_state("workspace", w)

    async def fetch_folder(self, path: str, depth: int):
        code = base64.b64encode(b'''
import os, fnmatch, sys
def exclude(p,pats):return any(fnmatch.fnmatch(p,"*"+pat)for pat in pats)
def list_dir(p,d,e,c=1):
    r=[]
    try:
        for it in sorted(os.listdir(p)):
            if it.startswith('.')or exclude(os.path.join(p,it),e):continue
            fp=os.path.join(p,it)
            r.append(fp)
            if os.path.isdir(fp)and c<d:r.extend(list_dir(fp,d,e,c+1))
    except:pass
    return r
p,excl,depth=sys.argv[1],sys.argv[2].split(','),int(sys.argv[3])
print(f'<directory path="{p}">')
for i in list_dir(p,depth,excl):print(i)
print('</directory>')
''').decode()
        cmd = f"echo {shlex.quote(code)}|base64 -d|python3 - {shlex.quote(path)} {shlex.quote('.rst,.pyc')} {depth}"
        stdout, stderr, c = await self.executor.execute(cmd)
        if c==0 and not stderr.strip():
            return self.success_response(stdout.strip())
        return self.fail_response(stderr.strip())

    @xml_schema(
        tag_name="view_folder",
        mappings=[
            {"param_name": "path", "node_type": "attribute", "path": "path"},
            {"param_name": "depth", "node_type": "attribute", "path": "depth"}
        ]
    )
    async def view_folder(self, path: str, depth: Optional[int]=2) -> ToolResult:
        w = await self.get_workspace()
        w.setdefault("open_folders", {})
        if path not in w["open_folders"]:
            w["open_folders"][path] = depth or 2
            await self.set_workspace(w)
            return self.success_response(f"Folder {path} added.")
        return self.success_response(f"Folder {path} already open.")

    @xml_schema(tag_name="SUBMIT_FINAL_SOLUTION_ONLY_IF_ALL_TESTS_PASS", mappings=[])
    async def SUBMIT_FINAL_SOLUTION_ONLY_IF_ALL_TESTS_PASS(self) -> ToolResult:
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
