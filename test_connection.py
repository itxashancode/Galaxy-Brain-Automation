# test_connection.py
import os
from dotenv import load_dotenv
import requests

load_dotenv()

print("Testing GitHub connection...")
token = os.getenv('GITHUB_TOKEN')
username = os.getenv('GITHUB_USERNAME')

headers = {"Authorization": f"Bearer {token}"}
r = requests.get("https://api.github.com/user", headers=headers)
if r.status_code == 200:
    print(f"✅ GitHub connected as: {r.json()['login']}")
else:
    print(f"❌ GitHub error: {r.status_code}")

print("\nTesting OpenRouter connection...")
keys = os.getenv('OPENROUTER_KEYS', '').split(',')
for key in keys[:1]:  # Test first key only
    key = key.strip()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": "qwen/qwen3.6-plus:free",
        "messages": [{"role": "user", "content": "Say 'OK'"}],
        "max_tokens": 10
    }
    r = requests.post("https://openrouter.ai/api/v1/chat/completions", 
                      headers=headers, json=payload)
    if r.status_code == 200:
        print(f"✅ OpenRouter key working")
        break
    else:
        print(f"❌ OpenRouter error: {r.status_code}")