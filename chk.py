import os
import dotenv

print("--- .env file check utility ---")

# .envファイルの絶対パスを指定してみる（もしカレントディレクトリでダメなら）
# env_path = os.path.join(os.path.dirname(__file__), '.env')
# print(f"Searching for .env at: {env_path}")
# loaded = dotenv.load_dotenv(dotenv_path=env_path)

# まずはシンプルにロードを試す
loaded = dotenv.load_dotenv()

if not loaded:
    print("\n!!! CRITICAL: .env file was NOT found or could NOT be loaded.")
    print("    Please ensure a file named '.env' exists in the same directory as this script.")
else:
    print("\n✓ .env file was found and loaded successfully.")

print("\n--- Checking for required variables ---")

redmine_url = os.environ.get("REDMINE_URL")
redmine_key = os.environ.get("REDMINE_API_KEY")

print(f"  Value of REDMINE_URL: {redmine_url}")
print(f"  Value of REDMINE_API_KEY: {redmine_key}")

print("\n--- Analysis ---")
if redmine_url and redmine_key:
    print("✓ SUCCESS: Both REDMINE_URL and REDMINE_API_KEY are present in the environment.")
else:
    print("✗ FAILURE: One or both variables are MISSING.")
    print("  This is the reason the main application is failing.")
    print("  Please double-check the variable names and content in your .env file.")

print("\n--- Test finished ---")