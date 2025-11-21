import { z } from "zod";
import * as path from "https://deno.land/std/path/mod.ts";
import type {
  McpServer,
  McpToolConfig,
  McpToolResult,
} from "@modelcontextprotocol/sdk/server/mcp.js";
import type { PyodideInterface } from "pyodide";

export interface ToolContext {
  pyodide: PyodideInterface;
  mcpServer: McpServer;
  WORK_DIR: string;
  logger: ReturnType<typeof createServerLogger>;
}

export interface Tool {
  name: string;
  config: McpToolConfig;
  handler: (context: ToolContext, args: any) => Promise<McpToolResult>;
}

type ErrorCode =
  | "TIMEOUT"
  | "PERMISSION_DENIED"
  | "PYTHON_EXCEPTION"
  | "UNKNOWN";

function formatError(error: any): { code: ErrorCode; message: string } {
  const msg = error instanceof Error ? error.message : String(error);

  if (
    error instanceof Deno.errors.PermissionDenied ||
    msg.includes("Permission denied")
  ) {
    return {
      code: "PERMISSION_DENIED",
      message: `Sandbox Permission Error: ${msg}`,
    };
  }

  if (msg.includes("Execution timed out")) {
    return { code: "TIMEOUT", message: msg };
  }

  if (msg.includes("PythonError") || msg.includes("Traceback")) {
    return { code: "PYTHON_EXCEPTION", message: msg };
  }

  return { code: "UNKNOWN", message: msg };
}

function listFilesRecursive(
  pyodide: PyodideInterface,
  dirPath: string
): string[] {
  const allFiles: string[] = [];
  const scan = (currentPath: string) => {
    for (const entry of pyodide.FS.readdir(currentPath)) {
      if (entry === "." || entry === "..") continue;
      const fullPath = path.join(currentPath, entry);
      if (pyodide.FS.isDir(pyodide.FS.stat(fullPath).mode)) scan(fullPath);
      else allFiles.push(fullPath);
    }
  };
  scan(dirPath);
  return allFiles;
}

function getVFSState(
  pyodide: PyodideInterface,
  dirPath: string
): Map<string, number> {
  const state = new Map<string, number>();
  const allFiles = listFilesRecursive(pyodide, dirPath);

  for (const filePath of allFiles) {
    try {
      const stats = pyodide.FS.stat(filePath);
      state.set(filePath, stats.mtime.getTime());
    } catch (e) {
      console.warn(`Could not stat file ${filePath}:`, e);
    }
  }
  return state;
}

const PACKAGE_DOWNLOAD_DOMAINS = [
  "cdn.jsdelivr.net:443",
  "pypi.org:443",
  "files.pythonhosted.org:443",
];

export interface PackagePermissions {
  isNetworkDisabled: boolean;
  isRuntimePackagesDisabled: boolean;
}

export async function readPackagePermissions(): Promise<PackagePermissions> {
  const netPermissions = await Promise.all(
    PACKAGE_DOWNLOAD_DOMAINS.map((host) =>
      Deno.permissions.query({ name: "net", host })
    )
  );
  const cacheDir = Deno.env.get("PACKAGE_CACHE_DIR");
  const writePerms = cacheDir
    ? await Deno.permissions.query({ name: "write", path: cacheDir })
    : null;
  return {
    isNetworkDisabled: netPermissions.every((perm) => perm.state !== "granted"),
    isRuntimePackagesDisabled: !writePerms || writePerms.state !== "granted",
  };
}

