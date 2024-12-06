import asyncio
import base64
import shlex
import json
import re
import os
import tiktoken
from agentpress.tool import Tool, ToolResult, openapi_schema, xml_schema
from agentpress.state_manager import StateManager
from typing import List, Optional

def transform_string_to_dict(input_string):
    """
    Transform a string containing replacement tags into a dictionary format using regex.
    
    Args:
        input_string (str): Input string with XML-like tags
        
    Returns:
        dict: Transformed dictionary with replacement information
    """
    # Pattern to match old_string and new_string content
    pattern = r'<old_string>(.*?)</old_string>\s*<new_string>(.*?)</new_string>'
    
    # Use re.DOTALL flag to make dot match newlines
    matches = re.finditer(pattern, input_string, re.DOTALL)
    
    replacements = []
    for match in matches:
        old_string = match.group(1)
        new_string = match.group(2)
        
        replacement = {
            "old_string": old_string,
            "new_string": new_string
        }
        replacements.append(replacement)
    
    return {"replacement": replacements}

class BashExecutor:
    """Executes bash commands in Docker container using individual exec calls."""
    
    def __init__(self, container_name: str):
        self.container_name = container_name
        
    async def execute(self, command: str, input_data: Optional[bytes] = None) -> tuple[str, str, int]:
        """Execute a command in the container using docker exec."""
        try:
            # Ensure we're in /testbed and have conda environment
            wrapped_command = (
                f'. /opt/miniconda3/etc/profile.d/conda.sh && '
                f'conda activate testbed && '
                f'cd /testbed && '
                f'set -o pipefail && '
                f'{command}'
            )
            
            # Use docker exec directly for each command
            cmd = [
                'docker', 'exec',
                '-i',  # Interactive mode
                self.container_name,
                '/bin/bash', '-c', wrapped_command
            ]
            
            # Execute the command
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if input_data else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Wait for command completion with timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=input_data),
                    timeout=120  # 2 minute timeout
                )
            except asyncio.TimeoutError:
                try:
                    process.terminate()
                except:
                    pass
                return '', 'Command execution timed out after 2 minutes', 1
                
            # Decode outputs with UTF-8 encoding and replace errors
            stdout_str = stdout.decode('utf-8', errors='replace').strip() if stdout else ''
            stderr_str = stderr.decode('utf-8', errors='replace').strip() if stderr else ''
            
            # Handle empty output
            if not stdout_str and not stderr_str and process.returncode == 0:
                stdout_str = "Command completed successfully but produced no output"
                
            return stdout_str, stderr_str, process.returncode
                
        except Exception as e:
            return '', f"Error executing command: {str(e)}", 1

