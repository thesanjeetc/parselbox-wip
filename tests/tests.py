import asyncio
import os
import shutil
import pytest
from pathlib import Path
from textwrap import dedent
from parselbox import (
    PythonSandbox,
    SandboxTimeoutError,
    SandboxRuntimeError,
    SandboxPermissionError,
    SandboxError,
)

# Apply asyncio marker to all tests in this module
pytestmark = pytest.mark.asyncio


class TestCoreExecution:
    """Tests for basic Python execution, state, and variable injection."""

    async def test_basic_execution(self):
        """Test if the sandbox can perform basic Python logic."""
        async with PythonSandbox() as sandbox:
            result = await sandbox.execute_python("x = 10 + 32; x")
            assert result.output == 42
            assert result.error is None

    async def test_globals_injection(self):
        """Test injecting global variables into the sandbox context."""
        params = {"user_name": "Alice", "score": 100}
        async with PythonSandbox(globals=params) as sandbox:
            code = "f'{user_name} has {score} points'"
            result = await sandbox.execute_python(code)
            assert result.output == "Alice has 100 points"

    async def test_state_persistence(self):
        """Test that state persists between execute calls in the same session."""
        async with PythonSandbox() as sandbox:
            await sandbox.execute_python("x = 500")
            result = await sandbox.execute_python("x + 1")
            assert result.output == 501

    async def test_syntax_error(self):
        """Test handling of invalid Python code (Should NOT raise, returns error object)."""
        async with PythonSandbox() as sandbox:
            result = await sandbox.execute_python("def broken_code(")
            # User code errors (Syntax/Runtime) are returned, not raised
            assert result.error is not None
            assert "SyntaxError" in result.error


class TestFileSystem:
    """Tests for file uploads, downloads, mounts, and permissions."""

    async def test_input_files(self, tmp_path):
        """Test uploading specific files into the sandbox."""
        input_file = tmp_path / "data.txt"
        input_file.write_text("Hello from Host")

        async with PythonSandbox(files=[str(input_file)]) as sandbox:
            code = dedent(
                """
                with open('mnt/files/data.txt', 'r') as f:
                    content = f.read()
                content
            """
            )
            result = await sandbox.execute_python(code)
            assert result.output == "Hello from Host"

    async def test_output_files(self, tmp_path):
        """Test that files created in sandbox appear in output_dir."""
        output_path = tmp_path / "outputs"
        output_path.mkdir()

        async with PythonSandbox(output_dir=str(output_path)) as sandbox:
            code = dedent(
                """
                with open('result.csv', 'w') as f:
                    f.write('col1,col2\\n1,2')
            """
            )
            result = await sandbox.execute_python(code)

            assert any("result.csv" in f for f in result.files)
            assert (output_path / "result.csv").exists()
            assert (output_path / "result.csv").read_text() == "col1,col2\n1,2"

    async def test_mounts_dict_readonly(self, tmp_path):
        """Test that writing to a read-only mount returns a PermissionError OR crashes the sandbox."""
        data_dir = tmp_path / "my_data"
        data_dir.mkdir()
        (data_dir / "config.json").write_text('{"key": "value"}')

        mounts = {"datasets": str(data_dir)}

        async with PythonSandbox(mounts=mounts) as sandbox:
            # 1. Test Read Success
            read_code = dedent(
                """
                import os
                with open('mnt/datasets/config.json', 'r') as f:
                    data = f.read()
                data
            """
            )
            result = await sandbox.execute_python(read_code)
            assert '"key": "value"' in str(result.output)

            # 2. Test Write Fail
            # Note: Pyodide on Deno currently crashes fatally (NotCapable error -> Connection Closed)
            # when writing to a read-only mount. We accept a crash/RuntimeError OR a Python error as success here.
            write_code = dedent(
                """
                with open('mnt/datasets/hack.txt', 'w') as f:
                    f.write('bad')
            """
            )

            try:
                result = await sandbox.execute_python(write_code)
                # If it didn't crash, it better have an error
                assert result.error is not None
                assert "Read-only" in result.error or "Permission" in result.error
            except (SandboxRuntimeError, SandboxError):
                # Sandbox crash (Connection closed) is ALSO a valid pass for "Permission Denied"
                pass

    async def test_mounts_list_behavior(self, tmp_path):
        """Test passing mounts as a list of paths."""
        host_data_dir = tmp_path / "data_v1"
        host_data_dir.mkdir()
        (host_data_dir / "secret.txt").write_text("Top Secret")

        async with PythonSandbox(mounts=[str(host_data_dir)]) as sandbox:
            code = dedent(
                """
                with open('mnt/data_v1/secret.txt', 'r') as f:
                    content = f.read()
                content
            """
            )
            result = await sandbox.execute_python(code)
            assert result.output == "Top Secret"


