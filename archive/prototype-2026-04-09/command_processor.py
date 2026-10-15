import json
import os
import subprocess

def load_policy(path="policy.json"):
    with open(path, "r") as f:
        return json.load(f)

def is_path_allowed(path, allowed_paths):
    path = os.path.abspath(path)
    for allowed in allowed_paths:
        allowed = os.path.abspath(allowed)
        if path.startswith(allowed):
            return True
    return False

def extract_command_name(command):
    return command.strip().split()[0]

def check_policy(user_role, command, cwd, policy):
    role = policy["roles"].get(user_role)
    if not role:
        return False, "Unknown role"

    cmd_name = extract_command_name(command)

    allowed_cmds = role["allow_commands"]
    if "*" not in allowed_cmds and cmd_name not in allowed_cmds:
        return False, f"Command '{cmd_name}' is not allowed"

    if not is_path_allowed(cwd, role["allow_paths"]):
        return False, f"Path '{cwd}' is not allowed"

    if role.get("read_only", False):
        if cmd_name in ["rm", "mv", "cp", "python3", "bash"]:
            return False, f"Command '{cmd_name}' is not allowed in read-only mode"

    return True, None

def execute_command(command, cwd):
    result = subprocess.run(command, shell=True, cwd=cwd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True)
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr
    }

def process_request(json_input, user_role="developer", cwd="."):
    policy = load_policy()

    cmd = json_input.get("arguments", {}).get("command")

    approved, reason = check_policy(user_role, cmd, cwd, policy)

    if not approved:
        return {
            "ok": False,
            "error": reason
        }

    result = execute_command(cmd, cwd)

    return {
        "ok": True,
        "command": cmd,
        "cwd": cwd,
        **result
    }
