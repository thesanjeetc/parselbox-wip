import asyncio
import os
import shutil
import pytest
from pathlib import Path
from textwrap import dedent
from parselbox import (
    PythonSandbox,
    SandboxTimeoutError,
    SandboxPermissionError,
    SandboxRuntimeError,
    SandboxError,
)

pytestmark = pytest.mark.asyncio


class TestCoreExecution:
    async def test_basic_execution(self):
        async with PythonSandbox() as sandbox:
            result = await sandbox.execute_python("x = 10 + 32; x")
            assert result.output == 42

    async def test_globals_injection(self):
        params = {"user_name": "Alice", "score": 100}
        async with PythonSandbox(globals=params) as sandbox:
            result = await sandbox.execute_python("f'{user_name} has {score} points'")
            assert result.output == "Alice has 100 points"

    async def test_state_persistence(self):
        async with PythonSandbox() as sandbox:
            await sandbox.execute_python("x = 500")
            result = await sandbox.execute_python("x + 1")
            assert result.output == 501

    async def test_syntax_error(self):
        async with PythonSandbox() as sandbox:
            result = await sandbox.execute_python("def broken_code(")
            assert result.error is not None
            assert "SyntaxError" in result.error

    async def test_return_complex_types(self):
        async with PythonSandbox() as sandbox:
            code = dedent(
                """
                data = {
                    "list": [1, 2, 3],
                    "dict": {"a": 1, "b": 2},
                    "bool": True,
                    "none": None
                }
                data
            """
            )
            result = await sandbox.execute_python(code)
            assert result.output.get("list") == [1, 2, 3]
            assert result.output.get("dict") == {"a": 1, "b": 2}
            assert result.output.get("bool") is True
            assert result.output.get("none") is None

    async def test_standard_library(self):
        async with PythonSandbox() as sandbox:
            code = dedent(
                """
                import math
                import json
                
                val = math.sqrt(16)
                serialized = json.dumps({"x": val})
                serialized
            """
            )
            result = await sandbox.execute_python(code)
            assert '"x": 4.0' in result.output

    async def test_custom_class_logic(self):
        async with PythonSandbox() as sandbox:
            code = dedent(
                """
                class Calculator:
                    def __init__(self, start):
                        self.val = start
                    def add(self, x):
                        self.val += x
                        return self.val
                
                c = Calculator(10)
                c.add(5)
            """
            )
            result = await sandbox.execute_python(code)
            assert result.output == 15


class TestFileSystem:
    async def test_input_files(self, tmp_path):
        input_file = tmp_path / "data.txt"
        input_file.write_text("Hello from Host")
        async with PythonSandbox(files=[str(input_file)]) as sandbox:
            code = dedent(
                """
                with open('/files/data.txt', 'r') as f:
                    content = f.read()
                content
            """
            )
            result = await sandbox.execute_python(code)
            assert result.output == "Hello from Host"

    async def test_multiple_input_files(self, tmp_path):
        file_a = tmp_path / "a.txt"
        file_b = tmp_path / "b.txt"
        file_a.write_text("AAA")
        file_b.write_text("BBB")

        async with PythonSandbox(files=[str(file_a), str(file_b)]) as sandbox:
            code = dedent(
                """
                with open('/files/a.txt') as fa, open('/files/b.txt') as fb:
                    res = fa.read() + fb.read()
                res
            """
            )
            result = await sandbox.execute_python(code)
            assert result.output == "AAABBB"

    async def test_binary_file_io(self, tmp_path):
        bin_file = tmp_path / "image.bin"
        bin_file.write_bytes(b"\x00\xff\x10\x20")

        async with PythonSandbox(files=[str(bin_file)]) as sandbox:
            code = dedent(
                """
                with open('/files/image.bin', 'rb') as f:
                    data = f.read()
                data == b'\\x00\\xFF\\x10\\x20'
            """
            )
            result = await sandbox.execute_python(code)
            assert result.output is True

    async def test_output_files(self, tmp_path):
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
            assert (output_path / "result.csv").exists()

    async def test_mounts_dict_readonly(self, tmp_path):
        data_dir = tmp_path / "my_data"
        data_dir.mkdir()
        (data_dir / "config.json").write_text('{"key": "value"}')

        async with PythonSandbox(mounts={"datasets": str(data_dir)}) as sandbox:
            res = await sandbox.execute_python(
                "open('/mnt/datasets/config.json').read()"
            )
            assert '"key": "value"' in str(res.output)

            with pytest.raises((SandboxRuntimeError, SandboxPermissionError)):
                await sandbox.execute_python(
                    "open('/mnt/datasets/hack.txt', 'w').write('bad')"
                )

    async def test_mounts_list_behavior(self, tmp_path):
        host_data_dir = tmp_path / "data_v1"
        host_data_dir.mkdir()
        (host_data_dir / "secret.txt").write_text("Top Secret")
        async with PythonSandbox(mounts=[str(host_data_dir)]) as sandbox:
            res = await sandbox.execute_python("open('/mnt/data_v1/secret.txt').read()")
            assert res.output == "Top Secret"

    async def test_filesystem_permission_error(self, tmp_path):
        input_file = tmp_path / "protected.txt"
        input_file.write_text("read only")
        async with PythonSandbox(files=[str(input_file)]) as sandbox:
            code = "open('/files/protected.txt', 'w').write('illegal')"
            with pytest.raises((SandboxPermissionError, SandboxRuntimeError)):
                await sandbox.execute_python(code)


