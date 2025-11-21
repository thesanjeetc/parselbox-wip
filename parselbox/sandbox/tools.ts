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

function listFilesRecursive(
  pyodide: PyodideInterface,
  dirPath: string
): string[] {
  const allFiles: string[] = [];
  const scan = (currentPath: string) => {
    for (const entry of pyodide.FS.readdir(currentPath)) {
      if (entry === "." || entry === ".." || entry === "mnt") continue;
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
    title: "Configure Python Environment",
    description:
      "Configures the Python sandbox by loading packages and/or setting up callbacks to the host. Both actions are optional.",
    inputSchema: {
      globals: z
        .record(z.any())
        .optional()
        .describe(
          "A dictionary of global variables to inject into the Python scope."
        ),
      mounts: z
        .record(z.string())
        .optional()
        .describe("Dictionary of folders to mount to /mnt/{name} (Read-Only)."),
      output_dir: z
        .string()
        .optional()
        .describe("Absolute path on host to mount as / (Writeable)."),
      tools: z
        .array(z.string())
        .optional()
        .describe(
          "List of function names for direct callbacks from Python to the host."
        ),
      proxy_tools: z
        .array(z.string())
        .optional()
        .describe(
          "List of object names for dynamic proxies from Python to the host."
        ),
      packages: z
        .array(z.string())
        .optional()
        .describe("List of packages to load into the Pyodide environment."),
      disable_net: z
        .boolean()
        .optional()
        .default(false)
        .describe(
          "If true, revokes all network access after loading packages."
        ),
      disable_runtime_packages: z
        .boolean()
        .optional()
        .default(false)
        .describe(
          "If true, prevents any more packages from being installed or modified."
        ),
    },
    outputSchema: { is_success: z.boolean() },
  },
  handler: async (
    { pyodide, WORK_DIR },
    {
      globals,
      mounts,
      output_dir,
      tools,
      proxy_tools,
      packages,
      disable_net,
      disable_runtime_packages,
    }
  ) => {
    try {
      if (globals) {
        for (const [key, value] of Object.entries(globals)) {
          pyodide.globals.set(key, pyodide.toPy(value));
        }
      }

      if (output_dir) {
        pyodide.FS.mount(
          pyodide.FS.filesystems.NODEFS,
          { root: output_dir },
          WORK_DIR
        );
      }

      if (mounts && Object.keys(mounts).length > 0) {
        const mountRoot = path.join(WORK_DIR, "mnt");
        pyodide.FS.mkdirTree(mountRoot);

        for (const [name, hostPath] of Object.entries(mounts)) {
          const mountPoint = path.join(mountRoot, name);
          pyodide.FS.mkdirTree(mountPoint);
          pyodide.FS.mount(
            pyodide.FS.filesystems.NODEFS,
            { root: hostPath },
            mountPoint
          );
        }
      }

      if (tools?.length || proxy_tools?.length) {
        const createCallback = pyodide.globals.get("_create_host_callback");
        const DynamicProxyClass = pyodide.globals.get("_DynamicProxy");

        (tools ?? []).forEach((name: string) => {
          const callbackFunc = createCallback(name);
          pyodide.globals.set(name, callbackFunc);
        });

        (proxy_tools ?? []).forEach((name: string) => {
          pyodide.globals.set(name, DynamicProxyClass(name));
        });
      }

      if (packages?.length) {
        const perms = await readPackagePermissions();

        if (perms.isNetworkDisabled) {
          throw new Error(
            "Network access is required to install Python packages."
          );
        }
        if (perms.isRuntimePackagesDisabled) {
          throw new Error("Runtime package loading is disabled.");
        }

        await pyodide.loadPackage(packages);
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
      if (disable_net) {
        await Deno.permissions.revoke({ name: "net" });
      }
      if (disable_runtime_packages) {
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
    description:
      "Executes Python code in the sandbox and optionally saves new files.",
    inputSchema: {
      code: z.string().describe("The Python code to execute."),
      timeout: z.number().optional().describe("Execution timeout in seconds."),
      auto_load_packages: z
        .boolean()
        .optional()
        .default(false)
        .describe("Whether packages should be auto-loaded."),
    },
    outputSchema: {
      is_success: z.boolean(),
      result: z.any().optional(),
      error: z.string().optional(),
      files: z
        .array(z.string())
        .optional()
        .describe("Absolute paths to any output files saved on the host."),
    },
  },
  handler: async (
    { pyodide, WORK_DIR },
    { code, timeout, auto_load_packages }
  ) => {
    const filesBefore = getVFSState(pyodide, WORK_DIR);
    let timeoutId: number | undefined;

    if (auto_load_packages) {
      await pyodide.loadPackagesFromImports(code);
    }

    const execOptions = {
      filename: "main.py",
      return_mode: "last_expr_or_assign" as const,
    };

    try {
      const interruptBuffer = new Int32Array(new SharedArrayBuffer(4));
      pyodide.setInterruptBuffer(interruptBuffer);

      const executionPromise = pyodide.runPythonAsync(code, execOptions);

      let resultProxy;
      if (timeout && timeout > 0) {
        const timeoutPromise = new Promise((_, reject) => {
          timeoutId = setTimeout(() => {
            Atomics.store(interruptBuffer, 0, 2);
            reject(new Error(`Execution timed out after ${timeout} seconds`));
          }, timeout * 1000);
        });
        resultProxy = await Promise.race([executionPromise, timeoutPromise]);
      } else {
        resultProxy = await executionPromise;
      }

      if (timeoutId) {
        clearTimeout(timeoutId);
      }

      const output_files: string[] = [];
      const filesAfter = getVFSState(pyodide, WORK_DIR);

      for (const [filePath, mtimeAfter] of filesAfter.entries()) {
        const mtimeBefore = filesBefore.get(filePath);
        if (mtimeBefore === undefined || mtimeAfter > mtimeBefore) {
          const relativePath = path.relative(WORK_DIR, filePath);
          output_files.push(relativePath);
        }
      }

      const result =
        resultProxy?.toJs?.({ dict_converter: Object.fromEntries }) ??
        resultProxy; // TODO: robust serialize outputs

      const message =
        output_files.length > 0
          ? `\nGenerated ${output_files.length} file(s).`
          : "";

      return {
        structuredContent: {
          is_success: true,
          result,
          files: output_files,
          error: message,
          error_code: code,
        },
        content: [
          { type: "text", text: JSON.stringify({ result }, null, 2) + message },
        ],
      };
    } catch (error: any) {
      if (timeoutId) {
        clearTimeout(timeoutId);
      }
      return {
        structuredContent: { error: error.message },
        content: [
          { type: "text", text: `Python Execution Error: ${error.message}` },
        ],
        isError: true,
      };
    } finally {
      pyodide.setInterruptBuffer(undefined);
    }
  },
};

export const TOOLS: Tool[] = [configureSandboxTool, executePythonTool];
