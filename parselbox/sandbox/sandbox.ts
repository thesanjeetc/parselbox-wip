// sandbox.ts

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { loadPyodide } from "pyodide";
import { type ToolContext, TOOLS } from "./tools.ts";
import {
  type LoggingLevel,
  SetLevelRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const WORK_DIR = "/workspace";

const PY_SETUP = `
import json
import datetime
import builtins

async def _host_rpc_call(payload_dict):
    payload_str = json.dumps(payload_dict)
    result_str = await _js_host_rpc_bridge(payload_str)
    result_obj = json.loads(result_str)
    if isinstance(result_obj, dict) and "__error__" in result_obj:
        error_type_name = result_obj.get("__error_type__", "Exception")
        error_message = result_obj.get("__error__", "An unknown error occurred in the host callback.")
        exception_class = getattr(builtins, error_type_name, Exception)
        raise exception_class(f"Host callback failed: {error_message}")
    return result_obj

def _create_host_callback(name):
    async def callback(*args, **kwargs):
        return await _host_rpc_call({
            "type": "callback",
            "name": name,
            "args": args,
            "kwargs": kwargs
        })
    callback.__name__ = name
    return callback

class _DynamicProxy:
    def __init__(self, root_name, path_parts=None):
        self._root_name = root_name
        self._path_parts = path_parts or []

    def __getattr__(self, name):
        new_path = self._path_parts + [name]
        return _DynamicProxy(self._root_name, new_path)

    async def __call__(self, *args, **kwargs):
        return await _host_rpc_call({
            "type": "proxy_callback",
            "name": self._root_name,
            "path": self._path_parts,
            "args": args,
            "kwargs": kwargs
        })

def robust_serialize(obj) -> str:
    def _robust_serialize(obj, ancestors=None):
        if isinstance(obj, (str, int, float, bool, type(None))):
            return obj

        if ancestors is None:
            ancestors = set()

        obj_id = id(obj)
        if obj_id in ancestors:
            return {"type": "circular_reference", "repr": repr(obj)}
        
        next_ancestors = ancestors | {obj_id}

        if isinstance(obj, (list, tuple, set, frozenset)):
            return [_robust_serialize(item, next_ancestors) for item in obj]

        if isinstance(obj, dict):
            return {str(key): _robust_serialize(value, next_ancestors) for key, value in obj.items()}

        if isinstance(obj, (datetime.date, datetime.datetime)):
            return obj.isoformat()

        return {"type": "not_serializable", "repr": repr(obj)}

    return json.dumps(_robust_serialize(obj))
`;

const LogLevels: LoggingLevel[] = [
  "debug",
  "info",
  "notice",
  "warning",
  "error",
  "critical",
  "alert",
  "emergency",
];

function createServerLogger(
  mcpServer: McpServer,
  getLogLevel: () => LoggingLevel
) {
  const log = (level: LoggingLevel, context: string, data: string | Error) => {
    if (LogLevels.indexOf(level) >= LogLevels.indexOf(getLogLevel())) {
      const message = data instanceof Error ? data.stack ?? data.message : data;
      mcpServer.server.sendLoggingMessage({
        level,
        data: `[SANDBOX:(${context.toUpperCase()})] ${message}`,
      });
    }
  };

  return {
    debug: (context: string, data: string | Error) =>
      log("debug", context, data),
    info: (context: string, data: string | Error) => log("info", context, data),
    warn: (context: string, data: string | Error) =>
      log("warning", context, data),
    error: (context: string, data: string | Error) =>
      log("error", context, data),
  };
}

async function main() {
  const deno_dir = Deno.env.get("DENO_DIR");
  await Deno.permissions.revoke({ name: "write", path: deno_dir });

  const mcpServer = new McpServer(
    {
      name: "parselbox-sandbox",
      version: "0.0.1",
    },
    {
      capabilities: {
        logging: {},
      },
    }
  );

  let setLogLevel: LoggingLevel = "info";
  const logger = createServerLogger(mcpServer, () => setLogLevel);

  mcpServer.server.setRequestHandler(SetLevelRequestSchema, (request) => {
    setLogLevel = request.params.level;
    logger.info("deno", `Log level set to: ${setLogLevel}`);
    return {};
  });

  const env = Deno.env.toObject();
  const packageCacheDir = Deno.env.get("PACKAGE_CACHE_DIR");

  const pyodide = await loadPyodide({
    env: env,
    packageCacheDir: packageCacheDir,
    stdout: (msg: string) => {
      logger.info("pyodide.stdout", msg);
    },
    stderr: (msg: string) => {
      logger.warn("pyodide.stderr", msg);
    },
  });

  const origLoadPackage = pyodide.loadPackage;
  pyodide.loadPackage = (pkgs, options) =>
    origLoadPackage(pkgs, {
      messageCallback: (msg: string) => {
        logger.debug("pyodide.pip", msg);
      },
      errorCallback: (msg: string) => {
        logger.error("pyodide.pip", `install error: ${msg}`);
      },
      ...options,
    });

  pyodide.FS.mkdir(WORK_DIR);
  pyodide.runPython(`import os; os.chdir('${WORK_DIR}')`);

  await pyodide.runPythonAsync(PY_SETUP);

  const jsHostRPCBridge = async (payloadStr: string): Promise<string> => {
    const response = await mcpServer.server.elicitInput({
      message: payloadStr,
      requestedSchema: {
        type: "object",
        properties: { result: { type: "string" } },
      },
    });

    if (response.action === "accept" && response.content) {
      return response.content.result;
    } else {
      throw new Error(`Host call rejected or failed: ${response.action}`);
    }
  };

  pyodide.globals.set("_js_host_rpc_bridge", jsHostRPCBridge);

  const context: ToolContext = { pyodide, mcpServer, WORK_DIR, logger };

  for (const tool of TOOLS) {
    mcpServer.registerTool(tool.name, tool.config, (args) =>
      tool.handler(context, args)
    );
  }

  const transport = new StdioServerTransport();
  await mcpServer.connect(transport);
  logger.info("deno", "Server connected. Ready for requests.");
}

main().catch((err) => {
  console.error("An unrecoverable error occurred:", err);
  Deno.exit(1);
});
