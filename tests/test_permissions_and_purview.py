"""
Test script for SharePoint permissions sync and Purview/RMS label detection.

Validates that the app registration can:
1. Read file permissions from SharePoint via Graph API
2. Detect sensitivity labels on files (Purview)
3. Read tenant sensitivity label definitions
4. Extract RMS protection permissions
5. Merge SP + RMS permissions correctly

Usage:
    # Test permissions only
    python test_permissions_and_purview.py --test permissions

    # Test Purview/RMS only
    python test_permissions_and_purview.py --test purview

    # Test both (default)
    python test_permissions_and_purview.py --test all

    # Dry run: just check API connectivity, don't write anything
    python test_permissions_and_purview.py --test all --verbose

Environment variables required (in .env or exported):
    AZURE_CLIENT_ID       - App registration client ID (M365 tenant)
    AZURE_CLIENT_SECRET   - App registration client secret
    AZURE_TENANT_ID       - M365 tenant ID
    SHAREPOINT_SITE_URL   - e.g. https://m365x33469201.sharepoint.com/sites/demorag
    SHAREPOINT_DRIVE_NAME - e.g. "Documents" or "Shared Documents"
"""

import asyncio
import argparse
import json
import os
import sys
from datetime import datetime

# Add sync directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sync"))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "sync", ".env"))  # Try sync/.env
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))          # Try root .env

