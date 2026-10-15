import json
from command_processor import process_request

example = {
    "name": "bash",
    "arguments": {
        "command": "ls"
    }
}

result = process_request(example, user_role="developer", cwd=".")
print(json.dumps(result, indent=2))
