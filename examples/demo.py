import asyncio
import json
import os
import shutil
from parselbox import PythonSandbox
from PIL import Image, ImageDraw
import httpx


# Helper to clean/create directories
def reset_dir(path):
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path)
    return os.path.abspath(path)


# ==============================================================================
# DEMO 1: DATA SCIENCE WORKFLOW (THE "MOUNT" STRATEGY)
#
# Best for: ETL, Data Analysis, Batch Processing
# Architecture:
#   - Input:  Mounted Read-Only at /workspace/mnt/datasets
#   - Output: Mounted Read-Write at /workspace
#   - Result: Zero-copy data loading, instant results on host.
# ==============================================================================


async def demo_pandas_mounts():
    print("\n" + "=" * 80)
    print("--- DEMO 1: Data Analysis with Mounts (Zero-Copy) ---")
    print("=" * 80)

    # 1. Setup Host Directories
    input_dir = reset_dir("./demo1_input")
    output_dir = reset_dir("./demo1_output")

    # Create dummy data on host
    csv_path = os.path.join(input_dir, "sales.csv")
    with open(csv_path, "w") as f:
        f.write(
            "category,revenue\nElectronics,5000\nBooks,1200\nElectronics,3000\nApparel,800"
        )
    print(f"[HOST] Created data: {csv_path}")

    # 2. Initialize Sandbox
    # We map our local input_dir to 'datasets' inside the sandbox
    print("[HOST] Initializing Sandbox with Mounts...")
    sandbox = PythonSandbox(
        mounts={"datasets": input_dir},  # -> /workspace/mnt/datasets
        output_dir=output_dir,  # -> /workspace
        packages=["pandas"],
    )
    await sandbox.connect()

    # 3. Run Analysis
    # Notice we read from 'mnt/datasets' and write to root
    print("[HOST] Running Pandas Analysis...")
    code = """
import pandas as pd
import os

# 1. Read from the mounted input (FAST - no upload needed)
df = pd.read_csv("mnt/datasets/sales.csv")
print(f"Loaded {len(df)} rows from mount.")

# 2. Analyze
summary = df.groupby("category")["revenue"].sum()

# 3. Write to the mounted output (FAST - appears instantly on host)
summary.to_csv("revenue_report.csv")
print("Saved revenue_report.csv to workspace root.")
"""
    result = await sandbox.execute_python(code)

    if result.error:
        print(f"❌ Error: {result.error}")
    else:
        # 4. Verify Results on Host
        report_path = os.path.join(output_dir, "revenue_report.csv")
        if os.path.exists(report_path):
            print(f"✅ [HOST] Report found at: {report_path}")
            with open(report_path, "r") as f:
                print(f"   Content:\n{f.read().strip()}")

    await sandbox.close()


# ==============================================================================
# DEMO 2: IMAGE PROCESSING (RUNTIME FILES & tools)
#
# Best for: Dynamic tasks, Agentic workflows
# Architecture:
#   - Input:  Injected via 'files' param -> /workspace/mnt/files
#   - Logic:  Python calls back to Host for instructions
# ==============================================================================


def get_watermark_text() -> str:
    print("    [CALLBACK] Sandbox asked for watermark text.")
    return "CONFIDENTIAL"


