import sys
import os

# Add src to path
sys.path.append(os.path.join(os.getcwd(), "src"))

from auto_coder.aider_client import AiderClient

def test_aider_client():
    print("Testing AiderClient...")
    try:
        # Force model to openrouter/nvidia/nemotron-3-nano-30b-a3b:free for testing
        # We need to mock the config or pass it somehow if AiderClient doesn't accept model in init directly
        # AiderClient init takes backend_name.
        # Let's try to set the model attribute after init or use a custom backend name if config allows.
        # But AiderClient reads from config.
        # Let's just set the environment variable AIDER_MODEL
        # os.environ["AIDER_MODEL"] = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
        client = AiderClient()
        client.model_name = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
        print(f"AiderClient initialized. Model: {client.model_name}")

        # Simple prompt test
        prompt = "Hello, are you working?"
        print(f"Running prompt: {prompt}")
        response = client._run_llm_cli(prompt)
        print(f"Response: {response}")

        if response:
            print("✅ AiderClient test passed!")
        else:
            print("❌ AiderClient returned empty response.")

    except Exception as e:
        print(f"❌ AiderClient test failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_aider_client()
