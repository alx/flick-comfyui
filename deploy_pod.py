#!/usr/bin/env python3
# /// script
# dependencies = ["runpod"]
# ///
"""
deploy_pod.py — Deploy the flick-comfyui pod on RunPod.

Polls live GPU availability, picks the cheapest available 24 GB+ GPU
from a ranked preference list, creates the network volume in the same
datacenter, and deploys the pod automatically.

A "flick-models" network volume (60 GB) is created automatically if none
exists. The pod and volume are always placed in the same datacenter.

Usage:
    uv run --script deploy_pod.py [--network-volume-id <id>] [--poll-interval <s>] [--dry-run]

Requirements:
    RUNPOD_API_KEY in environment or .env file in repo root / parent dir.

Options:
    --network-volume-id  Attach a specific existing network volume (overrides
                         auto-create logic; its datacenter is used for the pod).
    --poll-interval      Seconds between availability checks (default: 60).
    --dry-run            Show which GPU would be selected without deploying.
"""

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

import runpod
from runpod.api.graphql import run_graphql_query
from runpod import error as runpod_error

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TEMPLATE_ID = "3i5suq2fr4"   # flick-comfyui
CONTAINER_DISK_GB = 20
MIN_VRAM_GB = 24
VOLUME_NAME = "flick-models"
VOLUME_SIZE_GB = 60
DEFAULT_POLL_INTERVAL = 60

