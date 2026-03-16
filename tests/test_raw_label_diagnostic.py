"""Diagnostic: show raw sensitivityLabel data from Graph API for all files."""
import asyncio, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sync"))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "sync", ".env"))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import httpx
from azure.identity.aio import ClientSecretCredential
from urllib.parse import urlparse


async def main():
    cred = ClientSecretCredential(
        os.environ["AZURE_TENANT_ID"],
        os.environ["AZURE_CLIENT_ID"],
        os.environ["AZURE_CLIENT_SECRET"],
    )
    token = await cred.get_token("https://graph.microsoft.com/.default")
    h = {"Authorization": f"Bearer {token.token}"}

    site_url = os.environ["SHAREPOINT_SITE_URL"]
    parsed = urlparse(site_url)

    async with httpx.AsyncClient(timeout=30) as http:
        # Resolve drive
        r = await http.get(f"https://graph.microsoft.com/v1.0/sites/{parsed.netloc}:{parsed.path}", headers=h)
        site_id = r.json()["id"]
        r = await http.get(f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives", headers=h)
        drive_id = r.json()["value"][0]["id"]

        # Collect all file IDs recursively
        files = []
        async def collect(parent_path, parent_url):
            r = await http.get(parent_url, headers=h)
            for item in r.json().get("value", []):
                if item.get("folder"):
                    child_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item['id']}/children"
                    await collect(f"{parent_path}/{item['name']}", child_url)
                elif item.get("file"):
                    files.append((f"{parent_path}/{item['name']}", item["id"]))

        await collect("", f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/children")

        print(f"Found {len(files)} files\n")

        for fname, fid in files:
            print(f"{'='*60}")
            print(f"FILE: {fname} (id={fid})")

            # v1.0 - full driveItem (no $select, to see all properties)
            for api in ["v1.0", "beta"]:
                r = await http.get(
                    f"https://graph.microsoft.com/{api}/drives/{drive_id}/items/{fid}",
                    headers=h,
                )
                data = r.json()
                label = data.get("sensitivityLabel")
                print(f"\n  [{api}] sensitivityLabel = {json.dumps(label, indent=4)}")

                # Also check if there's a 'sensitivity' or 'classification' property
                for key in ["sensitivity", "classification", "protectionSettings"]:
                    if key in data:
                        print(f"  [{api}] {key} = {json.dumps(data[key], indent=4)}")

            # Try extractSensitivityLabels (beta)
            r = await http.post(
                f"https://graph.microsoft.com/beta/drives/{drive_id}/items/{fid}/extractSensitivityLabels",
                headers={**h, "Content-Type": "application/json"},
                content="{}",
            )
            print(f"\n  [beta] extractSensitivityLabels -> {r.status_code}")
            if r.status_code == 200:
                print(f"  Response: {json.dumps(r.json(), indent=4)}")
            else:
                print(f"  Response: {r.text[:300]}")

            print()

    await cred.close()


if __name__ == "__main__":
    asyncio.run(main())
