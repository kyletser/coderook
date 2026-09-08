def setup(api):
    api.register_provider(
        "local-proxy",
        {
            "name": "Local Python Proxy",
            "baseUrl": "http://127.0.0.1:8080/v1",
            "apiKey": "local-test-key",
            "api": "openai-completions",
            "headers": {"X-Python-Provider": "provider", "X-Literal-Dollar": "$$value"},
            "models": [
                {
                    "id": "model-a",
                    "name": "Model A",
                    "input": ["text", "image"],
                    "contextWindow": 64000,
                    "headers": {"X-Python-Provider": "model-a"},
                },
                {"id": "model-b", "name": "Model B", "input": ["text"]},
            ],
        },
    )

    async def use_provider(arguments):
        await api.set_model("local-proxy", arguments.strip() or "model-a")
        return "provider selected"

    api.register_command("use-provider", use_provider, description="Select local proxy")
