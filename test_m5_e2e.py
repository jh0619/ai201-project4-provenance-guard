"""End-to-end M5 verification. Assumes server is running on 127.0.0.1:5000."""
import json
import requests

BASE = "http://127.0.0.1:5000"


def submit_and_show(text, creator, label):
    print(f"\n--- {label} ---")
    r = requests.post(f"{BASE}/submit", json={"text": text, "creator_id": creator})
    b = r.json()
    print(f"  http={r.status_code} attribution={b['attribution']:<13} conf={b['confidence']}")
    print(f"  styl={b['signals']['stylometric']['score']} llm={b['signals']['llm']['score']}")
    print("  label:")
    for ln in b["label"].split("\n"):
        print(f"      {ln}")
    return b["content_id"]


print("=" * 72)
print("STEP 1-3: hit all three label variants")
print("=" * 72)

human_id = submit_and_show(
    "ok so i finally tried that new ramen place downtown and honestly? underwhelming. "
    "the broth was fine but they put WAY too much sodium and i was thirsty for like "
    "three hours after. my friend got the spicy version. probably wont go back unless "
    "someone drags me there",
    "diner-1",
    "1. HUMAN STYLE  (expect likely_human)",
)

ai_id = submit_and_show(
    "In todays competitive landscape, businesses must leverage cutting-edge technology "
    "to achieve sustainable growth. Companies that embrace digital transformation gain "
    "significant advantages over their competitors. By implementing innovative strategies, "
    "organizations can streamline operations and enhance productivity. Furthermore, "
    "adopting data-driven approaches enables companies to make informed decisions and "
    "drive better outcomes. Additionally, businesses must continuously evolve and adapt "
    "to stay ahead in this dynamic environment. Ultimately, success in the modern era "
    "requires a commitment to continuous improvement and strategic innovation.",
    "marketing-bot",
    "2. MARKETING AI  (expect likely_ai)",
)

unc_id = submit_and_show(
    "Furthermore, the relationship between monetary policy and asset price inflation has "
    "been extensively studied. Central banks face a leverage tension between price "
    "stability and the unintended consequences of prolonged low interest rates on equity "
    "valuations. Innovative central bank tools have emerged in response.",
    "econ-grad",
    "3. POLISHED BORDERLINE  (expect uncertain)",
)

print("\n" + "=" * 72)
print("STEP 4: appeal the human verdict")
print("=" * 72)
ar = requests.post(f"{BASE}/appeal", json={
    "content_id": human_id,
    "creator_reasoning": ("I wrote this myself. English is not my first language and "
                          "my style may look more uniform than typical native writing."),
})
print(json.dumps(ar.json(), indent=2))

print("\n" + "=" * 72)
print("STEP 5: GET /log")
print("=" * 72)
log = requests.get(f"{BASE}/log").json()
print(f"count: {log['count']}\n")
for e in log["entries"]:
    et = e.get("entry_type")
    cid = e.get("content_id", "")[:8]
    status = e.get("status", "N/A")
    if et == "decision":
        print(f"  [decision] {cid}.. status={status:<13} attr={e.get('attribution'):<13} "
              f"conf={e.get('confidence')} styl={e.get('stylometric_score')} llm={e.get('llm_score')}")
    elif et == "appeal":
        print(f"  [appeal]   {cid}.. status={status:<13} "
              f"reasoning={e.get('appeal_reasoning', '')[:60]}...")

print("\n" + "=" * 72)
print("STEP 6: verify original decision status updated, verdict preserved")
print("=" * 72)
sub = requests.get(f"{BASE}/submission/{human_id}").json()
print(f"  status now: {sub.get('status')}    (was 'classified')")
print(f"  attribution still: {sub.get('attribution')}    (original preserved)")
print(f"  confidence still: {sub.get('confidence')}    (original preserved)")
