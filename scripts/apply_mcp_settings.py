import json
import shutil

SETTINGS_PATH = "/root/.claude/settings.json"

with open(SETTINGS_PATH, "r") as f:
    settings = json.load(f)

shutil.copy2(SETTINGS_PATH, SETTINGS_PATH + ".bak")

settings["mcpServers"] = {
    "aads-tools": {
        "command": "docker",
        "args": [
            "exec", "-i",
            "-e", "AADS_SESSION_ID=",
            "aads-server",
            "python3", "-m", "mcp_servers.aads_tools_bridge"
        ]
    }
}

with open(SETTINGS_PATH, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print("OK: mcpServers added to", SETTINGS_PATH)