# Ranked preference: first available wins. Approx on-demand price for reference.
GPU_PREFERENCE = [
    ("RTX 4090",               0.44),
    ("RTX 5090",               0.79),
    ("L40S",                   0.89),
    ("L40",                    0.69),
    ("RTX A6000",              0.50),
    ("RTX 6000 Ada",           0.89),
    ("RTX PRO 6000 MaxQ",      0.79),
    ("RTX PRO 6000",           0.89),
    ("A40",                    0.55),
    ("A100 SXM 80",            1.89),
    ("A100 PCIe 80",           1.09),
    ("RTX 3090 Ti",            0.35),
    ("RTX 3090",               0.30),
    ("RTX A5000",              0.32),
    ("RTX 5000 Ada",           0.55),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_api_key() -> None:
    key = os.environ.get("RUNPOD_API_KEY", "")
    if not key:
        for candidate in [
            Path(__file__).parent.parent.parent / "flick" / ".env",
            Path(__file__).parent.parent / ".env",
            Path(__file__).parent / ".env",
            Path.home() / ".env",
        ]:
            if candidate.exists():
                for line in candidate.read_text().splitlines():
                    if line.startswith("RUNPOD_API_KEY="):
                        key = line.split("=", 1)[1].strip()
                        break
            if key:
                break
    if not key:
        print("ERROR: RUNPOD_API_KEY not found. Set it in env or .env file.")
        sys.exit(1)
    runpod.api_key = key


def gql(query: str) -> dict:
    return run_graphql_query(query)["data"]


# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

GPU_QUERY = """
{ gpuTypes {
    id displayName memoryInGb secureCloud communityCloud
    lowestPrice(input: { gpuCount: 1 }) { stockStatus }
    nodeGroupDatacenters { id }
} }
"""

VOLUME_QUERY = "{ myself { networkVolumes { id name size dataCenterId } } }"


def _slot_query(gpu_id: str, dc_id: str) -> str:
    return f"""
    {{ gpuTypes(input: {{ id: "{gpu_id}" }}) {{
        lowestPrice(input: {{ gpuCount: 1, dataCenterId: "{dc_id}" }}) {{
            stockStatus
            availableGpuCounts
        }}
    }} }}
    """


def _create_volume_mutation(name: str, size: int, dc_id: str) -> str:
    return f"""
    mutation {{
        createNetworkVolume(input: {{ name: "{name}", size: {size}, dataCenterId: "{dc_id}" }}) {{
            id name size dataCenterId
        }}
    }}
    """


def _delete_volume_mutation(volume_id: str) -> str:
    return f"""
    mutation {{
        deleteNetworkVolume(input: {{ id: "{volume_id}" }})
    }}
    """


# ---------------------------------------------------------------------------
# Volume management
# ---------------------------------------------------------------------------

def ensure_volume(override_id: Optional[str], datacenter: str) -> tuple[str, str, bool]:
    """Return (volume_id, datacenter_id, freshly_created)."""
    vols = gql(VOLUME_QUERY)["myself"]["networkVolumes"]

    if override_id:
        match = next((v for v in vols if v["id"] == override_id), None)
        if not match:
            raise runpod_error.QueryError(f"volume {override_id} not found in your account.", "")
        print(f"Using specified volume: {match['name']} ({match['id']})  dc={match['dataCenterId']}")
        return match["id"], match["dataCenterId"], False

    existing = next((v for v in vols if v["name"] == VOLUME_NAME), None)
    if existing:
        print(f"Found existing volume '{VOLUME_NAME}': {existing['id']}  {existing['size']} GB  dc={existing['dataCenterId']}")
        return existing["id"], existing["dataCenterId"], False

    if vols:
        print("\nExisting network volumes (none named 'flick-models'):")
        for v in vols:
            print(f"  {v['id']}  {v['name']}  {v['size']} GB  dc={v['dataCenterId']}")

    print(f"\nCreating '{VOLUME_NAME}' network volume ({VOLUME_SIZE_GB} GB) in datacenter {datacenter}...")
    result = gql(_create_volume_mutation(VOLUME_NAME, VOLUME_SIZE_GB, datacenter))
    vol = result["createNetworkVolume"]
    print(f"Created volume: {vol['id']}  {vol['name']}  {vol['size']} GB  dc={vol['dataCenterId']}")
    return vol["id"], vol["dataCenterId"], True


def delete_volume(volume_id: str) -> None:
    print(f"Deleting volume {volume_id}...")
    gql(_delete_volume_mutation(volume_id))
    print(f"Volume {volume_id} deleted.")


# ---------------------------------------------------------------------------
# GPU availability
# ---------------------------------------------------------------------------

def _check_slot(gpu_id: str, dc_id: str) -> bool:
    """Return True if a real-time slot is available for gpu_id in dc_id."""
    try:
        data = gql(_slot_query(gpu_id, dc_id))
    except runpod_error.QueryError:
        return False
    types = data.get("gpuTypes") or []
    lp = (types[0].get("lowestPrice") if types else None) or {}
    stock = lp.get("stockStatus", "OUT_OF_STOCK")
    counts = lp.get("availableGpuCounts") or []
    return stock != "OUT_OF_STOCK" and sum(c for c in counts if c and c > 0) > 0


def find_available_gpu(gpu_data: list[dict]) -> Optional[tuple[dict, str, float]]:
    """Return (gpu, datacenter_id, price) for the first preferred GPU+DC with a confirmed slot."""
    by_name: dict[str, dict] = {}
    for g in gpu_data:
        if g["memoryInGb"] < MIN_VRAM_GB:
            continue
        stock = (g.get("lowestPrice") or {}).get("stockStatus", "OUT_OF_STOCK")
        if stock == "OUT_OF_STOCK":
            continue
        by_name[g["displayName"]] = g

    # Only try listed GPUs; unlisted ones are not considered
    ordered = [(name, price) for name, price in GPU_PREFERENCE if name in by_name]

    for name, price in ordered:
        g = by_name[name]
        for dc in g.get("nodeGroupDatacenters") or []:
            if _check_slot(g["id"], dc["id"]):
                return g, dc["id"], price

    return None


def print_gpu_table(gpu_data: list[dict]) -> None:
    by_name: dict[str, dict] = {}
    for g in gpu_data:
        if g["memoryInGb"] >= MIN_VRAM_GB:
            by_name[g["displayName"]] = g

    preference_map = dict(GPU_PREFERENCE)
    shown: set[str] = set()

    for name, price in GPU_PREFERENCE:
        if name not in by_name:
            continue
        g = by_name[name]
        stock = (g.get("lowestPrice") or {}).get("stockStatus", "OUT_OF_STOCK")
        if stock == "OUT_OF_STOCK":
            continue
        stock_tag = "HIGH" if stock == "HIGH_AVAILABILITY" else "LOW"
        cloud = "secure+community" if g["secureCloud"] and g["communityCloud"] else ("secure" if g["secureCloud"] else "community")
        print(f"  ✓  {name:40} {g['memoryInGb']:3} GB   ~${price:.2f}/hr  [{cloud}] [{stock_tag}]")
        shown.add(name)

    for name, g in sorted(by_name.items(), key=lambda x: x[1]["memoryInGb"]):
        if name in shown or name in preference_map:
            continue
        stock = (g.get("lowestPrice") or {}).get("stockStatus", "OUT_OF_STOCK")
        if stock == "OUT_OF_STOCK":
            continue
        stock_tag = "HIGH" if stock == "HIGH_AVAILABILITY" else "LOW"
        cloud = "secure" if g["secureCloud"] else "community"
        print(f"  ?  {name:40} {g['memoryInGb']:3} GB   [unlisted]  [{cloud}] [{stock_tag}]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy flick-comfyui pod on RunPod")
    parser.add_argument("--network-volume-id", help="Use a specific existing network volume ID")
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL,
                        help=f"Seconds between availability checks (default: {DEFAULT_POLL_INTERVAL})")
    parser.add_argument("--dry-run", action="store_true", help="Show GPU selection without deploying")
    args = parser.parse_args()

    load_api_key()
    print("Authenticated with RunPod API")

    pod = None
    while pod is None:
        print("\nQuerying live GPU availability...")
        gpu_data = gql(GPU_QUERY)["gpuTypes"]

        print(f"\nAvailable GPUs ({MIN_VRAM_GB}+ GB VRAM):")
        print_gpu_table(gpu_data)

        hit = find_available_gpu(gpu_data)
        if not hit:
            if args.dry_run:
                print(f"\nNo GPU with {MIN_VRAM_GB}+ GB VRAM available in any datacenter right now.")
                return
            print(f"\nNo GPU available yet. Retrying in {args.poll_interval}s... (Ctrl-C to abort)")
            time.sleep(args.poll_interval)
            continue

        selected_gpu, selected_dc, selected_price = hit
        cloud_type = "SECURE" if selected_gpu["secureCloud"] else "COMMUNITY"
        print(f"\nSelected: {selected_gpu['displayName']}  {selected_gpu['memoryInGb']} GB"
              f"  ~${selected_price:.2f}/hr  [{cloud_type}]  dc={selected_dc}")

        if args.dry_run:
            print("\n[dry-run] Skipping deployment.")
            return

        volume_id = None
        volume_created = False
        try:
            volume_id, volume_dc, volume_created = ensure_volume(args.network_volume_id, selected_dc)

            print("Deploying...")
            pod = runpod.create_pod(
                name="flick-comfyui",
                gpu_type_id=selected_gpu["id"],
                cloud_type=cloud_type,
                data_center_id=volume_dc,
                gpu_count=1,
                container_disk_in_gb=CONTAINER_DISK_GB,
                start_ssh=True,
                template_id=TEMPLATE_ID,
                network_volume_id=volume_id,
            )
        except (runpod_error.QueryError, runpod_error.AuthenticationError) as e:
            if volume_created and volume_id:
                delete_volume(volume_id)
            print(f"  Failed ({e}). Retrying in {args.poll_interval}s... (Ctrl-C to abort)")
            time.sleep(args.poll_interval)

    pod_id = pod["id"]
    print(f"\n{'='*52}")
    print(f"Pod deployed successfully!")
    print(f"  ID:      {pod_id}")
    print(f"  Name:    flick-comfyui")
    print(f"\nFrontend URL (ready after ~2 min, or ~30 min first start):")
    print(f"  https://{pod_id}-8000.proxy.runpod.net")
    print(f"\nMonitor logs:")
    print(f"  https://www.runpod.io/console/pods/{pod_id}")
    print(f"\nVerify once ready:")
    print(f"  python scripts/verify.py https://{pod_id}-8000.proxy.runpod.net")


if __name__ == "__main__":
    main()
