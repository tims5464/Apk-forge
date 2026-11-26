import os, json, requests

api_key = os.environ.get("GEMINI_API_KEY", "")
with open("build_log.txt") as f:
    log = f.read()[-3000:]

url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={api_key}"
prompt = f"The Android Gradle build failed. Return a JSON object with 'target_file' and 'fixed_content' with no markdown fences.\nLog:\n{log}"

payload = {
    "contents": [{"parts": [{"text": prompt}]}],
    "generationConfig": {"responseMimeType": "application/json"}
}
res = requests.post(url, json=payload).json()
if "candidates" in res:
    data = json.loads(res["candidates"][0]["content"]["parts"][0]["text"])
    os.makedirs(os.path.dirname(data["target_file"]), exist_ok=True)
    with open(data["target_file"], "w") as out:
        out.write(data["fixed_content"])
    print(f"[+] Self-healed {data['target_file']}")
