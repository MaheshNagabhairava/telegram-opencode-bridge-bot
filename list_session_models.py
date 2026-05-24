import asyncio
import aiohttp
import json

async def main():
    url = "http://localhost:4444"
    async with aiohttp.ClientSession() as session:
        # Create a session
        async with session.post(f"{url}/session") as resp:
            create_json = await resp.json()
            session_id = create_json.get("id") or create_json.get("session_id")
            
        print(f"Created session ID: {session_id}")
        
        # Query GET /session/:id/models
        models_url = f"{url}/session/{session_id}/models"
        print(f"Querying {models_url}...")
        async with session.get(models_url) as resp:
            print(f"Status: {resp.status}")
            data = await resp.json()
            
            # Print complete JSON to see all configured models
            print("\n--- All Configured Models on OpenCode Serve ---")
            print(json.dumps(data, indent=2))
            
            # Search for DeepSeek or Zen
            print("\n--- Searching for 'deepseek' or 'zen' ---")
            found = False
            data_str = json.dumps(data)
            if "deepseek" in data_str.lower() or "zen" in data_str.lower():
                print("MATCH FOUND!")
                # Let's inspect the exact provider and model IDs
                if isinstance(data, dict):
                    # Usually returned as { "models": { ... }, "providers": [ ... ] } or similar
                    for k, v in data.items():
                        if "deepseek" in k.lower() or "zen" in k.lower() or "deepseek" in json.dumps(v).lower() or "zen" in json.dumps(v).lower():
                            print(f"\nKey: {k}")
                            print(json.dumps(v, indent=2)[:1000])
                            found = True
            if not found:
                print("No matches found for DeepSeek or Zen.")

if __name__ == "__main__":
    asyncio.run(main())
