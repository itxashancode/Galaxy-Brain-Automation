import os
import sys
import json
from datetime import datetime
import requests

def test_imports():
    """Test all required imports"""
    print("🔍 Testing imports...")
    try:
        import requests
        print("  ✓ requests")
        from rich.console import Console
        print("  ✓ rich")
        from dotenv import load_dotenv
        print("  ✓ python-dotenv")
        print("✅ All imports successful\n")
        return True
    except ImportError as e:
        print(f"❌ Import failed: {e}")
        return False

def test_env_file():
    """Test .env file exists and has required keys"""
    print("🔍 Testing .env file...")
    
    if not os.path.exists('.env'):
        print("  ⚠️ .env file not found, creating template...")
        create_env_template()
        print("  📝 Created .env.template - please rename to .env and add your keys")
        return False
    
    from dotenv import load_dotenv
    load_dotenv()
    
    required_keys = ['GITHUB_TOKEN', 'GITHUB_USERNAME', 'OPENROUTER_KEYS']
    missing = []
    
    for key in required_keys:
        if not os.getenv(key):
            missing.append(key)
        else:
            value = os.getenv(key)
            masked = value[:10] + "..." if len(value) > 10 else "***"
            print(f"  ✓ {key}: {masked}")
    
    if missing:
        print(f"  ❌ Missing required keys: {', '.join(missing)}")
        return False
    
    print("✅ .env file configured\n")
    return True

def create_env_template():
    """Create .env template file"""
    template = """# GitHub Configuration
GITHUB_TOKEN=your_github_token_here
GITHUB_USERNAME=your_username_here

# OpenRouter API Keys (comma-separated for rotation)
OPENROUTER_KEYS=your_openrouter_key_here

# Webhook URLs (optional)
DISCORD_WEBHOOK_URL=
SLACK_WEBHOOK_URL=

# Bot Configuration
MIN_REPO_STARS=10
MAX_ANSWERS_PER_SESSION=5
DELAY_BETWEEN_ANSWERS=5
AUTO_APPROVE_ANSWERS=false

# Search Filters
SEARCH_TOPICS=python
"""
    with open('.env.template', 'w') as f:
        f.write(template)
    print("  📝 Created .env.template file")

def test_github_api():
    """Test GitHub API connection"""
    print("🔍 Testing GitHub API...")
    
    from dotenv import load_dotenv
    load_dotenv()
    
    token = os.getenv('GITHUB_TOKEN')
    if not token:
        print("  ❌ No GitHub token found")
        return False
    
    try:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        response = requests.get(
            "https://api.github.com/user",
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            user_data = response.json()
            print(f"  ✓ Authenticated as: {user_data.get('login', 'Unknown')}")
            print(f"  ✓ Rate limit: {response.headers.get('X-RateLimit-Remaining', 'Unknown')} remaining")
            print("✅ GitHub API working\n")
            return True
        else:
            print(f"  ❌ GitHub API error: {response.status_code}")
            return False
    except Exception as e:
        print(f"  ❌ GitHub API error: {e}")
        return False

def test_openrouter_api():
    """Test OpenRouter API connection"""
    print("🔍 Testing OpenRouter API...")

    from dotenv import load_dotenv
    load_dotenv()

    keys_str = os.getenv('OPENROUTER_KEYS', '')
    keys = [k.strip() for k in keys_str.split(',') if k.strip()]

    if not keys:
        print("  ❌ No OpenRouter keys found")
        return False

    # Verified working free models on OpenRouter (in priority order)
    FREE_MODELS = [
        "qwen/qwen3.6-plus:free",
        "stepfun/step-3.5-flash:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "arcee-ai/trinity-large-preview:free",
    ]

    test_prompt = "Reply with exactly: API is working"

    for i, key in enumerate(keys[:2], 1):  # Test first 2 keys
        print(f"  Testing key {i}...")
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/galaxy-brain-bot",
            "X-Title": "Galaxy Brain Bot"
        }

        for model in FREE_MODELS:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": test_prompt}],
                "max_tokens": 50
            }

            try:
                response = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=30
                )

                if response.status_code == 200:
                    result = response.json()
                    choices = result.get("choices")
                    if not choices:
                        print(f"    ⚠️ Model {model}: 200 OK but empty choices — skipping")
                        continue
                    answer = choices[0].get("message", {}).get("content", "").strip()
                    if not answer:
                        print(f"    ⚠️ Model {model}: empty response content — skipping")
                        continue
                    print(f"    ✓ Key {i} working via [{model}]: {answer[:60]}")
                    print("✅ OpenRouter API working\n")
                    return True
                elif response.status_code == 429:
                    print(f"    ⚠️ Model {model}: rate limited, trying next...")
                elif response.status_code == 404:
                    print(f"    ⚠️ Model {model}: not found (404), trying next...")
                else:
                    body = response.json() if response.content else {}
                    err_msg = body.get("error", {}).get("message", response.text[:80])
                    print(f"    ⚠️ Model {model}: HTTP {response.status_code} — {err_msg}")

            except Exception as e:
                print(f"    ⚠️ Model {model}: exception — {e}")

        print(f"    ❌ Key {i}: all models failed")

    print("❌ No working OpenRouter keys found")
    return False