class RepositoryTools(Tool):
    def __init__(self, container_name: str, state_manager: StateManager):
        super().__init__()
        self.state_manager = state_manager
        self.container_name = container_name
        self._bash_executor = BashExecutor(container_name)

    async def _init_workspace(self):
        """Initialize the workspace state with empty structures if not already initialized."""
        workspace = await self.state_manager.get("workspace")
        if workspace is None:
            workspace = {
                "open_folders": {},        # Dictionary with folder paths as keys and depths as values
                "open_files": [],          # List of paths to open files
                "last_terminal_session": [],    # Current terminal session output (last N commands)
            }
            
            # Find and add only main source code folders with depth 3
            exclude_dirs = ['tests', 'doc', 'docs', 'examples', 
                            'utils', 'tools', 'egg-info', 'build', 'dist',
                            '__pycache__', '.git', '.github', 'licenses', 'scripts', 'script', 'extras', 'properties', 'asv', 'ci', 'extern', 'lib', 'galleries', 'requirements', 'tmp',
                            '.devcontainer', 'ext', '.binder', 'design_notes', 'bench', 'changelog', '.circleci', '.spin' , 'benchmark', 'bin',
                            'data', 'release'
                            ]
            
            # Command to list directories in /testbed
            cmd = 'ls -d /testbed/*/'
            stdout, stderr, returncode = await self._bash_executor.execute(cmd)
            
            folders = [f for f in stdout.splitlines() if not any(exclude_dir in f for exclude_dir in exclude_dirs)]

            # Add initial view of /testbed with depth 1
            workspace["open_folders"]["/testbed"] = 1

            # Check if tests/ is a folder of /testbed and add it with depth 1
            if '/testbed/tests/' in folders:
                workspace["open_folders"]["/testbed/tests"] = 2

            if len(folders) == 1:
                workspace["open_folders"][folders[0]] = 2
            else: 
                self.fail_response(f"Error finding main source code folder: {stderr}")
            
            await self.state_manager.set("workspace", workspace)

    async def _update_terminal(self, command: str, output: Optional[str] = None, success: Optional[bool] = None):
        """Update terminal session with new command and optionally outputs."""
        workspace = await self.state_manager.get("workspace")
        if "last_terminal_session" not in workspace:
            workspace["last_terminal_session"] = []
        # Add new command to terminal session
        workspace["last_terminal_session"].append({
            "command": command,
            "output": output,
            "success": success,
        })
        await self.state_manager.set("workspace", workspace)

    async def execute_command_in_container(self, command: str):
        """
        Executes a given bash command inside the specified Docker container.
        
        Parameters:
            command (str): The bash command to execute.
        
        Returns:
            tuple: (stdout, stderr, returncode)
        """
        # MUST keep this env activation command
        full_command = (
            f'. /opt/miniconda3/etc/profile.d/conda.sh && '
            f'conda activate testbed && {command}'
        )
        cmd = ['docker', 'exec', self.container_name, 'bash', '-c', full_command]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        # Decode outputs with UTF-8 encoding and replace errors
        stdout_decoded = stdout.decode('utf-8', errors='replace')
        stderr_decoded = stderr.decode('utf-8', errors='replace')
        return stdout_decoded, stderr_decoded, process.returncode

    async def _extract_file_content(self, output: str) -> str:
        """Extract file content from view output."""
        content_lines = []
        in_file_content = False
        
        for line in output.strip().split('\n'):
            if line.startswith('<file'):
                in_file_content = True
                continue
            elif line.startswith('</file'):
                in_file_content = False
                continue
            if in_file_content and '\t' in line:
                content_lines.append(line.split('\t', 1)[1])
        
        return '\n'.join(content_lines)

    async def format_workspace_xml(self) -> str:
        """Format the workspace into an XML string for the Agent."""
        workspace = await self.state_manager.get("workspace")
        xml_output = "<workspace>\n"
        # Include content from open folders with their specified depths
        for path, depth in workspace["open_folders"].items():
            result = await self._fetch_folder_contents(path=path, depth=depth)
            if result.success:
                xml_output += f"{result.output}\n"

        if "implementation_trials" in workspace:
            xml_output += "<IMPLEMENTATION_TRAILS>\n"
            for trial_id, data in workspace["implementation_trials"].items():
                status = data.get("status", "")
                note = data.get("note", "")
                xml_output += f'<implementation_trial id="{trial_id}" status="{status}">\n{note}\n</implementation_trial>\n'
            xml_output += "</IMPLEMENTATION_TRAILS>\n"

        # use reversed order because we want important files to be at the end
        debug_files = []
        for file_path in reversed(workspace["open_files"]):
            command = f"cat {file_path}"
            stdout, stderr, returncode = await self._bash_executor.execute(command)
            if returncode == 0:
                # remove (/testbed)
                MAX_LENGTH = 30000 if "test" in file_path[9:] else 100000
                if len(stdout) > MAX_LENGTH:
                    stdout = stdout[:MAX_LENGTH] + "\n... File content truncated due to length ... \n"
                xml_output += f'<file path="{file_path}">\n{stdout}\n</file>\n'
                debug_files.append((file_path, len(tiktoken.get_encoding("cl100k_base").encode(stdout))))
            else:
                xml_output += f'<!-- Error reading file {file_path}: {stderr} -->\n'

        # add <current_changes> (result of "git diff")
        stdout, stderr, returncode = await self._bash_executor.execute('git diff')
        xml_output += f"<last_try>\n" 
        xml_output += "<last_terminal_session>\n"
        for session_entry in workspace.get("last_terminal_session", []):
            xml_output += f"<bash_command_executed command=\"{session_entry['command']}\">\n"
            xml_output += f"{session_entry['output']}\n"
            xml_output += "</bash_command_executed>\n"

        if "latest_failures" in workspace and workspace["latest_failures"]:
            xml_output += "<latest_failures>\n"
            for failure_message in workspace["latest_failures"]:
                xml_output += f"<failure>{failure_message}</failure>\n"
            xml_output += "</latest_failures>\n"
            workspace["latest_failures"] = []
            await self.state_manager.set("workspace", workspace)
        xml_output += "</last_terminal_session>\n"
        xml_output += f"<git_diff>{stdout}</git_diff>\n"
        xml_output += "</last_try>\n"

        xml_output += "</workspace>\n"

        # reset terminal session
        workspace["last_terminal_session"] = []

        return xml_output

    async def _fetch_folder_contents(self, path: str, depth: Optional[int]) -> ToolResult:
        """Fetch the contents of a folder."""
        try:
            exclude_patterns = ['.rst', '.pyc']
            python_code = '''
import os
import fnmatch
import sys
from typing import List

def should_exclude(path: str, patterns: List[str]) -> bool:
    return any(fnmatch.fnmatch(path, f"*{pattern}") for pattern in patterns)

def list_directory(root_path: str, depth: int, exclude_patterns: List[str], current_depth: int = 1) -> List[str]:
    results = []
    try:
        for item in sorted(os.listdir(root_path)):
            if item.startswith('.'):
                continue
                
            full_path = os.path.join(root_path, item)
            if should_exclude(full_path, exclude_patterns):
                continue
                
            results.append(full_path)
            if os.path.isdir(full_path) and current_depth < depth:
                results.extend(list_directory(full_path, depth, exclude_patterns, current_depth + 1))
    except PermissionError:
        print(f"Permission denied: {root_path}", file=sys.stderr)
    except Exception as e:
        print(f"Error accessing {root_path}: {str(e)}", file=sys.stderr)
    return results

def view_path(path: str, depth: int, exclude_patterns: List[str]):
    if os.path.isdir(path):
        print(f'<directory path="{path}">')
        for item in list_directory(path, depth, exclude_patterns):
            print(item)
        print('</directory>')
    else:
        print(f"The path '{path}' is not a directory.", file=sys.stderr)

def main():
    path = sys.argv[1]
    exclude_patterns = sys.argv[2].split(',')
    depth = int(sys.argv[3])
    view_path(path.strip(), depth, exclude_patterns)

if __name__ == '__main__':
    main()
'''
            # Encode the Python script and arguments
            code_base64 = base64.b64encode(python_code.encode('utf-8')).decode('ascii')
            exclude_str = ','.join(exclude_patterns)
            # Command to execute the Python script in the container
            command = (
                f"echo {repr(code_base64)} | base64 -d | "
                f"python3 - {repr(path)} {repr(exclude_str)} {depth}"
            )
            stdout, stderr, returncode = await self.execute_command_in_container(command)
            success = returncode == 0

            if success and not stderr.strip():
                return self.success_response(stdout.strip())
            else:
                return self.fail_response(f"Error fetching folder contents: {stderr.strip()}")
        except Exception as e:
            return self.fail_response(f"Exception during folder content fetch: {str(e)}")

    @openapi_schema({
        "type": "function",
        "function": {
            "name": "view_folder",
            "description": (
                "Add a directory to the workspace to view its contents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The directory path to add to the workspace."
                    },
                    "depth": {
                        "type": "integer",
                        "description": "The maximum directory depth to search for contents.",
                        "default": 2
                    },
                },
                "required": ["path"]
            }
        }
    })
    @xml_schema(
        tag_name="view_folder",
        mappings=[
            {"param_name": "path", "node_type": "attribute", "path": "path"},
            {"param_name": "depth", "node_type": "attribute", "path": "depth"}
        ],
        example='''
        <!-- Repository View Folder Tool -->
        <!-- Add directory to workspace to view its contents -->

        <!-- Parameters Description:
             - path: Directory path to add to workspace (REQUIRED)
             - depth: Maximum directory depth to search for contents (optional)
        -->

        <!-- Add directory to workspace with depth -->
        <view_folder path="/testbed" depth="2" />

        <!-- Important Notes:
             - Path should be absolute path from repository root
             - Hidden files and directories are automatically excluded
        -->
        '''
    )
    async def view_folder(self, path: str, depth: Optional[int] = 2) -> ToolResult:
        """Add a directory to the workspace to view its contents."""
        try:
            workspace = await self.state_manager.get("workspace")
            if "open_folders" not in workspace:
                workspace["open_folders"] = {}
            if path not in workspace["open_folders"]:
                workspace["open_folders"][path] = depth or 2
                await self.state_manager.set("workspace", workspace)
                return self.success_response(f"Folder {path} added to workspace.")
            else:
                return self.success_response(f"Folder {path} is already open in the workspace.")
        except Exception as e:
            return self.fail_response(f"Error adding folder {path} to workspace: {str(e)}")

    @openapi_schema({
        "type": "function",
        "function": {
            "name": "SUBMIT_FINAL_SOLUTION_ONLY_IF_ALL_TESTS_PASS",
            "description": "Use this tool only if all test files are working including edge cases, and existing tests pass, and you are confident that the issue is resolved.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    })
    @xml_schema(
        tag_name="SUBMIT_FINAL_SOLUTION_ONLY_IF_ALL_TESTS_PASS",
        mappings=[],
        example='''
        <!-- Repository Tool: SUBMIT_FINAL_SOLUTION_ONLY_IF_ALL_TESTS_PASS -->
        <!-- Use when all tests of last_try pass and the issue is resolved -->

        <!-- No Parameters Required -->

        <!-- Mark the PR as solved -->
        <SUBMIT_FINAL_SOLUTION_ONLY_IF_ALL_TESTS_PASS />

        <!-- Important Notes:
             - Only use when all test files of last_try are working perfectly
             - Ensure edge cases are covered and existing tests pass
             - Use this tool only when the implementation works perfectly and all tests pass
        -->
        '''
    )
    async def SUBMIT_FINAL_SOLUTION_ONLY_IF_ALL_TESTS_PASS(self) -> ToolResult:
        """
        Signals that the task is completed.

        Returns:
            ToolResult: Success message indicating task completion.
        """
        return self.success_response("Task terminated, Agent stopped!")


    # @openapi_schema({
    #     "type": "function",
    #     "function": {
    #         "name": "close_file",
    #         "description": "Close a file and remove its content from the workspace state.",
    #         "parameters": {
    #             "type": "object",
    #             "properties": {
    #                 "path": {"type": "string", "description": "The file path to close."},
    #             },
    #             "required": ["path"]
    #         }
    #     }
    # })
    # @xml_schema(
    #     tag_name="close_file",
    #     mappings=[{"param_name": "path", "node_type": "attribute", "path": "."}],
    #     example='''
    #     <!-- Close File Tool -->
    #     <!-- Close a file and remove its content from the workspace state -->

    #     <!-- Parameters:
    #          - path: The file path to close (REQUIRED)
    #     -->
    #     <close_file path="/testbed/.../example.py" />
    #     '''
    # )
    # async def close_item(self, path: str) -> ToolResult:
    #     """Close a file or folder by removing its path from the workspace."""
    #     try:
    #         workspace = await self.state_manager.get("workspace")
    #         if path in workspace["open_folders"]:
    #             del workspace["open_folders"][path]
    #             await self.state_manager.set("workspace", workspace)
    #             return self.success_response(f"Folder {path} closed successfully.")
    #         elif path in workspace["open_files"]:
    #             workspace["open_files"].remove(path)
    #             await self.state_manager.set("workspace", workspace)
    #             return self.success_response(f"File {path} closed successfully.")
    #         else:
    #             return self.fail_response(f"Item {path} is not open.")
    #     except Exception as e:
    #         return self.fail_response(f"Error closing item {path}: {str(e)}")

    @openapi_schema({
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Create a new file with the specified content and add it to the workspace state. Do not create new test files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The file path to create."},
                    "content": {"type": "string", "description": "The content to write into the file."}
                },
                "required": ["path", "content"]
            }
        }
    })
    @xml_schema(
        tag_name="create_file",
        mappings=[
            {"param_name": "path", "node_type": "attribute", "path": "path"},
            {"param_name": "content", "node_type": "content", "path": "."}
        ],
        example='''
        <!-- Create File Tool -->
        <!-- Create a new file with specified content -->

        <!-- Parameters:
             - path: The file path to create (REQUIRED)
             - content: The content to write into the file (REQUIRED)
        -->
        <create_file path="/testbed/.../new_file.py">
print("Hello, World!")
        </create_file>
        '''
    )
    async def create_file(self, path: str, content: str) -> ToolResult:
        try:
            # Ensure the directory exists before creating the file
            command = (
                f"mkdir -p $(dirname {shlex.quote(path)}) && "
                f"echo {shlex.quote(content)} > {shlex.quote(path)}"
            )
            stdout, stderr, returncode = await self._bash_executor.execute(command)
            if returncode == 0:
                # Add to open_files
                workspace = await self.state_manager.get("workspace")
                if "open_files" not in workspace:
                    workspace["open_files"] = []
                if path not in workspace["open_files"]:
                    workspace["open_files"].append(path)
                    await self.state_manager.set("workspace", workspace)
                return self.success_response(f"File {path} created successfully.")
            else:
                return self.fail_response(f"Failed to create file {path}: {stderr}")
        except Exception as e:
            return self.fail_response(f"Error creating file {path}: {str(e)}")

    @openapi_schema({
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Edit an existing file by replacing specified strings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The file path to edit."},
                    "replacements": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {"type": "string"},
                                "new_string": {"type": "string"}
                            },
                            "required": ["old_string", "new_string"]
                        },
                        "description": "List of string replacements to perform."
                    }
                },
                "required": ["path", "replacements"]
            }
        }
    })
    @xml_schema(
        tag_name="edit_file",
        mappings=[
            {"param_name": "path", "node_type": "attribute", "path": "."},
            {"param_name": "replacements", "node_type": "element", "path": "replacements"}
        ],
        example='''
        <!-- Edit File Tool -->
        <!-- Edit an existing file by replacing specified strings -->

        <!-- Parameters:
            - path: The file path to edit (REQUIRED)
            - replacements: List of string replacements (REQUIRED)
        -->

        <!-- Note: For accurate replacements, ensure you match the exact indentation of the exact strings. -->

        <edit_file path="/testbed/.../example.py">
            <replacements>
                <replacement>
                    <old_string>foo</old_string>
                    <new_string>bar</new_string>
                </replacement>
                <replacement>
                    <old_string>hello</old_string>
                    <new_string>world</new_string>
                </replacement>
            </replacements>
        </edit_file>
        '''
    )
    async def edit_file(self, path: str, replacements) -> ToolResult:
        """Edit an existing file by replacing specified strings."""
        try:
            # Ensure the file is open in the workspace
            workspace = await self.state_manager.get("workspace")
            if "open_files" not in workspace or path not in workspace["open_files"]:
                return self.fail_response(f"File {path} is not open. Please open the file before editing.")

            # Read the current content from the file system
            command = f"cat {shlex.quote(path)}"
            stdout, stderr, returncode = await self._bash_executor.execute(command)
            if returncode != 0:
                return self.fail_response(f"Failed to read file {path}: {stderr.strip()}")

            content = stdout

            # Process the replacements
            replacements_list = []

            if isinstance(replacements, dict):
                if 'replacement' in replacements:
                    replacements_data = replacements['replacement']
                    if isinstance(replacements_data, list):
                        replacements_list = replacements_data
                    elif isinstance(replacements_data, dict):
                        replacements_list = [replacements_data]
                    else:
                        return self.fail_response("Invalid 'replacement' format in 'replacements'.")
                elif 'old_string' in replacements and 'new_string' in replacements:
                    replacements_list = [replacements]
                else:
                    return self.fail_response("Invalid replacements format.")
            elif isinstance(replacements, list):
                replacements_list = replacements
            elif isinstance(replacements, str):
                # Try to parse the string as XML-like tags
                replacements_dict = transform_string_to_dict(replacements)
                if 'replacement' in replacements_dict:
                    replacements_list = replacements_dict['replacement']
                else:
                    return self.fail_response("Invalid replacements format in string.")
            else:
                return self.fail_response("Invalid replacements format.")

            if not replacements_list:
                return self.fail_response("No valid replacements provided.")

            # Apply replacements
            for rep in replacements_list:
                if isinstance(rep, dict) and 'old_string' in rep and 'new_string' in rep:
                    old_string = rep['old_string']
                    new_string = rep['new_string']
                    if not isinstance(old_string, str) or not isinstance(new_string, str):
                        return self.fail_response("Both 'old_string' and 'new_string' must be strings.")
                    if old_string not in content:
                        return self.fail_response(f"The string to replace '{old_string}' was not found in the file. Please check your old_string: Indentation really matters! When editing a file, make sure to insert appropriate indentation before each line!")
                    content = content.replace(old_string, new_string)
                else:
                    return self.fail_response("Invalid replacement format in one of the replacements.")

            # Write the updated content back to the file
            input_data = content.encode('utf-8')
            command = f"cat > {shlex.quote(path)}"
            stdout, stderr, returncode = await self._bash_executor.execute(command, input_data=input_data)
            if returncode != 0:
                return self.fail_response(f"Failed to write to file {path}: {stderr.strip()}")

            return self.success_response(f"File {path} edited successfully.")
        except Exception as e:
            return self.fail_response(f"Error editing file {path}: {str(e)}")

    @openapi_schema({
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Run a shell command in the terminal and update the workspace state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute."}
                },
                "required": ["command"]
            }
        }
    })
    @xml_schema(
        tag_name="run_bash",
        mappings=[
            {"param_name": "command", "node_type": "attribute", "path": "command"}
        ],
        example='''
        <!-- Run Bash Command Tool -->
        <!-- Please AVOID commands that can produce lengthy output -->

        <!-- Examples -->
        <!-- Avoid -v for verbose output; instead use the recommended options like the following -->
        <run_bash command="python -m pytest /testbed/.../test_example.py -q -vv --tb=short --no-header -rFE" />

        <!-- For Django-like recommended command-->
        <run_bash command="/testbed/tests/runtests.py --verbosity 1 --settings=test_sqlite --parallel 1 example.test_example " />

        '''
    )
        # <run_bash command="DJANGO_SETTINGS_MODULE=test_sqlite pytest tests/.../test_example.py -q -rFE" />
    async def run_bash(self, command: str) -> ToolResult:
        """Execute a shell command and update the terminal session."""
        return await self._execute_command(command)

    async def _execute_command(self, command: str) -> ToolResult:
        """Execute a shell command and update the terminal session."""
        try:
            stdout, stderr, returncode = await self._bash_executor.execute(command)
            success = returncode == 0
            
            MAX_OUTPUT = 15000  
            KEEP_HEAD = 5000   
            KEEP_TAIL = 10000   
            
            combined_output = stdout + stderr
            if not combined_output:
                combined_output = "Command completed successfully but produced no output"
                
            if len(combined_output) > MAX_OUTPUT:
                head = combined_output[:KEEP_HEAD]
                tail = combined_output[-KEEP_TAIL:]
                truncated_output = head + '\n\n...LENGTHY OUTPUT TRUNCATED...\n\n' + tail
            else:
                truncated_output = combined_output
                
            await self._update_terminal(command, truncated_output, success)
            return self.success_response(f"Command executed:\n{truncated_output}")
            
        except Exception as e:
            return self.fail_response(f"Error executing command: {str(e)}")

    def success_response(self, message: str) -> ToolResult:
        result = super().success_response(message)
        # asyncio.create_task(self._add_action(message))
        return result

    def fail_response(self, message: str) -> ToolResult:
        result = super().fail_response(message)
        asyncio.create_task(self._add_failure(message))
        return result

    async def _add_failure(self, message: str):
        workspace = await self.state_manager.get("workspace")
        if "latest_failures" not in workspace:
            workspace["latest_failures"] = []
        workspace["latest_failures"].append(message)
        await self.state_manager.set("workspace", workspace)

    async def _add_action(self, message: str):
        workspace = await self.state_manager.get("workspace")
        # Ensure actions_taken is initialized
        if "actions_taken" not in workspace:
            workspace["actions_taken"] = []
        workspace["actions_taken"].append(
            message
        )
        await self.state_manager.set("workspace", workspace)

    @openapi_schema({
        "type": "function",
        "function": {
            "name": "open_file",
            "description": "Add a file to the workspace to view its content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The file path to add to the workspace."}
                },
                "required": ["path"]
            }
        }
    })
    @xml_schema(
        tag_name="open_file",
        mappings=[{"param_name": "path", "node_type": "attribute", "path": "."}],
        example='''
        <!-- Open File Tool -->
        <!-- Add a file to the workspace to view its content -->

        <!-- Parameters:
             - path: The file path to add to the workspace (REQUIRED)
        -->
        <!-- It's recommended to open multiple relevant files in the same time like this -->
        <open_file path="/testbed/.../example.py" />
        <open_file path="/testbed/.../example2.py" />
        <open_file path="/testbed/.../example3.py" />
        '''
    )
    async def open_file(self, path: str) -> ToolResult:
        """Add a file to the workspace to view its content."""
        try:
            workspace = await self.state_manager.get("workspace")
            if "open_files" not in workspace:
                workspace["open_files"] = []
            if path not in workspace["open_files"]:
                workspace["open_files"].append(path)
                await self.state_manager.set("workspace", workspace)
                return self.success_response(f"File {path} added to workspace.")
            else:
                return self.success_response(f"File {path} is already open in the workspace.")
        except Exception as e:
            return self.fail_response(f"Error adding file {path} to workspace: {str(e)}, please provide a valid file path. You may use view_folder to explore the folder structure.")

    @openapi_schema({
        "type": "function",
        "function": {
            "name": "track_implementation",
            "description": "Track implementation trials with IDs, statuses, and optional notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Unique identifier for the implementation trial (e.g., 'A', 'B')."
                    },
                    "status": {
                        "type": "string",
                        "description": (
                            "Status of the implementation trial (e.g., 'not tried', 'currently implementing', "
                            "'waiting for test', 'tried; not working')."
                        )
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional note containing additional information, code snippets, or analysis.",
                        "nullable": True
                    }
                },
                "required": ["id", "status"]
            }
        }
    })
    @xml_schema(
        tag_name="track_implementation",
        mappings=[
            {"param_name": "id", "node_type": "attribute", "path": "id"},
            {"param_name": "status", "node_type": "attribute", "path": "status"},
            {"param_name": "note", "node_type": "content", "path": "."}
        ],
        example='''
        <!-- Track Implementation Tool -->
        <!-- Track implementation trials with IDs, statuses, and notes -->

        <!-- Example Usage -->
        <track_implementation id="A" status="currently implementing;waiting for test">
        [Approach Title]
        `file:/testbed/.../example.py
        [Short update snippet]
        `
        `file:/testbed/.../example2.py
        [Short update snippet]
        `
        [Analysis] 
        [Log based on last try]
        [Test results summary]
        </track_implementation>
        
        <track_implementation id="B" status="tried; not working, bad resolution">
        [...example content...]
        </track_implementation>

        <track_implementation id="C" status="not tried">
        [...example content...]
        </track_implementation>
        '''
    )
    async def track_implementation(self, id: str, status: str, note: Optional[str] = None) -> ToolResult:
        """Track implementation trials with IDs, statuses, and optional notes."""
        try:
            workspace = await self.state_manager.get("workspace")
            if "implementation_trials" not in workspace:
                workspace["implementation_trials"] = {}
            workspace["implementation_trials"][id] = {
                "status": status,
                "note": note or ""
            }
            await self.state_manager.set("workspace", workspace)
            return self.success_response(f"Implementation trial '{id}' status updated to '{status}'.")
        except Exception as e:
            return self.fail_response(f"Error tracking implementation trial '{id}': {str(e)}")