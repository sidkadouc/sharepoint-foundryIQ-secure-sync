"""
Apply a sensitivity label to a SharePoint file and run full Purview test.

This script:
1. Lists available sensitivity labels
2. Applies the "Highly Confidential" label to one file
3. Re-runs the Purview detection to verify it's picked up
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sync"))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "sync", ".env"))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import httpx
from azure.identity.aio import ClientSecretCredential


def p(tag, msg):
    print(f"  [{tag}] {msg}")


async def main():
    client_id = os.environ["AZURE_CLIENT_ID"]
    client_secret = os.environ["AZURE_CLIENT_SECRET"]
    tenant_id = os.environ["AZURE_TENANT_ID"]
    site_url = os.environ.get("SHAREPOINT_SITE_URL", "")
    drive_name = os.environ.get("SHAREPOINT_DRIVE_NAME", "Documents")

    from urllib.parse import urlparse
    parsed = urlparse(site_url)
    hostname = parsed.netloc
    site_path = parsed.path

    cred = ClientSecretCredential(tenant_id, client_id, client_secret)
    token = await cred.get_token("https://graph.microsoft.com/.default")
    headers = {"Authorization": f"Bearer {token.token}"}

    async with httpx.AsyncClient(timeout=60.0) as http:
        # ── Step 1: Get site & drive ──
        print("\n=== Step 1: Resolve site & drive ===")
        resp = await http.get(
            f"https://graph.microsoft.com/v1.0/sites/{hostname}:{site_path}",
            headers=headers,
        )
        site_id = resp.json()["id"]
        p("OK", f"site_id={site_id[:40]}...")

        resp = await http.get(
            f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives",
            headers=headers,
        )
        drives = resp.json().get("value", [])
        drive_id = None
        for d in drives:
            if d["name"] == drive_name or d["name"] == "Shared Documents":
                drive_id = d["id"]
                break
        if not drive_id and drives:
            drive_id = drives[0]["id"]
        p("OK", f"drive_id={drive_id[:40]}...")

        # ── Step 2: List files ──
        print("\n=== Step 2: List files ===")
        resp = await http.get(
            f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/children",
            headers=headers,
        )
        items = resp.json().get("value", [])
        # Also check subfolders
        all_files = []
        for item in items:
            if item.get("file"):
                all_files.append(item)
            elif item.get("folder"):
                # List children of the folder
                resp2 = await http.get(
                    f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item['id']}/children",
                    headers=headers,
                )
                for child in resp2.json().get("value", []):
                    if child.get("file"):
                        child["_folder"] = item["name"]
                        all_files.append(child)

        for f in all_files:
            folder = f.get("_folder", "root")
            p("FILE", f"{folder}/{f['name']} (id={f['id'][:16]}...)")

        if not all_files:
            p("WARN", "No files found!")
            await cred.close()
            return

        # ── Step 3: List sensitivity labels (beta) ──
        print("\n=== Step 3: List available sensitivity labels ===")
        resp = await http.get(
            "https://graph.microsoft.com/beta/security/informationProtection/sensitivityLabels",
            headers=headers,
        )
        if resp.status_code != 200:
            p("FAIL", f"Cannot list labels: {resp.status_code} - {resp.text[:200]}")
            await cred.close()
            return

        labels = resp.json().get("value", [])
        for lbl in labels:
            p("LABEL", f"'{lbl['name']}' id={lbl['id']} active={lbl.get('isActive')}")

        if not labels:
            p("WARN", "No sensitivity labels configured in tenant!")
            await cred.close()
            return

        # ── Step 4: Apply the "Highly Confidential" label to the FIRST file ──
        print("\n=== Step 4: Apply sensitivity label to file ===")

        # Pick label - prefer "Highly Confidential", else first one
        target_label = None
        for lbl in labels:
            if "highly confidential" in lbl["name"].lower():
                target_label = lbl
                break
        if not target_label:
            target_label = labels[0]

        target_file = all_files[0]
        file_name = f"{target_file.get('_folder', 'root')}/{target_file['name']}"
        p("INFO", f"Applying label '{target_label['name']}' to '{file_name}'")

        # Use beta endpoint: PATCH /drives/{drive-id}/items/{item-id}
        # with sensitivityLabel body
        # Alternatively: POST /drives/{drive-id}/items/{item-id}/assignSensitivityLabel
        assign_url = (
            f"https://graph.microsoft.com/beta/drives/{drive_id}"
            f"/items/{target_file['id']}/assignSensitivityLabel"
        )
        body = {
            "sensitivityLabelId": target_label["id"],
            "assignmentMethod": "standard",
            "justificationText": "Testing Purview integration"
        }

        resp = await http.post(
            assign_url,
            headers={**headers, "Content-Type": "application/json"},
            content=json.dumps(body),
        )

        if resp.status_code in (200, 202, 204):
            p("OK", f"Label applied! Status={resp.status_code}")
        else:
            p("FAIL", f"Could not apply label: {resp.status_code}")
            p("FAIL", f"Response: {resp.text[:500]}")
            p("INFO", "This might need Files.ReadWrite.All or Sites.FullControl.All permission")
            p("INFO", "Or the label may already be applied. Continuing with existing labels...")

        # ── Step 5: Verify label on file ──
        print("\n=== Step 5: Verify sensitivity label on files ===")
        # Wait a moment for label propagation
        await asyncio.sleep(2)

        for f in all_files:
            fname = f"{f.get('_folder', 'root')}/{f['name']}"
            # Try beta endpoint for richer label info
            resp = await http.get(
                f"https://graph.microsoft.com/beta/drives/{drive_id}/items/{f['id']}"
                f"?$select=id,name,sensitivityLabel",
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                label = data.get("sensitivityLabel")
                if label:
                    label_id = label.get("labelId", "?")
                    display = label.get("displayName", "?")
                    method = label.get("assignmentMethod", "?")
                    p("LABELED", f"{fname} → '{display}' (id={label_id[:16]}..., method={method})")
                else:
                    p("NO_LABEL", f"{fname}")
            else:
                p("ERROR", f"{fname} → {resp.status_code}")

        # ── Step 6: Now run the full Purview detection using the updated client ──
        print("\n=== Step 6: Full Purview detection via PurviewClient ===")
        from sharepoint_client import SharePointClient
        from purview_client import PurviewClient, ProtectionStatus
        from permissions_sync import PermissionsClient

        async with SharePointClient(site_url, drive_name) as sp_client:
            _, resolved_drive_id = sp_client.get_resolved_ids()

            files = []
            async for f in sp_client.list_files("/"):
                files.append(f)
            p("OK", f"Found {len(files)} file(s)")

            async with PurviewClient(resolved_drive_id) as purview:
                label_count = len(purview._label_cache)
                p("OK", f"Label cache loaded: {label_count} label(s)")
                for lid, linfo in purview._label_cache.items():
                    enc = "ENCRYPTED" if linfo.is_encrypted else "no-encryption"
                    p("CACHE", f"  '{linfo.label_name}' → {enc}")

                for f in files:
                    print(f"\n  --- File: {f.path} ---")
                    protection = await purview.get_file_protection(f.id, f.path)

                    p("STATUS", f"{protection.status.value}")

                    if protection.sensitivity_label:
                        lbl = protection.sensitivity_label
                        p("LABEL", f"'{lbl.label_name}' encrypted={lbl.is_encrypted}")

                    if protection.status == ProtectionStatus.PROTECTED:
                        p("RMS", f"{len(protection.rms_permissions)} RMS permission entries")
                        for rp in protection.rms_permissions:
                            p("RMS_ENTRY", f"[{rp.identity_type}] {rp.display_name} "
                              f"({rp.identity}) rights={rp.usage_rights[:3]}...")

                        # Show merge with SP permissions
                        async with PermissionsClient(resolved_drive_id) as perm_client:
                            sp_perms = await perm_client.get_file_permissions(f.id, f.path)

                        from purview_client import merge_permissions_for_search
                        sp_uids = sp_perms._extract_user_ids()
                        sp_gids = sp_perms._extract_group_ids()
                        eff_u, eff_g = merge_permissions_for_search(sp_uids, sp_gids, protection)
                        p("MERGE", f"SP users={sp_uids}")
                        p("MERGE", f"RMS users={protection.get_user_ids_with_view_access()}")
                        p("MERGE", f"Effective users={eff_u}")
                        p("MERGE", f"Effective groups={eff_g}")

                    elif protection.status == ProtectionStatus.LABEL_ONLY:
                        p("INFO", "Label without encryption - SP permissions apply directly")

                    # Show what metadata would be written to blob
                    if protection.sensitivity_label:
                        meta = protection.to_metadata()
                        p("META", f"Blob metadata: {json.dumps(meta, indent=2)[:300]}")

    await cred.close()
    print("\n=== DONE ===")


if __name__ == "__main__":
    asyncio.run(main())
