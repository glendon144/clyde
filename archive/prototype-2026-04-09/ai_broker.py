import json
from pathlib import Path
from json_tool_executor import (
    extract_first_json_object,
    normalize_tool_payload,
    ensure_safe_cwd,
    ExecResult,
    execute_tool,
    asdict
)

# --- POLICY ---

def load_policy(path="policy.json"):
    return json.loads(Path(path).read_text())

def extract_command_name(command):
    return command.strip().split()[0]

def is_path_allowed(path, allowed_paths):
    from pathlib import Path
    path = Path(path).resolve()
    for allowed in allowed_paths:
        if path.is_relative_to(Path(allowed).resolve()):
            return True
    return False

def check_policy(role, command, cwd, policy):
    role_cfg = policy["roles"].get(role)
    if not role_cfg:
        return False, "Unknown role"

    cmd = extract_command_name(command)

    if "*" not in role_cfg["allow_commands"] and cmd not in role_cfg["allow_commands"]:
        return False, f"Command '{cmd}' not allowed"

    if not is_path_allowed(cwd, role_cfg["allow_paths"]):
        return False, f"Path '{cwd}' not allowed"

    if role_cfg.get("read_only", False):
        if cmd in ["rm", "mv", "cp"]:
            return False, f"Write command '{cmd}' not allowed"

    return True, None


# --- MAIN FLOW ---

def broker_execute(raw_text, role="developer", cwd="."):
    policy = load_policy()

    try:
        payload = extract_first_json_object(raw_text)
        tool, command, description = normalize_tool_payload(payload)
        cwd = ensure_safe_cwd(cwd)

        approved, reason = check_policy(role, command, cwd, policy)

        if not approved:
            return {
                "ok": False,
                "error": f"Denied by AI Broker: {reason}"
            }

        result = execute_tool(
            tool=tool,
            command=command,
            description=description,
            cwd=cwd,
            timeout=10,
        )

        return asdict(result)

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--role", default="developer")
    parser.add_argument("--pretty", action="store_true")

    args = parser.parse_args()

    text = Path(args.input_file).read_text()

    result = broker_execute(text, role=args.role, cwd=args.cwd)

    print(json.dumps(result, indent=2 if args.pretty else None))
