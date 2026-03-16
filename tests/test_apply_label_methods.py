"""
Check if sensitivity labels are published to users (label policies) and
try to apply a label using the v1.0 PATCH method as alternative.
"""
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
        # Get detailed label info (beta)
        print("=== Sensitivity Labels (detailed) ===")
        r = await http.get(
            "https://graph.microsoft.com/beta/security/informationProtection/sensitivityLabels",
            headers=h,
        )
        if r.status_code == 200:
            labels = r.json().get("value", [])
            for lbl in labels:
                print(f"\n  Label: '{lbl.get('name')}'")
                print(f"    id: {lbl.get('id')}")
                print(f"    isActive: {lbl.get('isActive')}")
                print(f"    tooltip: {lbl.get('tooltip')}")
                print(f"    color: {lbl.get('color')}")
                print(f"    contentFormats: {lbl.get('contentFormats')}")
                # Print all other fields
                for k, v in lbl.items():
                    if k not in ('name', 'id', 'isActive', 'tooltip', 'color', 'contentFormats', '@odata.type'):
                        print(f"    {k}: {v}")

        # Check label policies
        print("\n=== Label Policies ===")
        policy_endpoints = [
            ("beta /security/informationProtection/sensitivityLabels/policies",
             "https://graph.microsoft.com/beta/security/informationProtection/sensitivityLabels/policies"),
            ("beta /informationProtection/policy",
             "https://graph.microsoft.com/beta/informationProtection/policy"),
        ]
        for name, url in policy_endpoints:
            r = await http.get(url, headers=h)
            print(f"\n  {name} -> {r.status_code}")
            if r.status_code == 200:
                print(f"  {json.dumps(r.json(), indent=2)[:500]}")
            else:
                print(f"  {r.text[:200]}")

        # Resolve drive
        r = await http.get(f"https://graph.microsoft.com/v1.0/sites/{parsed.netloc}:{parsed.path}", headers=h)
        site_id = r.json()["id"]
        r = await http.get(f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives", headers=h)
        drive_id = r.json()["value"][0]["id"]

        # Get the test-purview-label.docx file
        r = await http.get(
            f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/test-purview-label.docx",
            headers=h,
        )
        if r.status_code != 200:
            print(f"\nCan't find test-purview-label.docx: {r.status_code}")
            await cred.close()
            return

        file_id = r.json()["id"]
        print(f"\n=== Trying to apply label to test-purview-label.docx ===")
        print(f"  file_id: {file_id}")

        # Get the first label
        r = await http.get(
            "https://graph.microsoft.com/beta/security/informationProtection/sensitivityLabels",
            headers=h,
        )
        labels = r.json().get("value", [])
        # Find "Highly Confidential"
        target = None
        for lbl in labels:
            if "highly" in lbl["name"].lower():
                target = lbl
                break
        if not target:
            target = labels[0]

        print(f"  Using label: '{target['name']}' (id={target['id']})")

        # Method 1: PATCH driveItem with sensitivityLabel (v1.0)
        print("\n  Method 1: PATCH /drives/{drive-id}/items/{id} with sensitivityLabel")
        body1 = {"sensitivityLabel": {"labelId": target["id"], "assignmentMethod": "standard"}}
        r = await http.patch(
            f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{file_id}",
            headers={**h, "Content-Type": "application/json"},
            content=json.dumps(body1),
        )
        print(f"    Status: {r.status_code}")
        if r.status_code != 200:
            print(f"    Body: {r.text[:300]}")

        # Method 2: PATCH via beta
        print("\n  Method 2: PATCH via beta")
        r = await http.patch(
            f"https://graph.microsoft.com/beta/drives/{drive_id}/items/{file_id}",
            headers={**h, "Content-Type": "application/json"},
            content=json.dumps(body1),
        )
        print(f"    Status: {r.status_code}")
        if r.status_code != 200:
            print(f"    Body: {r.text[:300]}")

        # Method 3: assignSensitivityLabel (beta)
        print("\n  Method 3: POST assignSensitivityLabel (beta)")
        body3 = {
            "sensitivityLabelId": target["id"],
            "assignmentMethod": "standard",
            "justificationText": "test"
        }
        r = await http.post(
            f"https://graph.microsoft.com/beta/drives/{drive_id}/items/{file_id}/assignSensitivityLabel",
            headers={**h, "Content-Type": "application/json"},
            content=json.dumps(body3),
        )
        print(f"    Status: {r.status_code}")
        if r.status_code in (200, 202, 204):
            print(f"    SUCCESS!")
            if r.text:
                print(f"    Body: {r.text[:300]}")
        else:
            print(f"    Body: {r.text[:300]}")

        # Method 4: Try v1.0 assignSensitivityLabel
        print("\n  Method 4: POST assignSensitivityLabel (v1.0)")
        r = await http.post(
            f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{file_id}/assignSensitivityLabel",
            headers={**h, "Content-Type": "application/json"},
            content=json.dumps(body3),
        )
        print(f"    Status: {r.status_code}")
        if r.status_code in (200, 202, 204):
            print(f"    SUCCESS!")
        else:
            print(f"    Body: {r.text[:300]}")

    await cred.close()


if __name__ == "__main__":
    asyncio.run(main())