const configureSandboxTool: Tool = {
  name: "configure",
  config: {
    title: "Configure Python Sandbox.",
    description: "Configure Python Sandbox.",
    inputSchema: {
      globals: z.record(z.any()).optional(),
      mounts: z.record(z.string()).optional(),
      input_dir: z.string().optional(),
      output_dir: z.string().optional(),
      tools: z.array(z.string()).optional(),
      proxy_tools: z.array(z.string()).optional(),
      packages: z.array(z.string()).optional(),
      disable_net: z.boolean().optional().default(false),
      disable_runtime_packages: z.boolean().optional().default(false),
    },
    outputSchema: {
      is_success: z.boolean(),
      error_code: z
        .enum(["TIMEOUT", "PERMISSION_DENIED", "PYTHON_EXCEPTION", "UNKNOWN"])
        .optional(),
      error: z.string().optional(),
    },
  },
  handler: async ({ pyodide, WORK_DIR }, args) => {
    try {
      if (args.globals) {
        for (const [key, value] of Object.entries(args.globals)) {
          pyodide.globals.set(key, pyodide.toPy(value));
        }
      }

      if (args.output_dir) {
        pyodide.FS.mount(
          pyodide.FS.filesystems.NODEFS,
          { root: args.output_dir },
          WORK_DIR
        );
      }

      if (args.input_dir) {
        const filesRoot = "/files";
        pyodide.FS.mkdirTree(filesRoot);
        pyodide.FS.mount(
          pyodide.FS.filesystems.NODEFS,
          { root: args.input_dir },
          filesRoot
        );
      }

      if (args.mounts && Object.keys(args.mounts).length > 0) {
        const mountRoot = "/mnt";
        pyodide.FS.mkdirTree(mountRoot);

        for (const [name, hostPath] of Object.entries(args.mounts)) {
          const mountPoint = path.join(mountRoot, name);
          pyodide.FS.mkdirTree(mountPoint);
          pyodide.FS.mount(
            pyodide.FS.filesystems.NODEFS,
            { root: hostPath },
            mountPoint
          );
        }
      }

      if (args.tools?.length || args.proxy_tools?.length) {
        const createCallback = pyodide.globals.get("_create_host_callback");
        const DynamicProxyClass = pyodide.globals.get("_DynamicProxy");

        (args.tools ?? []).forEach((name: string) => {
          const callbackFunc = createCallback(name);
          pyodide.globals.set(name, callbackFunc);
        });

        (args.proxy_tools ?? []).forEach((name: string) => {
          pyodide.globals.set(name, DynamicProxyClass(name));
        });
      }

      if (args.packages?.length) {
        const perms = await readPackagePermissions();

        if (perms.isNetworkDisabled) {
          throw new Error(
            "Network access is required to install Python packages."
          );
        }
        if (perms.isRuntimePackagesDisabled) {
          throw new Error("Runtime package loading is disabled.");
        }

        await pyodide.loadPackage(args.packages);
      }

      return {
        structuredContent: { is_success: true },
        content: [
          { type: "text", text: "Python environment configured successfully." },
        ],
      };
    } catch (error: any) {
      return {
        structuredContent: { error: error.message },
        content: [
          { type: "text", text: `Configuration Error: ${error.message}` },
        ],
        isError: true,
      };
    } finally {
      if (args.disable_net) {
        await Deno.permissions.revoke({ name: "net" });
      }
      if (args.disable_runtime_packages) {
        await Deno.permissions.revoke({
          name: "write",
          path: Deno.env.get("PACKAGE_CACHE_DIR"),
        });
      }
    }
  },
};

const executePythonTool: Tool = {
  name: "execute_python",
  config: {
    title: "Execute Python Code",
    description: "Executes Python code.",
    inputSchema: {
      code: z.string(),
      timeout: z.number().optional(),
      auto_load_packages: z.boolean().optional().default(false),
    },
    outputSchema: {
      is_success: z.boolean(),
      result: z.any().optional(),
      files: z.array(z.string()).optional(),
      error: z.string().optional(),
      error_code: z
        .enum(["TIMEOUT", "PERMISSION_DENIED", "PYTHON_EXCEPTION", "UNKNOWN"])
        .optional(),
    },
  },
  handler: async (
    { pyodide, WORK_DIR },
    { code, timeout, auto_load_packages }
  ) => {
    let timeoutId: number | undefined;

    try {
      const filesBefore = getVFSState(pyodide, WORK_DIR);

      if (auto_load_packages) {
        await pyodide.loadPackagesFromImports(code);
      }

      const interruptBuffer = new Int32Array(new SharedArrayBuffer(4));
      pyodide.setInterruptBuffer(interruptBuffer);

      const execOptions = {
        filename: "main.py",
        return_mode: "last_expr_or_assign" as const,
      };

      const executionPromise = pyodide.runPythonAsync(code, execOptions);
      let resultProxy;

      if (timeout && timeout > 0) {
        resultProxy = await Promise.race([
          executionPromise,
          new Promise((_, reject) => {
            timeoutId = setTimeout(() => {
              Atomics.store(interruptBuffer, 0, 2); // SIGINT
              reject(new Error(`Execution timed out after ${timeout} seconds`));
            }, timeout * 1000);
          }),
        ]);
      } else {
        resultProxy = await executionPromise;
      }

      const result =
        resultProxy?.toJs?.({ dict_converter: Object.fromEntries }) ??
        resultProxy; // TODO: robust serialize outputs

      const output_files: string[] = [];
      const filesAfter = getVFSState(pyodide, WORK_DIR);

      for (const [filePath, mtimeAfter] of filesAfter.entries()) {
        const mtimeBefore = filesBefore.get(filePath);
        if (mtimeBefore === undefined || mtimeAfter > mtimeBefore) {
          const relativePath = path.relative(WORK_DIR, filePath);
          output_files.push(relativePath);
        }
      }

      return {
        structuredContent: {
          is_success: true,
          result,
          files: output_files,
        },
        content: [{ type: "text", text: JSON.stringify({ result }, null, 2) }],
      };
    } catch (error: any) {
      const { code, message } = formatError(error);
      const isSystemError = code === "TIMEOUT" || code === "PERMISSION_DENIED";

      return {
        structuredContent: {
          is_success: false,
          error: message,
          error_code: code,
        },
        content: [{ type: "text", text: message }],
        isError: isSystemError,
      };
    } finally {
      if (timeoutId) {
        clearTimeout(timeoutId);
      }
      pyodide.setInterruptBuffer(undefined);
    }
  },
};

export const TOOLS: Tool[] = [configureSandboxTool, executePythonTool];
