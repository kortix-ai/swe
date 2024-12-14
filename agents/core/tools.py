from typing import Dict, Any, Union, Optional, List, Type, Callable
from dataclasses import dataclass, field
from abc import ABC
from enum import Enum
import json
import inspect
import logging
import asyncio
import re

class SchemaType(Enum):
    OPENAPI = "openapi"
    XML = "xml"
    CUSTOM = "custom"


@dataclass
class XMLNodeMapping:
    param_name: str
    node_type: str = "element"
    path: str = "."


@dataclass
class XMLTagSchema:
    tag_name: str
    mappings: List[XMLNodeMapping] = field(default_factory=list)
    example: Optional[str] = None

    def add_mapping(self, param_name: str, node_type: str = "element", path: str = "."):
        self.mappings.append(XMLNodeMapping(param_name, node_type, path))


@dataclass
class ToolSchema:
    schema_type: SchemaType
    schema: Dict[str, Any]
    xml_schema: Optional[XMLTagSchema] = None


@dataclass
class ToolResult:
    success: bool
    output: str
    def __str__(self) -> str:
        return self.output


class Tool(ABC):
    def __init__(self):
        self._schemas: Dict[str, List[ToolSchema]] = {}
        self._register_schemas()

    def _register_schemas(self):
        for name, method in inspect.getmembers(self, predicate=inspect.ismethod):
            if hasattr(method, 'tool_schemas'):
                self._schemas[name] = method.tool_schemas
            else:
                logging.debug(f"Method {name} has no tool_schemas attribute.")

    def get_schemas(self) -> Dict[str, List[ToolSchema]]:
        return self._schemas

    def success_response(self, data: Union[Dict[str, Any], str]) -> ToolResult:
        return ToolResult(True, data if isinstance(data, str) else json.dumps(data))

    def fail_response(self, msg: str) -> ToolResult:
        return ToolResult(False, msg)


def _add_schema(func, schema: ToolSchema):
    if not hasattr(func, 'tool_schemas'):
        func.tool_schemas = []
    func.tool_schemas.append(schema)
    return func


def xml_schema(tag_name: str, mappings: Optional[List[Dict[str, str]]] = None, example: Optional[str] = None):
    if mappings is None:
        mappings = []
    def decorator(func):
        xml_schema_obj = XMLTagSchema(tag_name, example=example)
        if mappings:
            for m in mappings:
                if "param_name" not in m:
                    logging.error("Mapping missing 'param_name'.")
                    continue
            xml_schema_obj.add_mapping(m["param_name"], m.get("node_type", "element"), m.get("path", "."))
        return _add_schema(func, ToolSchema(SchemaType.XML, {}, xml_schema=xml_schema_obj))
    return decorator


class ToolRegistry:
    def __init__(self):
        self.tools = {}
        self.xml_tools = {}
        self.lock = asyncio.Lock()

    async def register_tool(self, tool_class: Type[Tool], function_names: Optional[List[str]] = None, **kwargs):
        async with self.lock:
            tool_instance = tool_class(**kwargs)
            schemas = tool_instance.get_schemas()
            logging.info(f"Registering {tool_class.__name__} with schemas {list(schemas.keys())}")
            for func_name, schema_list in schemas.items():
                if not function_names or func_name in function_names:
                    for schema in schema_list:
                        if schema.schema_type == SchemaType.XML and schema.xml_schema:
                            self.xml_tools[schema.xml_schema.tag_name] = {
                                "instance": tool_instance,
                                "method": func_name,
                                "schema": schema
                            }
                            logging.debug(f"Registered XML tag {schema.xml_schema.tag_name} -> {func_name}")

    def get_available_functions(self) -> Dict[str, Callable]:
        funcs = {}
        for tool_info in self.xml_tools.values():
            instance = tool_info['instance']
            method_name = tool_info['method']
            method = getattr(instance, method_name, None)
            if callable(method):
                funcs[method_name] = method
        return funcs

    def get_tool(self, tool_name: str) -> Dict[str, Any]:
        # tool_name here refers to the function name
        # Find a xml_tool whose method matches tool_name
        for t in self.xml_tools.values():
            if t['method'] == tool_name:
                return t
        return {}

    def get_xml_tool(self, tag_name: str) -> Dict[str, Any]:
        return self.xml_tools.get(tag_name, {})