class TestToolsAndCallbacks:
    """Tests for Host-Guest interoperability via tools and proxies."""

    async def test_tools_as_dict(self):
        """Test Python calling a function defined on the Host (dict format)."""

        def heavy_calculation(a, b):
            return a * b

        async with PythonSandbox(tools={"calc": heavy_calculation}) as sandbox:
            result = await sandbox.execute_python("await calc(5, 5)")
            assert result.output == 25

    async def test_tools_as_list(self):
        """Test passing tools as a list of callables."""

        def echo_shout(msg):
            return f"{msg.upper()}!"

        async with PythonSandbox(tools=[echo_shout]) as sandbox:
            result = await sandbox.execute_python("await echo_shout('hello')")
            assert result.output == "HELLO!"

    async def test_proxy_tools(self):
        """Test the dynamic proxy object capability."""

        class Database:
            def query(self, sql):
                return f"Result for {sql}"

        db = Database()

        async def db_proxy(callback):
            if callback.path[0] == "query":
                return db.query(*callback.args)

        async with PythonSandbox(proxy_tools={"db": db_proxy}) as sandbox:
            result = await sandbox.execute_python('await db.query("SELECT *")')
            assert result.output == "Result for SELECT *"


class TestPackagesAndNetwork:
    """Tests for package installation and network permissions."""

    async def test_packages_explicit_install(self):
        """Test installing a package via micropip."""
        async with PythonSandbox(packages=["pytz"], allow_net=True) as sandbox:
            code = "import pytz; 'UTC' in pytz.all_timezones"
            result = await sandbox.execute_python(code)
            assert result.output is True

    async def test_auto_load_packages_enabled(self):
        """Test that imports trigger installation when auto_load_packages is True."""
        async with PythonSandbox(auto_load_packages=True, allow_net=True) as sandbox:
            code = "import pytz; str(pytz.timezone('US/Pacific'))"
            result = await sandbox.execute_python(code)
            assert result.output == "US/Pacific"

    async def test_auto_load_packages_disabled(self):
        """Test that imports fail when auto_load is False."""
        async with PythonSandbox(auto_load_packages=False) as sandbox:
            code = "import pytz"
            result = await sandbox.execute_python(code)
            assert result.error is not None
            assert "ModuleNotFoundError" in result.error

    async def test_network_restriction(self):
        """Test that allow_net=False prevents arbitrary network access."""
        # This works because we catch the OSError inside Python and return a string.
        # If we didn't catch it, it would be a result.error (PYTHON_EXCEPTION).
        async with PythonSandbox(allow_net=False) as sandbox:
            code = dedent(
                """
                import urllib.request
                try:
                    urllib.request.urlopen('https://google.com')
                    res = "connected"
                except OSError:
                    res = "blocked"
                except Exception as e:
                    res = str(e)
                res
            """
            )
            result = await sandbox.execute_python(code)
            assert (
                "blocked" in str(result.output).lower()
                or "network" in str(result.output).lower()
            )


class TestConstraints:
    """Tests for resource limits and environment variables."""

    async def test_timeout_enforcement(self):
        """Test that long-running code raises SandboxTimeoutError."""
        async with PythonSandbox(timeout=2) as sandbox:
            code = "import time; time.sleep(5); 'Finished'"

            # Expecting the custom exception from main.py
            with pytest.raises(SandboxTimeoutError) as excinfo:
                await sandbox.execute_python(code)

            assert "timed out" in str(excinfo.value).lower()

    async def test_memory_limit_enforcement(self):
        """Test that the sandbox crashes or errors when exceeding memory limit."""
        # 50MB limit is soft; we need a very large allocation to guarantee OOM crash in V8
        async with PythonSandbox(memory_limit=50) as sandbox:
            # Increase allocation to ~500MB
            code = "x = 'a' * (1024 * 1024 * 500000); len(x)"
            try:
                await sandbox.execute_python(code)
                pytest.fail("Sandbox should have run out of memory but succeeded.")
            except (RuntimeError, SandboxError, SandboxRuntimeError) as e:
                assert "Connection" in str(e) or "closed" in str(e) or "Error" in str(e)

    async def test_env_vars(self):
        """Test passing environment variables."""
        os.environ["MY_TEST_KEY"] = "SECRET_123"
        try:
            async with PythonSandbox(env=["MY_TEST_KEY"]) as sandbox:
                code = "import os; os.environ.get('MY_TEST_KEY')"
                result = await sandbox.execute_python(code)
                assert result.output == "SECRET_123"
        finally:
            del os.environ["MY_TEST_KEY"]
