"""Quick test to find which Graph API endpoint works for sensitivity labels."""
import asyncio, httpx, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sync"))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "sync", ".env"))
from azure.identity.aio import ClientSecretCredential


async def main():
    cred = ClientSecretCredential(
        os.environ["AZURE_TENANT_ID"],
        os.environ["AZURE_CLIENT_ID"],
        os.environ["AZURE_CLIENT_SECRET"],
    )
    token = await cred.get_token("https://graph.microsoft.com/.default")
    headers = {"Authorization": f"Bearer {token.token}"}

    endpoints = [
        ("v1.0 /security/informationProtection/sensitivityLabels",
         "https://graph.microsoft.com/v1.0/security/informationProtection/sensitivityLabels"),
        ("beta /security/informationProtection/sensitivityLabels",
         "https://graph.microsoft.com/beta/security/informationProtection/sensitivityLabels"),
        ("v1.0 /informationProtection/policy/labels",
         "https://graph.microsoft.com/v1.0/informationProtection/policy/labels"),
        ("beta /informationProtection/policy/labels",
         "https://graph.microsoft.com/beta/informationProtection/policy/labels"),
    ]

    async with httpx.AsyncClient(timeout=30) as http:
        for name, url in endpoints:
            print(f"\n--- {name} ---")
            resp = await http.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                labels = data.get("value", [])
                print(f"  STATUS: {resp.status_code} OK - {len(labels)} label(s)")
                for label in labels:
                    lid = label.get("id", "?")
                    lname = label.get("name", label.get("displayName", "?"))
                    tooltip = label.get("tooltip", "")
                    is_active = label.get("isActive", "?")
                    print(f"    [{lid[:16]}...] {lname} (active={is_active}) {tooltip[:60]}")
            else:
                print(f"  STATUS: {resp.status_code}")
                print(f"  BODY: {resp.text[:200]}")

    # Also check the driveItem sensitivityLabel on a file
    site_url = os.environ.get("SHAREPOINT_SITE_URL", "")
    from urllib.parse import urlparse
    parsed = urlparse(site_url)
    hostname = parsed.netloc
    site_path = parsed.path

    async with httpx.AsyncClient(timeout=30) as http:
        # Get site ID
        resp = await http.get(
            f"https://graph.microsoft.com/v1.0/sites/{hostname}:{site_path}",
            headers=headers,
        )
        site_data = resp.json()
        site_id = site_data.get("id", "")

        # Get drive
        resp = await http.get(
            f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives",
            headers=headers,
        )
        drives = resp.json().get("value", [])
        drive_id = drives[0]["id"] if drives else None

        if drive_id:
            # List files and check each for sensitivity labels
            resp = await http.get(
                f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/children",
                headers=headers,
            )
            items = resp.json().get("value", [])

            print(f"\n--- Checking sensitivityLabel on each file ---")
            for item in items:
                item_id = item["id"]
                item_name = item.get("name", "?")
                is_file = "file" in item
                is_folder = "folder" in item

                if is_folder:
                    # List files inside folder
                    resp2 = await http.get(
                        f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/children",
                        headers=headers,
                    )
                    sub_items = resp2.json().get("value", [])
                    for si in sub_items:
                        if "file" in si:
                            await _check_file_label(http, headers, drive_id, si["id"], f"{item_name}/{si['name']}")
                elif is_file:
                    await _check_file_label(http, headers, drive_id, item_id, item_name)

    await cred.close()


async def _check_file_label(http, headers, drive_id, file_id, file_name):
    """Check sensitivityLabel property on a specific file."""
    # v1.0 with sensitivityLabel
    resp = await http.get(
        f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{file_id}?$select=id,name,sensitivityLabel",
        headers=headers,
    )
    if resp.status_code == 200:
        data = resp.json()
        label = data.get("sensitivityLabel")
        if label:
            label_id = label.get("labelId", "?")
            display = label.get("displayName", "?")
            method = label.get("assignmentMethod", "?")
            print(f"  {file_name}: LABELED -> '{display}' (id={label_id[:16]}..., method={method})")
        else:
            print(f"  {file_name}: NO LABEL")
    else:
        print(f"  {file_name}: ERROR {resp.status_code}")

    # Also try beta endpoint for more details
    resp2 = await http.get(
        f"https://graph.microsoft.com/beta/drives/{drive_id}/items/{file_id}?$select=id,name,sensitivityLabel",
        headers=headers,
    )
    if resp2.status_code == 200:
        data2 = resp2.json()
        label2 = data2.get("sensitivityLabel")
        if label2 and label2 != label:
            print(f"    (beta gives different data: {label2})")


if __name__ == "__main__":
    asyncio.run(main())
