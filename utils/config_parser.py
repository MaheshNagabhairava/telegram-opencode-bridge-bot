import os
import json
import re
import logging

logger = logging.getLogger(__name__)

def find_config_file(work_dir: str) -> str:
    """Locate the opencode config file in workspace or global config.
    Returns the absolute path to the file.
    If none exist, returns the default path to write to: <work_dir>/.opencode/opencode.json
    """
    paths = [
        os.path.join(work_dir, ".opencode", "opencode.jsonc"),
        os.path.join(work_dir, ".opencode", "opencode.json"),
        os.path.expanduser("~/.config/opencode/opencode.jsonc"),
        os.path.expanduser("~/.config/opencode/opencode.json")
    ]
    for p in paths:
        if os.path.exists(p) and os.path.isfile(p):
            return os.path.abspath(p)
    
    # Default to project-local json
    default_p = os.path.join(work_dir, ".opencode", "opencode.json")
    return os.path.abspath(default_p)

def parse_jsonc(content: str) -> dict:
    """Strips comments and trailing commas from JSONC content and parses it."""
    # Pattern to match strings or comments. Comments are replaced with empty string.
    pattern = r'("(?:\\.|[^"\\])*")|//[^\r\n]*|/\*.*?\*/'
    content_clean = re.sub(pattern, lambda m: m.group(1) if m.group(1) else '', content, flags=re.DOTALL)
    
    # Strip trailing commas before closing braces/brackets
    content_clean = re.sub(r',(\s*[\]}])', r'\1', content_clean)
    
    if not content_clean.strip():
        return {}
    
    try:
        return json.loads(content_clean)
    except Exception as e:
        logger.error(f"Failed to parse cleaned JSONC content: {e}")
        raise

def read_config(file_path: str) -> dict:
    """Read and parse the config file. If not found or empty, returns an empty dict."""
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return parse_jsonc(content)
    except Exception as e:
        logger.error(f"Error reading config {file_path}: {e}")
        return {}

def save_config(file_path: str, config: dict) -> None:
    """Saves the config dictionary as formatted JSON. Creates parent folders if needed."""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save config to {file_path}: {e}")
        raise

def get_mcp_servers(work_dir: str) -> dict:
    """Get all MCP server configurations from the config file."""
    config_file = find_config_file(work_dir)
    config = read_config(config_file)
    return config.get("mcp", {})

def update_mcp_servers(work_dir: str, mcp_servers: dict) -> None:
    """Update the entire mcp section in the config file."""
    config_file = find_config_file(work_dir)
    config = read_config(config_file)
    config["mcp"] = mcp_servers
    save_config(config_file, config)

def toggle_mcp_server(work_dir: str, name: str, enabled: bool) -> bool:
    """Toggle the enabled status of an MCP server."""
    config_file = find_config_file(work_dir)
    config = read_config(config_file)
    if "mcp" not in config:
        config["mcp"] = {}
    
    if name in config["mcp"]:
        config["mcp"][name]["enabled"] = enabled
        save_config(config_file, config)
        return True
    return False

def add_mcp_server(work_dir: str, name: str, mcp_config: dict) -> None:
    """Add or overwrite an MCP server configuration."""
    config_file = find_config_file(work_dir)
    config = read_config(config_file)
    if "mcp" not in config:
        config["mcp"] = {}
    
    config["mcp"][name] = mcp_config
    save_config(config_file, config)

def delete_mcp_server(work_dir: str, name: str) -> bool:
    """Delete an MCP server from the configuration."""
    config_file = find_config_file(work_dir)
    config = read_config(config_file)
    if "mcp" in config and name in config["mcp"]:
        del config["mcp"][name]
        save_config(config_file, config)
        return True
    return False

def get_skill_permissions(work_dir: str) -> dict:
    """Get all skill permissions from configuration."""
    config_file = find_config_file(work_dir)
    config = read_config(config_file)
    return config.get("permission", {}).get("skill", {})

def set_skill_permission(work_dir: str, name: str, level: str) -> None:
    """Set permission level ('allow', 'deny', 'ask') for a skill."""
    config_file = find_config_file(work_dir)
    config = read_config(config_file)
    if "permission" not in config:
        config["permission"] = {}
    if "skill" not in config["permission"]:
        config["permission"]["skill"] = {}
        
    config["permission"]["skill"][name] = level
    save_config(config_file, config)

def delete_skill_permission(work_dir: str, name: str) -> bool:
    """Remove a skill permission configuration."""
    config_file = find_config_file(work_dir)
    config = read_config(config_file)
    if "permission" in config and "skill" in config["permission"]:
        if name in config["permission"]["skill"]:
            del config["permission"]["skill"][name]
            save_config(config_file, config)
            return True
    return False
