import json
import os
import tempfile

from ai_broker import AIBroker
from command_processor import process_request


broker = AIBroker()

print("=== 1. Backward-compatible shell request ===")
legacy = {
    "name": "bash",
    "arguments": {
        "command": "ls"
    }
}
print(json.dumps(process_request(legacy, user_role="developer", cwd="."), indent=2))

print("\n=== 2. LLM summarize request (first call) ===")
llm_task = {
    "type": "llm",
    "intent": "summarize",
    "payload": {
        "text": "AI Broker sits between the app and the models. It routes, caches, and protects the budget."
    }
}
print(json.dumps(broker.process_task(llm_task, user_role="developer"), indent=2))

print("\n=== 3. Same LLM request again (cache hit) ===")
print(json.dumps(broker.process_task(llm_task, user_role="developer"), indent=2))

print("\n=== 4. OCR request ===")
ocr_task = {
    "type": "ocr",
    "intent": "ocr_extract",
    "payload": {
        "image_path": "/tmp/frame_0001.jpg"
    }
}
print(json.dumps(broker.process_task(ocr_task, user_role="developer"), indent=2))

print("\n=== 5. Warm cache / scheduled prefetch ===")
print(json.dumps(broker.warm_cache(), indent=2))

print("\n=== 6. Provider status snapshot ===")
print(json.dumps(broker.provider_status, indent=2))

print("\n=== 7. Budget ledger snapshot ===")
print(json.dumps(broker.budget_ledger, indent=2))

print("\n=== 8. Explicit Dream milestone capture ===")
dream_task = {
    "type": "dream",
    "intent": "store_milestone",
    "payload": {
        "summary": "AI Broker example: explicit Dream milestone capture remains opt-in and broker-managed.",
        "key_facts": [
            "Dream writes are explicit tasks, not automatic inference side effects",
            "The broker routes milestone capture through the Dream REST store_capsule path",
            "Maintainer approval and policy still govern the write"
        ],
        "themes": ["AI Broker", "Dream Server", "milestone capture"],
        "entities": ["AI Broker", "Dream Server"],
        "source_references": ["run_example.py"],
        "metadata": {"example": True, "component": "dream_rest"},
    },
}
with tempfile.TemporaryDirectory() as td:
    dream_broker = AIBroker(
        cache_path=os.path.join(td, "cache.json"),
        budget_ledger_path=os.path.join(td, "budget.json"),
        provider_status_path=os.path.join(td, "provider_status.json"),
        audit_log_path=os.path.join(td, "audit.jsonl"),
    )
    print(json.dumps(dream_broker.process_task(dream_task, user_role="maintainer"), indent=2))