from sharepoint_client import SharePointClient, SharePointFile
from permissions_sync import PermissionsClient, FilePermissions, permissions_to_summary
from purview_client import (
    PurviewClient,
    FileProtectionInfo,
    ProtectionStatus,
    merge_permissions_for_search,
    is_purview_sync_enabled,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def print_header(title: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def print_section(title: str) -> None:
    print(f"\n--- {title} ---")


def print_ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def print_fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def print_info(msg: str) -> None:
    print(f"  [INFO] {msg}")


def print_warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def check_env_vars() -> bool:
    """Validate required environment variables are set."""
    required = {
        "AZURE_CLIENT_ID": os.environ.get("AZURE_CLIENT_ID"),
        "AZURE_CLIENT_SECRET": os.environ.get("AZURE_CLIENT_SECRET"),
        "AZURE_TENANT_ID": os.environ.get("AZURE_TENANT_ID"),
        "SHAREPOINT_SITE_URL": os.environ.get("SHAREPOINT_SITE_URL"),
    }

    missing = [k for k, v in required.items() if not v]
    if missing:
        print_fail(f"Missing environment variables: {', '.join(missing)}")
        print_info("Set them in sync/.env or export them before running this script.")
        return False

    print_ok(f"AZURE_TENANT_ID = {required['AZURE_TENANT_ID']}")
    print_ok(f"AZURE_CLIENT_ID = {required['AZURE_CLIENT_ID'][:8]}...")
    print_ok(f"SHAREPOINT_SITE_URL = {required['SHAREPOINT_SITE_URL']}")
    print_ok(f"SHAREPOINT_DRIVE_NAME = {os.environ.get('SHAREPOINT_DRIVE_NAME', 'Documents')}")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Permissions Sync
# ─────────────────────────────────────────────────────────────────────────────

async def test_permissions(verbose: bool = False) -> bool:
    """
    Test that the app can fetch permissions from SharePoint files.
    
    Steps:
    1. Connect to SharePoint and list files
    2. For each file, fetch permissions via Graph API
    3. Show the extracted user_ids, group_ids, and permission details
    """
    print_header("TEST: SharePoint Permissions Sync")

    site_url = os.environ.get("SHAREPOINT_SITE_URL", "")
    drive_name = os.environ.get("SHAREPOINT_DRIVE_NAME", "Documents")
    folder_path = os.environ.get("SHAREPOINT_FOLDER_PATH", "/")

    # Step 1: Connect and list files
    print_section("Step 1: Connect to SharePoint and list files")
    try:
        async with SharePointClient(site_url, drive_name) as sp_client:
            site_id, drive_id = sp_client.get_resolved_ids()
            print_ok(f"Connected! site_id={site_id[:20]}..., drive_id={drive_id[:20]}...")

            files = []
            async for f in sp_client.list_files(folder_path):
                files.append(f)
            print_ok(f"Found {len(files)} file(s) in '{folder_path}'")

            if not files:
                print_warn("No files found. Cannot test permissions.")
                return True

            for f in files:
                print_info(f"  - {f.path}  (id={f.id[:12]}..., size={f.size})")

            # Step 2: Fetch permissions
            print_section("Step 2: Fetch permissions via Graph API")
            async with PermissionsClient(drive_id) as perm_client:
                for f in files:
                    print(f"\n  File: {f.path}")
                    try:
                        file_perms: FilePermissions = await perm_client.get_file_permissions(
                            file_id=f.id,
                            file_path=f.path,
                        )

                        if not file_perms.permissions:
                            print_warn("    No permissions returned (may need Sites.Read.All or Sites.Selected)")
                            continue

                        print_ok(f"    {len(file_perms.permissions)} permission(s) found")

                        for perm in file_perms.permissions:
                            role_str = ",".join(perm.roles)
                            id_str = perm.identity_id or "no-entra-id"
                            print(f"      [{perm.identity_type}] {perm.display_name} "
                                  f"({perm.email or 'no-email'}) → roles={role_str}, "
                                  f"entra_id={id_str}, inherited={perm.inherited}")

                        # Step 3: Show extracted ACL metadata
                        metadata = file_perms.to_metadata()
                        user_ids = metadata.get("user_ids", "")
                        group_ids = metadata.get("group_ids", "")

                        print_section(f"  ACL Metadata for '{f.path}'")
                        print_info(f"    user_ids  = {user_ids}")
                        print_info(f"    group_ids = {group_ids}")
                        print_info(f"    permissions_hash = {metadata.get('permissions_hash', 'N/A')}")

                        if verbose:
                            print_info(f"    Full permissions JSON:")
                            perms_data = json.loads(metadata.get("sharepoint_permissions", "[]"))
                            print(json.dumps(perms_data, indent=6))

                    except Exception as e:
                        print_fail(f"    Error fetching permissions: {e}")

            print_ok("Permissions test completed!")
            return True

    except Exception as e:
        print_fail(f"Failed to connect to SharePoint: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Purview / RMS
# ─────────────────────────────────────────────────────────────────────────────

async def test_purview(verbose: bool = False) -> bool:
    """
    Test Purview sensitivity label detection and RMS permission extraction.
    
    Steps:
    1. Connect and load tenant sensitivity label definitions
    2. For each file, check if it has a sensitivity label
    3. If label has encryption (RMS), extract protection permissions
    4. Show the SP ∩ RMS permission merge result
    """
    print_header("TEST: Purview / RMS Protection Detection")

    site_url = os.environ.get("SHAREPOINT_SITE_URL", "")
    drive_name = os.environ.get("SHAREPOINT_DRIVE_NAME", "Documents")
    folder_path = os.environ.get("SHAREPOINT_FOLDER_PATH", "/")

    # Step 1: Connect and get files + drive_id
    print_section("Step 1: Connect to SharePoint")
    try:
        async with SharePointClient(site_url, drive_name) as sp_client:
            site_id, drive_id = sp_client.get_resolved_ids()
            print_ok(f"Connected! drive_id={drive_id[:20]}...")

            files = []
            async for f in sp_client.list_files(folder_path):
                files.append(f)
            print_ok(f"Found {len(files)} file(s)")

            if not files:
                print_warn("No files found. Cannot test Purview.")
                return True

            # Step 2: Initialize Purview client and load labels
            print_section("Step 2: Load tenant sensitivity labels")
            try:
                async with PurviewClient(drive_id) as purview_client:
                    label_count = len(purview_client._label_cache)
                    if label_count > 0:
                        print_ok(f"Loaded {label_count} sensitivity label(s) from tenant")
                        for lid, linfo in purview_client._label_cache.items():
                            enc_flag = "ENCRYPTED" if linfo.is_encrypted else "no-encryption"
                            parent = f" (parent: {linfo.parent_label_name})" if linfo.parent_label_name else ""
                            print_info(f"    Label: '{linfo.label_name}'{parent} → {enc_flag} [id={lid[:12]}...]")
                    else:
                        print_warn(
                            "No labels loaded. Possible causes:\n"
                            "      - App lacks InformationProtectionPolicy.Read.All permission\n"
                            "      - Tenant has no Purview sensitivity labels configured\n"
                            "      - M365 E5/E3+Compliance license not assigned"
                        )

                    # Step 3: Check each file for protection
                    print_section("Step 3: Check files for sensitivity labels & RMS")

                    stats = {
                        "unprotected": 0,
                        "label_only": 0,
                        "protected": 0,
                        "unknown": 0,
                    }

                    for f in files:
                        print(f"\n  File: {f.path}")
                        try:
                            protection: FileProtectionInfo = await purview_client.get_file_protection(
                                file_id=f.id,
                                file_path=f.path,
                            )

                            status_str = protection.status.value
                            stats[status_str] = stats.get(status_str, 0) + 1

                            if protection.status == ProtectionStatus.UNPROTECTED:
                                print_info(f"    Status: UNPROTECTED (no sensitivity label)")

                            elif protection.status == ProtectionStatus.LABEL_ONLY:
                                label = protection.sensitivity_label
                                print_ok(f"    Status: LABEL_ONLY (label without encryption)")
                                print_info(f"    Label:  '{label.label_name}' (id={label.label_id[:12]}...)")
                                print_info(f"    Method: {label.assignment_method}")

                            elif protection.status == ProtectionStatus.PROTECTED:
                                label = protection.sensitivity_label
                                print_ok(f"    Status: PROTECTED (RMS encrypted)")
                                print_info(f"    Label:  '{label.label_name}' (id={label.label_id[:12]}...)")
                                print_info(f"    Encrypted: {label.is_encrypted}")

                                if protection.rms_permissions:
                                    print_info(f"    RMS Permissions ({len(protection.rms_permissions)} entries):")
                                    for rp in protection.rms_permissions:
                                        rights = ", ".join(rp.usage_rights[:5])
                                        if len(rp.usage_rights) > 5:
                                            rights += "..."
                                        print(f"        [{rp.identity_type}] {rp.display_name} "
                                              f"({rp.identity}) → {rights}")
                                        if rp.entra_object_id:
                                            print(f"            entra_id = {rp.entra_object_id}")
                                else:
                                    print_warn("    No RMS permissions extracted "
                                              "(extractSensitivityLabels may not be available)")

                                # Step 4: Show merged permissions
                                print_section(f"  Permission Merge for '{f.path}'")
                                # Get SP permissions to merge
                                async with PermissionsClient(drive_id) as perm_client:
                                    sp_perms = await perm_client.get_file_permissions(f.id, f.path)

                                sp_user_ids = sp_perms._extract_user_ids()
                                sp_group_ids = sp_perms._extract_group_ids()

                                eff_users, eff_groups = merge_permissions_for_search(
                                    sp_user_ids, sp_group_ids, protection
                                )

                                print_info(f"    SP user_ids:        {sp_user_ids}")
                                print_info(f"    SP group_ids:       {sp_group_ids}")
                                rms_user_ids = protection.get_user_ids_with_view_access()
                                rms_group_ids = protection.get_group_ids_with_view_access()
                                print_info(f"    RMS user_ids:       {rms_user_ids}")
                                print_info(f"    RMS group_ids:      {rms_group_ids}")
                                print_info(f"    Effective user_ids: {eff_users}")
                                print_info(f"    Effective group_ids:{eff_groups}")

                            elif protection.status == ProtectionStatus.UNKNOWN:
                                print_warn(f"    Status: UNKNOWN (could not determine)")

                            # Show metadata that would be stored on blob
                            if verbose and protection.sensitivity_label:
                                meta = protection.to_metadata()
                                print_info(f"    Blob metadata that would be written:")
                                for k, v in meta.items():
                                    val_preview = v[:100] + "..." if len(v) > 100 else v
                                    print(f"        {k} = {val_preview}")

                        except Exception as e:
                            print_fail(f"    Error: {e}")
                            import traceback
                            traceback.print_exc()

                    # Summary
                    print_section("Summary")
                    print_info(f"  Unprotected (no label): {stats['unprotected']}")
                    print_info(f"  Label only (no RMS):    {stats['label_only']}")
                    print_info(f"  Protected (RMS):        {stats['protected']}")
                    print_info(f"  Unknown:                {stats['unknown']}")

                    print_ok("Purview/RMS test completed!")
                    return True

            except Exception as e:
                print_fail(f"Failed to initialize Purview client: {e}")
                import traceback
                traceback.print_exc()
                return False

    except Exception as e:
        print_fail(f"Failed to connect to SharePoint: {e}")
        import traceback
        traceback.print_exc()
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: App Registration API Permissions Check
# ─────────────────────────────────────────────────────────────────────────────

async def test_api_permissions() -> bool:
    """
    Quick diagnostic: test which Graph API scopes the app registration has.
    Tries each relevant endpoint and reports success/failure.
    """
    print_header("TEST: App Registration API Permission Check")

    import httpx
    from azure.identity.aio import ClientSecretCredential

    client_id = os.environ.get("AZURE_CLIENT_ID", "")
    client_secret = os.environ.get("AZURE_CLIENT_SECRET", "")
    tenant_id = os.environ.get("AZURE_TENANT_ID", "")
    site_url = os.environ.get("SHAREPOINT_SITE_URL", "")

    if not all([client_id, client_secret, tenant_id, site_url]):
        print_fail("Missing required environment variables")
        return False

    # Parse site URL to get hostname and site path
    from urllib.parse import urlparse
    parsed = urlparse(site_url)
    hostname = parsed.netloc
    site_path = parsed.path  # e.g., /sites/demorag

    credential = ClientSecretCredential(tenant_id, client_id, client_secret)
    try:
        token = await credential.get_token("https://graph.microsoft.com/.default")
        headers = {"Authorization": f"Bearer {token.token}"}
    except Exception as e:
        print_fail(f"Failed to acquire token: {e}")
        await credential.close()
        return False

    endpoints = [
        {
            "name": "Sites.Read.All / Sites.Selected",
            "url": f"https://graph.microsoft.com/v1.0/sites/{hostname}:{site_path}",
            "description": "Read SharePoint site metadata",
        },
        {
            "name": "Files.Read.All / Sites.Selected",
            "url": None,  # Will be set dynamically after getting drive_id
            "description": "List items in document library",
        },
        {
            "name": "InformationProtectionPolicy.Read.All",
            "url": "https://graph.microsoft.com/v1.0/security/informationProtection/sensitivityLabels",
            "description": "Read tenant sensitivity label definitions",
        },
    ]

    async with httpx.AsyncClient(timeout=30.0) as http:
        # Test site access
        ep = endpoints[0]
        print(f"\n  Testing: {ep['name']}")
        print(f"    → {ep['description']}")
        resp = await http.get(ep["url"], headers=headers)
        if resp.status_code == 200:
            site_data = resp.json()
            site_id = site_data.get("id", "")
            print_ok(f"    Status: {resp.status_code} — site_id={site_id[:30]}...")
        else:
            print_fail(f"    Status: {resp.status_code} — {resp.text[:200]}")
            await credential.close()
            return False

        # Get drive_id for further tests
        drives_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives"
        resp = await http.get(drives_url, headers=headers)
        drive_id = None
        if resp.status_code == 200:
            drives = resp.json().get("value", [])
            drive_name = os.environ.get("SHAREPOINT_DRIVE_NAME", "Documents")
            for d in drives:
                if d.get("name") == drive_name or d.get("name") == "Shared Documents":
                    drive_id = d.get("id")
                    break
            if not drive_id and drives:
                drive_id = drives[0].get("id")

        if drive_id:
            # Test file listing
            ep = endpoints[1]
            ep["url"] = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/children"
            print(f"\n  Testing: {ep['name']}")
            print(f"    → {ep['description']}")
            resp = await http.get(ep["url"], headers=headers)
            if resp.status_code == 200:
                items = resp.json().get("value", [])
                print_ok(f"    Status: {resp.status_code} — {len(items)} item(s) in root")
            else:
                print_fail(f"    Status: {resp.status_code} — {resp.text[:200]}")

            # Test file permissions on first file
            first_file_id = None
            for item in items:
                if item.get("file"):  # It's a file, not a folder
                    first_file_id = item["id"]
                    first_file_name = item.get("name", "")
                    break

            if first_file_id:
                perm_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{first_file_id}/permissions"
                print(f"\n  Testing: File permissions (on '{first_file_name}')")
                print(f"    → GET /drives/.../items/.../permissions")
                resp = await http.get(perm_url, headers=headers)
                if resp.status_code == 200:
                    perms = resp.json().get("value", [])
                    print_ok(f"    Status: {resp.status_code} — {len(perms)} permission(s)")
                    for p in perms:
                        roles = p.get("roles", [])
                        gtv2 = p.get("grantedToV2", {})
                        user = gtv2.get("user", {})
                        group = gtv2.get("group", {})
                        sg = gtv2.get("siteGroup", {})
                        name = user.get("displayName") or group.get("displayName") or sg.get("displayName") or "?"
                        print_info(f"      {name} → roles={roles}")
                else:
                    print_fail(f"    Status: {resp.status_code} — {resp.text[:200]}")

                # Test sensitivity label on file
                label_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{first_file_id}?$select=id,name,sensitivityLabel"
                print(f"\n  Testing: Sensitivity label on file (on '{first_file_name}')")
                print(f"    → GET /drives/.../items/...?$select=sensitivityLabel")
                resp = await http.get(label_url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    label = data.get("sensitivityLabel")
                    if label:
                        print_ok(f"    Label found: '{label.get('displayName', '?')}' "
                                f"(id={label.get('labelId', '?')[:12]}...)")
                    else:
                        print_info(f"    No sensitivity label on this file")
                else:
                    print_fail(f"    Status: {resp.status_code}")

        # Test sensitivity labels endpoint
        ep = endpoints[2]
        print(f"\n  Testing: {ep['name']}")
        print(f"    → {ep['description']}")
        resp = await http.get(ep["url"], headers=headers)
        if resp.status_code == 200:
            labels = resp.json().get("value", [])
            print_ok(f"    Status: {resp.status_code} — {len(labels)} label(s) found")
            for label in labels:
                print_info(f"      '{label.get('name')}' (id={label.get('id', '')[:12]}...)")
        elif resp.status_code == 403:
            print_warn(f"    Status: 403 — Need InformationProtectionPolicy.Read.All permission")
            print_info("      Add this API permission in Azure Portal → App registrations → API permissions")
        else:
            print_fail(f"    Status: {resp.status_code} — {resp.text[:200]}")

    await credential.close()
    print_ok("\nAPI permission check completed!")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(
        description="Test SharePoint permissions sync and Purview/RMS detection"
    )
    parser.add_argument(
        "--test",
        choices=["all", "permissions", "purview", "api-check"],
        default="all",
        help="Which test to run (default: all)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed output including full JSON",
    )
    args = parser.parse_args()

    print_header("SharePoint Permissions & Purview Test Suite")
    print_info(f"Time: {datetime.now().isoformat()}")
    print_info(f"Test: {args.test}")

    # Check environment
    print_section("Environment Check")
    if not check_env_vars():
        sys.exit(1)

    results = {}

    if args.test in ("all", "api-check"):
        results["api-check"] = await test_api_permissions()

    if args.test in ("all", "permissions"):
        results["permissions"] = await test_permissions(verbose=args.verbose)

    if args.test in ("all", "purview"):
        results["purview"] = await test_purview(verbose=args.verbose)

    # Final report
    print_header("RESULTS")
    all_passed = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")
        if not passed:
            all_passed = False

    if all_passed:
        print("\nAll tests passed!")
    else:
        print("\nSome tests failed — see details above.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