class TestToolsAndCallbacks:
    async def test_tools_as_dict(self):

        def calc(a, b):
            return a * b

        async with PythonSandbox(tools={"calc": calc}) as sandbox:
            res = await sandbox.execute_python("await calc(5, 5)")
            assert res.output == 25

    async def test_tools_as_list(self):
        def echo_shout(msg):
            return f"{msg.upper()}!"

        async with PythonSandbox(tools=[echo_shout]) as sandbox:
            res = await sandbox.execute_python("await echo_shout('hello')")
            assert res.output == "HELLO!"

    async def test_proxy_tools(self):

        class DB:
            def query(self, s):
                return f"Res: {s}"

        db = DB()

        async def db_proxy(cb):
            return db.query(*cb.args)

        async with PythonSandbox(proxy_tools={"db": db_proxy}) as sandbox:
            res = await sandbox.execute_python('await db.query("SELECT")')
            assert res.output == "Res: SELECT"

    async def test_tool_raising_exception(self):
        def faulty_tool():
            raise ValueError("Boom!")

        async with PythonSandbox(tools=[faulty_tool]) as sandbox:
            res = await sandbox.execute_python("await faulty_tool()")
            assert res.output is None
            assert res.error is not None
            assert "Boom!" in res.error


class TestPackagesAndNetwork:
    async def test_packages_explicit_install(self):
        async with PythonSandbox(packages=["pytz"], allow_net=True) as sandbox:
            res = await sandbox.execute_python(
                "import pytz; 'UTC' in pytz.all_timezones"
            )
            assert res.output is True

    async def test_auto_load_packages_enabled(self):
        async with PythonSandbox(auto_load_packages=True) as sandbox:
            res = await sandbox.execute_python(
                "import pytz; str(pytz.timezone('US/Pacific'))"
            )
            assert res.output == "US/Pacific"

    async def test_auto_load_packages_disabled(self):
        async with PythonSandbox(auto_load_packages=False) as sandbox:
            res = await sandbox.execute_python("import pytz")
            assert res.error is not None
            assert "ModuleNotFoundError" in res.error

    async def test_network_restriction_default(self):
        async with PythonSandbox(allow_net=False) as sandbox:
            code = dedent(
                """
                import pyodide.http
                try:
                    await pyodide.http.pyfetch('https://google.com')
                    res = "connected"
                except OSError:
                    res = "blocked"
                except Exception as e:
                    res = str(e)
                res
            """
            )
            res = await sandbox.execute_python(code)
            assert "blocked" in str(res.output) or "Permission" in str(res.output)

    async def test_network_allowlist_enforcement(self):
        async with PythonSandbox(allow_net=["example.com"]) as sandbox:
            code = dedent(
                """
                import pyodide.http
                try:
                    await pyodide.http.pyfetch('https://google.com')
                    res = "connected"
                except OSError:
                    res = "blocked"
                except Exception as e:
                    res = str(e)
                res
            """
            )
            res = await sandbox.execute_python(code)
            assert "blocked" in str(res.output) or "Permission" in str(res.output)

    async def test_network_allow_all(self):
        async with PythonSandbox(allow_net=True) as sandbox:
            code = dedent(
                """
                import pyodide.http
                try:
                    await pyodide.http.pyfetch('https://www.google.com')
                    res = "connected"
                except Exception as e:
                    res = str(e)
                res
            """
            )
            res = await sandbox.execute_python(code)
            assert res.output == "connected"


class TestLogging:
    async def test_log_handler_receives_output(self):
        logs = []

        def handler(message):
            logs.append(message.data)

        async with PythonSandbox(log_handler=handler) as sandbox:
            await sandbox.execute_python("print('hello world')")

        log_content = "".join(str(x) for x in logs)
        assert "hello world" in log_content


class TestConstraints:
    async def test_timeout_enforcement(self):
        async with PythonSandbox(timeout=2) as sandbox:
            with pytest.raises(SandboxTimeoutError):
                await sandbox.execute_python("import time; time.sleep(5)")

    async def test_env_vars(self):
        os.environ["MY_TEST_KEY"] = "SECRET_123"
        try:
            async with PythonSandbox(env=["MY_TEST_KEY"]) as sandbox:
                res = await sandbox.execute_python(
                    "import os; os.environ.get('MY_TEST_KEY')"
                )
                assert res.output == "SECRET_123"
        finally:
            del os.environ["MY_TEST_KEY"]

    async def test_invalid_deno_path(self):
        with pytest.raises(RuntimeError, match="Deno not found"):
            PythonSandbox(deno_path="/invalid/path/to/deno")
