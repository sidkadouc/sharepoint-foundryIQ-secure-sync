"""
Upload a test .docx file to SharePoint so the user can apply a sensitivity label
via Word Online, then verify the label is detected.
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


def create_minimal_docx() -> bytes:
    """
    Create a minimal .docx file in memory.
    A .docx is actually a ZIP with XML inside.
    """
    import zipfile
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # [Content_Types].xml
        zf.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""")
        # _rels/.rels
        zf.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""")
        # word/_rels/document.xml.rels
        zf.writestr("word/_rels/document.xml.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
</Relationships>""")
        # word/document.xml
        zf.writestr("word/document.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r>
        <w:t>This is a test document for Purview sensitivity label testing. It contains sample confidential financial data for demo purposes.</w:t>
      </w:r>
    </w:p>
    <w:p>
      <w:r>
        <w:t>Project Falcon budget: $1,234,567. Internal use only.</w:t>
      </w:r>
    </w:p>
  </w:body>
</w:document>""")

    return buf.getvalue()


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
        # Get site & drive
        resp = await http.get(
            f"https://graph.microsoft.com/v1.0/sites/{hostname}:{site_path}",
            headers=headers,
        )
        site_id = resp.json()["id"]

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

        p("OK", f"drive_id={drive_id[:30]}...")

        # === Upload test .docx ===
        print("\n=== Uploading test document ===")
        docx_content = create_minimal_docx()
        file_name = "test-purview-label.docx"

        upload_url = (
            f"https://graph.microsoft.com/v1.0/drives/{drive_id}"
            f"/root:/{file_name}:/content"
        )
        resp = await http.put(
            upload_url,
            headers={
                **headers,
                "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            },
            content=docx_content,
        )

        if resp.status_code in (200, 201):
            item = resp.json()
            file_id = item["id"]
            web_url = item.get("webUrl", "")
            p("OK", f"Uploaded '{file_name}' (id={file_id[:16]}...)")
            p("URL", f"{web_url}")
            print()
            print("  ┌─────────────────────────────────────────────────────┐")
            print("  │  NOW DO THIS:                                      │")
            print("  │                                                    │")
            print("  │  1. Open the link above in your browser            │")
            print("  │  2. The file will open in Word Online              │")
            print("  │  3. Look for the 'Sensitivity' button in the      │")
            print("  │     toolbar ribbon (Home tab)                      │")
            print("  │  4. Click it and select 'Highly Confidential'     │")
            print("  │  5. Save and close                                │")
            print("  │  6. Come back here and press Enter to continue    │")
            print("  └─────────────────────────────────────────────────────┘")
            print()
            input("  Press Enter after applying the label...")
        else:
            p("FAIL", f"Upload failed: {resp.status_code} - {resp.text[:300]}")
            await cred.close()
            return

        # === Verify label after user applies it ===
        print("\n=== Checking label on file ===")

        # Refresh token
        token = await cred.get_token("https://graph.microsoft.com/.default")
        headers = {"Authorization": f"Bearer {token.token}"}

        # Check with both v1.0 and beta
        for api_ver in ["v1.0", "beta"]:
            resp = await http.get(
                f"https://graph.microsoft.com/{api_ver}/drives/{drive_id}"
                f"/items/{file_id}?$select=id,name,sensitivityLabel",
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                label = data.get("sensitivityLabel")
                if label:
                    label_id = label.get("labelId", "?")
                    display = label.get("displayName", "?")
                    method = label.get("assignmentMethod", "?")
                    p(api_ver, f"LABELED → '{display}' (id={label_id}, method={method})")
                else:
                    p(api_ver, "NO LABEL found on file")
            else:
                p(api_ver, f"Error: {resp.status_code}")

        # === Full Purview detection ===
        print("\n=== Full Purview detection ===")
        from sharepoint_client import SharePointClient
        from purview_client import PurviewClient, ProtectionStatus, merge_permissions_for_search
        from permissions_sync import PermissionsClient

        async with SharePointClient(site_url, drive_name) as sp_client:
            _, resolved_drive_id = sp_client.get_resolved_ids()

            files = []
            async for f in sp_client.list_files("/"):
                files.append(f)

            async with PurviewClient(resolved_drive_id) as purview:
                p("OK", f"Label cache: {len(purview._label_cache)} label(s)")
                for lid, linfo in purview._label_cache.items():
                    enc = "ENCRYPTED" if linfo.is_encrypted else "no-enc"
                    p("CACHE", f"'{linfo.label_name}' → {enc} (id={lid[:16]}...)")

                async with PermissionsClient(resolved_drive_id) as perm_client:
                    for f in files:
                        print(f"\n  ━━━ {f.path} ━━━")

                        # Get protection info
                        protection = await purview.get_file_protection(f.id, f.path)
                        p("STATUS", protection.status.value)

                        if protection.sensitivity_label:
                            lbl = protection.sensitivity_label
                            p("LABEL", f"'{lbl.label_name}' encrypted={lbl.is_encrypted} method={lbl.assignment_method}")

                        # Get SP permissions
                        sp_perms = await perm_client.get_file_permissions(f.id, f.path)
                        sp_uids = sp_perms._extract_user_ids()
                        sp_gids = sp_perms._extract_group_ids()
                        p("SP_PERMS", f"users={sp_uids}, groups={sp_gids}")

                        if protection.status == ProtectionStatus.PROTECTED:
                            p("RMS", f"{len(protection.rms_permissions)} entries")
                            for rp in protection.rms_permissions:
                                p("RMS_ENTRY", f"[{rp.identity_type}] {rp.display_name} rights={rp.usage_rights}")

                            eff_u, eff_g = merge_permissions_for_search(sp_uids, sp_gids, protection)
                            p("EFFECTIVE", f"users={eff_u}")
                            p("EFFECTIVE", f"groups={eff_g}")

                        elif protection.status == ProtectionStatus.LABEL_ONLY:
                            p("INFO", "Label without encryption → SP permissions apply as-is")

                        # Metadata that would go to blob
                        meta = {}
                        if protection.sensitivity_label:
                            meta.update(protection.to_metadata())
                        perm_meta = sp_perms.to_metadata(protection_info=protection)
                        meta.update(perm_meta)
                        p("BLOB_META", "What would be written to blob:")
                        for k, v in meta.items():
                            val = v[:80] + "..." if len(v) > 80 else v
                            print(f"      {k} = {val}")

    await cred.close()
    print("\n=== ALL DONE ===")


if __name__ == "__main__":
    asyncio.run(main())
