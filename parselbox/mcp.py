import click
import asyncio
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# IMPORT YOUR CLASS
# ---------------------------------------------------------------------------
# If your class is in another file (e.g., codemode.py), uncomment the line below:
# from codemode import CodeMode

# For the purpose of this standalone file, we assume the class is available.
# If you are pasting this into your existing file, ensure CodeMode is defined above.
# ---------------------------------------------------------------------------


async def run_session(config_data: dict, auto_mode: bool):
    """
    Async handler to initialize CodeMode and manage the session.
    """
    try:
        # Initialize CodeMode with the loaded config
        # Note: CodeMode is assumed to be imported or defined in this scope
        async with CodeMode(config_data) as sandbox:

            click.secho("\n✓ CodeMode Initialized", fg="green")
            click.secho(f"  - Servers: {len(sandbox.servers)}", fg="blue")
            click.secho(f"  - Tools: {len(sandbox.tool_schemas)}", fg="blue")

            if auto_mode:
                click.secho("\n>>> Running in AUTO mode", fg="yellow", bold=True)
                # Add your autonomous logic here
                # e.g., await sandbox.run_auto_loop()
                click.echo("Auto mode logic finished.")
            else:
                click.secho("\n>>> Running in INTERACTIVE mode", fg="cyan", bold=True)
                # Add your interactive REPL or logic here
                # e.g., while True: code = input("code> "); await sandbox.execute(code)
                click.echo("Interactive session finished.")

    except Exception as e:
        click.secho(f"\nError initializing CodeMode: {e}", fg="red", err=True)
        sys.exit(1)


@click.command()
@click.option("--auto", is_flag=True, help="Enable autonomous execution mode.")
@click.option(
    "--config",
    type=click.Path(
        exists=True, file_okay=True, dir_okay=False, readable=True, path_type=Path
    ),
    required=True,
    help="Path to the MCP configuration JSON file (e.g., claude_desktop_config.json).",
)
def main(auto, config):
    """
    CLI for the CodeMode MCP Client.

    Loads a configuration file and starts the sandbox environment.
    """
    # 1. Load the Configuration
    try:
        with open(config, "r", encoding="utf-8") as f:
            config_data = json.load(f)

        # Basic validation to ensure it looks like an MCP config
        if "mcpServers" not in config_data:
            click.secho(
                "Warning: 'mcpServers' key not found in config JSON.", fg="yellow"
            )

    except json.JSONDecodeError:
        click.secho(f"Error: Failed to parse JSON from {config}", fg="red", err=True)
        sys.exit(1)
    except Exception as e:
        click.secho(f"Error reading file: {e}", fg="red", err=True)
        sys.exit(1)

    # 2. Run the Async Loop
    try:
        asyncio.run(run_session(config_data, auto))
    except KeyboardInterrupt:
        click.secho("\nSession terminated by user.", fg="yellow")


if __name__ == "__main__":
    main()