async def demo_image_processing():
    print("\n" + "=" * 80)
    print("--- DEMO 2: Image Processing (Runtime Files + tools) ---")
    print("=" * 80)

    output_dir = reset_dir("./demo2_output")

    # 1. Create a source image on Host
    img = Image.new("RGB", (400, 100), color=(73, 109, 137))
    d = ImageDraw.Draw(img)
    d.text((10, 10), "Hello World", fill=(255, 255, 0))
    img_path = "source_image.png"
    img.save(img_path)
    print(f"[HOST] Created source image: {img_path}")

    # 2. Async Context Manager (Auto-Close)
    # We pass 'output_dir' so we can see the result easily on disk
    print("[HOST] Launching Sandbox...")
    async with PythonSandbox(
        output_dir=output_dir,
        tools=[get_watermark_text],
        allow_net=True,
        auto_load_packages=True,
    ) as sandbox:

        # 3. Execute with File Injection
        # 'files' argument copies file to staging and mounts it at /workspace/mnt/files
        print("[HOST] Sending image and executing code...")

        code = """
from PIL import Image, ImageDraw
import os

# 1. Access injected file
# Loose files always appear in 'mnt/files/'
input_path = "mnt/files/source_image.png"
print(f"Opening {input_path}...")

img = Image.open(input_path)
draw = ImageDraw.Draw(img)

# 2. Ask Host for Watermark
text = await get_watermark_text()

# 3. Draw
draw.text((10, 50), text, fill=(255, 0, 0))

# 4. Save to Output (Host Disk)
img.save("watermarked.png")
print("Saved watermarked.png")
"""
        # Pass the file here. It is staged dynamically.
        result = await sandbox.execute_python(code, files=[img_path])

        if result.error:
            print(f"❌ Error: {result.error}")
        else:
            # Check Output
            out_file = os.path.join(output_dir, "watermarked.png")
            print(f"✅ [HOST] Result saved to: {out_file}")
            print(f"   Files created: {result.files}")

    # Cleanup local temp source
    if os.path.exists(img_path):
        os.remove(img_path)


# ==============================================================================
# DEMO 3: AGENTIC WEB SCRAPER (CONFIG INJECTION + NETWORK)
#
# Best for: AI Agents, Web Tasks
# Architecture:
#   - Config: Injected via __init__ files -> /workspace/mnt/files
#   - Network: Enabled
#   - Proxy:  Host handles complex logging
# ==============================================================================


async def fetch_url(url: str) -> str:
    print(f"    [CALLBACK] Fetching {url}...")
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        return resp.text[:200] + "..."  # Truncate for demo


async def demo_web_scraper():
    print("\n" + "=" * 80)
    print("--- DEMO 3: Web Scraper (Config Injection + Network) ---")
    print("=" * 80)

    output_dir = reset_dir("./demo3_output")

    # 1. Create a Config File
    config = {"target": "https://example.com", "extract": "h1"}
    config_path = "scraper_config.json"
    with open(config_path, "w") as f:
        json.dump(config, f)

    # 2. Init Sandbox with Files
    # We pass files in __init__ so they are ready immediately
    print("[HOST] Initializing with Config File...")
    sandbox = PythonSandbox(
        files=[config_path],  # -> /workspace/mnt/files/scraper_config.json
        output_dir=output_dir,
        tools=[fetch_url],
        packages=["beautifulsoup4"],
    )
    await sandbox.connect()

    # 3. Run Script
    code = """
import json
from bs4 import BeautifulSoup

# 1. Load Config from injected file
with open("mnt/files/scraper_config.json") as f:
    config = json.load(f)

url = config["target"]
print(f"Target URL: {url}")

# 2. Call Host to fetch HTML (simulated network or real)
html = await fetch_url(url)

# 3. Process
soup = BeautifulSoup(html, "html.parser")
title = soup.title.string if soup.title else "No Title"

# 4. Save Result
with open("scraping_result.txt", "w") as f:
    f.write(f"URL: {url}\\nTitle: {title}")
"""
    await sandbox.execute_python(code)

    # Verify
    res_path = os.path.join(output_dir, "scraping_result.txt")
    if os.path.exists(res_path):
        print(f"✅ [HOST] Scraped data saved to: {res_path}")
        with open(res_path) as f:
            print(f.read())

    await sandbox.close()
    if os.path.exists(config_path):
        os.remove(config_path)


async def main():
    await demo_pandas_mounts()
    await demo_image_processing()
    await demo_web_scraper()


if __name__ == "__main__":
    asyncio.run(main())
