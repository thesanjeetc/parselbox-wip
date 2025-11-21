import asyncio
import inspect
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union

from pathlib import Path
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
import subprocess


@dataclass
class Callback:
    type: Literal["callback", "proxy_callback"]
    name: str
    args: List[Any]
    kwargs: Dict[str, Any]
    path: Optional[List[str]] = field(default_factory=list)


@dataclass
class Result:
    output: Any
    files: List[str] = field(default_factory=list)
    error: Optional[str] = None


class SandboxError(Exception):
    """Base class for all sandbox errors."""

    pass


class SandboxTimeoutError(SandboxError):
    """Raised when code execution exceeds the time limit."""

    pass


class SandboxPermissionError(SandboxError):
    """Raised when the sandbox is denied access to a resource (net/fs)."""

    pass


class SandboxRuntimeError(SandboxError):
    """Raised when the Python code inside the sandbox fails (Syntax/Runtime)."""

    pass


DENO_SCRIPT_PATH = str(Path(__file__).parent.resolve() / "sandbox" / "sandbox.ts")

PACKAGE_DOWNLOAD_DOMAINS = [
    "cdn.jsdelivr.net:443",
    "pypi.org:443",
    "files.pythonhosted.org:443",
]


class PythonSandbox:
    def __init__(
        self,
        tools: Union[List[callable], Dict[str, callable]] = None,
        proxy_tools: Optional[Dict[str, callable]] = None,
        files: Optional[List[str]] = None,
        mounts: Optional[List[str] | Dict[str, str]] = None,
        output_dir: Optional[str] = None,
        allow_net: Union[bool, List[str]] = False,
        packages: Optional[List[str]] = None,
        auto_load_packages: bool = False,
        globals: Optional[Dict[str, Any]] = None,
        env: Optional[Dict[str, Any]] = None,
        deno_path: str = "deno",
        log_handler: Optional[callable] = None,
        memory_limit: int = 512,
        timeout: int = 60,
    ):
        self.deno_path = deno_path

        try:
            subprocess.run(
                [self.deno_path, "--version"], capture_output=True, check=True
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            raise RuntimeError(
                "Deno not found on PATH. Please install Deno from https://deno.land/."
            )

        self.tools = (
            {f.__name__: f for f in tools} if isinstance(tools, list) else (tools or {})
        )
        self.proxy_tools = proxy_tools or {}

        self.persistent_cache_root = Path.home() / ".cache" / "parselbox"
        self.deno_cache_dir = str(self.persistent_cache_root / "deno_core")
        os.makedirs(self.deno_cache_dir, exist_ok=True)

        self.temp_output_dir = tempfile.TemporaryDirectory() if not output_dir else None
        self.output_dir = output_dir or self.temp_output_dir.name

        self.cache_dir = tempfile.TemporaryDirectory()
        self.package_cache_dir = str(Path(self.cache_dir.name) / "package_cache_dir")
        self.input_dir = str(Path(self.cache_dir.name) / "files")
        os.makedirs(self.input_dir, exist_ok=True)

        self.files = files or []

        self.mounts = mounts or {}
        if isinstance(self.mounts, list):
            self.mounts = {Path(f).name: f for f in self.mounts}
        self.mounts["files"] = self.input_dir

        self.auto_load_packages = auto_load_packages
        self.allow_net = allow_net
        self.globals = globals or {}

        self.env = env or {}
        if isinstance(env, list):
            self.env = {var: os.environ[var] for var in env if var in os.environ}

        self.packages = packages or []
        self.log_handler = log_handler
        self.memory_limit = memory_limit
        self.timeout = timeout

        self.client: Client = None

    def _build_deno_args(self) -> List[str]:
        args = ["run"]

        read_write_paths = [
            self.output_dir,
            self.deno_cache_dir,
            self.package_cache_dir,
        ]

        read_only_paths = [*self.mounts.values(), self.input_dir]
        read_only_paths.extend(read_write_paths)

        args.append(f"--allow-read={','.join(sorted(set(read_only_paths)))}")
        args.append(f"--allow-write={','.join(sorted(set(read_write_paths)))}")

        allowed_domains = PACKAGE_DOWNLOAD_DOMAINS.copy()
        if isinstance(self.allow_net, list):
            allowed_domains.update(self.allow_net)
        args.append(f"--allow-net={','.join(sorted(list(allowed_domains)))}")

        args.append(f"--v8-flags=--max-old-space-size={self.memory_limit}")
        args.append("--allow-env")

        args.append(DENO_SCRIPT_PATH)
        return args

    def is_connected(self):
        return self.client and self.client.is_connected()

    async def connect(self):
        if self.is_connected():
            return

        deno_args = self._build_deno_args()

        self.env["PACKAGE_CACHE_DIR"] = self.package_cache_dir
        self.env["DENO_DIR"] = self.deno_cache_dir
        self.env["MPLBACKEND"] = "Agg"

        transport = StdioTransport(
            command=self.deno_path,
            args=deno_args,
            env=self.env,
        )

        self.client = Client(
            transport,
            elicitation_handler=self._handle_callback,
            log_handler=self.log_handler,
        )

        try:
            await self.client.__aenter__()
            if self.log_handler:
                await self.client.set_logging_level("info")
        except Exception as e:
            await self.close()
            raise RuntimeError(e) from e

        await self.initialize()

    async def initialize(self):
        disable_net = (not bool(self.allow_net)) and (not self.auto_load_packages)

        await self._configure(
            globals=self.globals,
            mounts=self.mounts,
            output_dir=self.output_dir,
            tools=self.tools,
            proxy_tools=self.proxy_tools,
            packages=self.packages,
            disable_net=disable_net,
            disable_runtime_packages=not self.auto_load_packages,
        )
        await self.upload_files(self.files)

    async def close(self):
        if not self.is_connected():
            return
        if self.client:
            await self.client.__aexit__(None, None, None)
        self.cache_dir.cleanup()
        if self.temp_output_dir:
            self.temp_output_dir.cleanup()
        self.client = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _handle_callback(
        self, callback_str: str, response_type, params: Any, context: Any
    ):
        try:
            callback = Callback(**json.loads(callback_str))
            if callback.type == "callback":
                func = self.tools[callback.name]
                output = (
                    await func(*callback.args, **callback.kwargs)
                    if inspect.iscoroutinefunction(func)
                    else func(*callback.args, **callback.kwargs)
                )
            elif callback.type == "proxy_callback":
                handler = self.proxy_tools[callback.name]
                output = (
                    await handler(callback)
                    if inspect.iscoroutinefunction(handler)
                    else handler(callback)
                )
            return response_type(result=json.dumps(output))
        except Exception as e:
            error_payload = {
                "__error__": str(e),
                "__error_type__": type(e).__name__,
            }
            return response_type(result=json.dumps(error_payload))

    async def _call_mcp(self, name, payload):
        if not self.is_connected():
            raise RuntimeError("Sandbox is not connected.")
        try:
            result = await self.client.call_tool(name, payload, raise_on_error=False)
        except Exception as e:
            if "Connection closed" in str(e) or "closed" in str(e).lower():
                raise SandboxRuntimeError(f"Sandbox crashed or connection closed: {e}")
            raise SandboxError(f"Communication error: {e}")
        if result.is_error:
            if result.structured_content and "error_code" in result.structured_content:
                self._raise_for_error(result.structured_content)
            error_text = result.content[0].text if result.content else "Unknown error"
            raise SandboxError(f"MCP Protocol Error: {error_text}")
        return result.structured_content

    def _raise_for_error(self, content: Dict[str, Any]):
        """Inspects the structured content and raises specific exceptions."""
        code = content.get("error_code")
        msg = content.get("error", "Unknown Error")

        if code == "TIMEOUT":
            raise SandboxTimeoutError(msg)
        if code == "PERMISSION_DENIED":
            raise SandboxPermissionError(msg)

    async def _configure(
        self,
        globals=None,
        mounts=None,
        output_dir=None,
        tools=None,
        proxy_tools=None,
        packages: List[str] = None,
        disable_net=None,
        disable_runtime_packages=None,
    ) -> Dict[str, Any]:
        payload = {}
        if globals:
            payload["globals"] = globals

        if output_dir:
            payload["output_dir"] = output_dir

        if mounts:
            payload["mounts"] = mounts

        if tools:
            payload["tools"] = list(tools.keys())

        if proxy_tools:
            payload["proxy_tools"] = list(proxy_tools.keys())

        if packages:
            payload["packages"] = packages

        if disable_net is not None:
            payload["disable_net"] = disable_net

        if disable_runtime_packages is not None:
            payload["disable_runtime_packages"] = disable_runtime_packages

        result = await self._call_mcp("configure", payload)
        if not result.get("is_success"):
            self._raise_for_error(result)
            raise SandboxError(f"Configuration failed: {result.get('error')}")
        return result

    async def upload_files(self, files: List[str]):
        if not files:
            return

        def _copy_sync(path: str):
            target_path = Path(path).resolve()
            if not target_path.is_file():
                return

            mount_path = Path(self.input_dir) / target_path.name

            if mount_path.exists():
                mount_path.unlink()

            try:
                os.link(target_path, mount_path)
            except (OSError, AttributeError):
                shutil.copy2(target_path, mount_path)

        loop = asyncio.get_running_loop()
        tasks = [loop.run_in_executor(None, _copy_sync, f) for f in files]
        await asyncio.gather(*tasks)

    async def execute_python(
        self, code: str, files: Optional[List[str]] = None
    ) -> Result:
        await self.upload_files(files)
        payload = {
            "code": code,
            "timeout": self.timeout,
            "auto_load_packages": self.auto_load_packages,
        }
        result = await self._call_mcp("execute_python", payload)
        files = [os.path.join(self.output_dir, f) for f in result.get("files", [])]

        return Result(
            output=result.get("result"),
            files=files,
            error=result.get("error"),
        )


async def run():
    sandbox = PythonSandbox()
    print(sandbox._build_deno_args())
    # mcp = FastMCP(name="PythonSandbox")
    # tools = {Tool.from_function(fn=sandbox.execute_python)}


if __name__ == "__main__":
    asyncio.run(run())
