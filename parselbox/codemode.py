from .main import PythonSandbox, Callback
from fastmcp import Client
from jsonschema import validate
import json


# auto load mcps from claude_desktop_config.json, mcp.json, .mcp.json, etc.


class CodeMode:
    def __init__(self, config, sandbox=None):
        self.internal_sandbox = False
        if not sandbox:
            self.internal_sandbox = True
            sandbox = PythonSandbox()
        self.sandbox = sandbox
        self.client = Client(config)
        self.servers = list(config["mcpServers"].keys())
        self.sandbox.proxy_tools |= {k: self.handle_tool for k in self.servers}
        self.tool_schemas = {}

    async def handle_tool(self, callback: Callback):
        server_name = callback.name
        tool_name = callback.path[0]
        mcp_tool = f"{server_name}_{tool_name}" if len(self.servers) > 1 else tool_name
        tool_schema = self.tool_schemas[tool_name]
        schema_props = list(tool_schema["properties"].keys())
        tool_params = {**dict(zip(schema_props, callback.args)), **callback.kwargs}
        validate(instance=tool_params, schema=tool_schema)
        result = await self.client.call_tool_mcp(mcp_tool, callback.kwargs)
        content = json.loads(result.content[0].text)
        # TODO: try json else return plain text
        if result.isError:
            raise Exception(content)
        return content

    async def load_tool_schemas(self):
        tools = await self.client.list_tools()
        for tool in tools:
            self.tool_schemas[tool.name] = tool.inputSchema

    async def __aenter__(self):
        await self.sandbox.__aenter__()
        await self.client.__aenter__()
        await self.load_tool_schemas()
        return self.sandbox

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.internal_sandbox:
            await self.sandbox.__aexit__(exc_type, exc_val, exc_tb)
        await self.client.__aexit__(exc_type, exc_val, exc_tb)

    def __getattr__(self, name):
        return getattr(self.sandbox, name)