def test_graphql_query():
    """Test GitHub GraphQL query for discussions"""
    print("🔍 Testing GraphQL query...")
    
    from dotenv import load_dotenv
    load_dotenv()
    
    token = os.getenv('GITHUB_TOKEN')
    username = os.getenv('GITHUB_USERNAME')
    
    if not token or not username:
        print("  ❌ Missing credentials")
        return False
    
    # NOTE: hasDiscussionsEnabled is NOT a valid filter argument on the
    # repositories() field. Fetch repos without that filter, then check the
    # hasDiscussionsEnabled field on each node in Python instead.
    query = """
    query {
        viewer {
            login
            repositories(first: 20, ownerAffiliations: OWNER, orderBy: {field: UPDATED_AT, direction: DESC}) {
                nodes {
                    name
                    hasDiscussionsEnabled
                }
            }
        }
    }
    """

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(
            "https://api.github.com/graphql",
            headers=headers,
            json={"query": query},
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            if 'errors' in data:
                print(f"  ❌ GraphQL errors: {data['errors']}")
                return False

            viewer = data.get('data', {}).get('viewer', {})
            all_repos = viewer.get('repositories', {}).get('nodes', [])
            discussion_repos = [r for r in all_repos if r.get('hasDiscussionsEnabled')]

            print(f"  ✓ Authenticated as: {viewer.get('login')}")
            print(f"  ✓ Your repos checked: {len(all_repos)}")
            print(f"  ✓ Repos with discussions enabled: {len(discussion_repos)}")
            if len(discussion_repos) == 0:
                print("  ℹ️  None of your own repos have discussions — that's fine, the bot searches public repos.")
            print("✅ GraphQL query working\n")
            return True
        else:
            print(f"  ❌ GraphQL HTTP error: {response.status_code}")
            return False
    except Exception as e:
        print(f"  ❌ GraphQL error: {e}")
        return False

def test_stats_file():
    """Test stats file creation and loading"""
    print("🔍 Testing stats file...")
    
    test_stats = {
        'total_answers': 0,
        'accepted_answers': 0,
        'answers': [],
        'answered_discussion_ids': [],
        'start_date': datetime.now().isoformat(),
        'last_update': datetime.now().isoformat()
    }
    
    test_file = "test_stats.json"
    
    try:
        # Write test file
        with open(test_file, 'w') as f:
            json.dump(test_stats, f, indent=2)
        
        # Read test file
        with open(test_file, 'r') as f:
            loaded = json.load(f)
        
        # Clean up
        os.remove(test_file)
        
        print("  ✓ Stats file creation and loading working")
        print("✅ Stats system working\n")
        return True
    except Exception as e:
        print(f"  ❌ Stats error: {e}")
        return False

def run_all_tests():
    """Run all tests"""
    print("\n" + "="*50)
    print("🧪 Galaxy Brain Bot - Test Suite")
    print("="*50 + "\n")
    
    tests = [
        ("Import Test", test_imports),
        ("Environment Test", test_env_file),
        ("GitHub API Test", test_github_api),
        ("OpenRouter API Test", test_openrouter_api),
        ("GraphQL Test", test_graphql_query),
        ("Stats Test", test_stats_file)
    ]
    
    results = []
    for name, test_func in tests:
        result = test_func()
        results.append((name, result))
        print("-"*50 + "\n")
    
    # Summary
    print("\n" + "="*50)
    print("📊 Test Summary")
    print("="*50)
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")
    
    print(f"\n{passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed! You can run the bot.")
        return True
    else:
        print("\n⚠️ Some tests failed. Please fix the issues before running the bot.")
        print("\nCommon fixes:")
        print("  1. Make sure .env file exists with valid credentials")
        print("  2. Check GitHub token has 'discussions' access")
        print("  3. Verify OpenRouter keys are valid")
        return False

def quick_test_run():
    """Quick test run without posting answers"""
    print("\n" + "="*50)
    print("🚀 Quick Test Run (No Posts)")
    print("="*50 + "\n")
    
    from dotenv import load_dotenv
    load_dotenv()
    
    token = os.getenv('GITHUB_TOKEN')
    username = os.getenv('GITHUB_USERNAME')
    
    if not token or not username:
        print("❌ Missing credentials in .env file")
        return
    
    print("🔍 Searching for a sample repository...")
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # Search for a public repo with discussions
    query = """
    query {
        search(query: "hasDiscussionsEnabled:true is:public", type: REPOSITORY, first: 3) {
            nodes {
                ... on Repository {
                    nameWithOwner
                    hasDiscussionsEnabled
                    stargazerCount
                }
            }
        }
    }
    """
    
    try:
        response = requests.post(
            "https://api.github.com/graphql",
            headers=headers,
            json={"query": query},
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            repos = data.get('data', {}).get('search', {}).get('nodes', [])
            
            if repos:
                print(f"✓ Found {len(repos)} repositories with discussions:\n")
                for repo in repos:
                    print(f"  • {repo['nameWithOwner']} (⭐ {repo['stargazerCount']} stars)")
                
                print("\n✅ Quick test successful! The bot can find repositories.")
                print("\n💡 To run the full bot:")
                print("  python galaxy_brain_bot.py")
            else:
                print("⚠️ No repositories found with discussions")
        else:
            print(f"❌ API error: {response.status_code}")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--quick":
        quick_test_run()
    else:
        success = run_all_tests()
        if success:
            print("\n💡 Next steps:")
            print("  1. Make sure .env file is properly configured")
            print("  2. Run the bot: python galaxy_brain_bot.py")
            print("  3. Check stats: python galaxy_brain_bot.py --stats")
            print("  4. Check for acceptances: python galaxy_brain_bot.py --check")