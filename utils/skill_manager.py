import os
import re
import html
import shutil
import logging
from utils.config_parser import get_skill_permissions

logger = logging.getLogger(__name__)

def parse_frontmatter(content: str) -> dict:
    """Safely extracts and parses yaml frontmatter headers from SKILL.md without dependencies."""
    frontmatter = {}
    if content.strip().startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            yaml_text = parts[1]
            for line in yaml_text.strip().split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    frontmatter[k.strip().lower()] = v.strip().strip("'\"")
            # Store raw markdown content without frontmatter
            frontmatter["_content"] = parts[2].strip()
    else:
        frontmatter["_content"] = content.strip()
    return frontmatter

def get_skills_dir(work_dir: str, scope: str) -> str:
    """Get the target skills directory path based on scope ('global' or 'local')."""
    if scope == "global":
        return os.path.abspath(os.path.expanduser("~/.config/opencode/skills"))
    else:
        return os.path.abspath(os.path.join(work_dir, ".opencode", "skills"))

def get_skills(work_dir: str) -> list[dict]:
    """Scan and retrieve all local and global skills with parsed metadata and permissions."""
    skills_list = []
    
    # 1. Fetch skill permissions
    permissions = get_skill_permissions(work_dir)
    
    scopes = ["global", "local"]
    for scope in scopes:
        s_dir = get_skills_dir(work_dir, scope)
        if not os.path.exists(s_dir) or not os.path.isdir(s_dir):
            continue
            
        try:
            subdirs = [d for d in os.listdir(s_dir) if os.path.isdir(os.path.join(s_dir, d))]
            for d in sorted(subdirs):
                skill_path = os.path.join(s_dir, d, "SKILL.md")
                if os.path.exists(skill_path) and os.path.isfile(skill_path):
                    try:
                        with open(skill_path, "r", encoding="utf-8") as f:
                            raw_content = f.read()
                        
                        meta = parse_frontmatter(raw_content)
                        s_name = meta.get("name", d)
                        s_desc = meta.get("description", "No description provided.")
                        
                        # Resolve permission
                        perm = permissions.get(s_name, "ask")  # Default to 'ask'
                        
                        skills_list.append({
                            "name": s_name,
                            "description": s_desc,
                            "scope": scope,
                            "path": os.path.abspath(os.path.join(s_dir, d)),
                            "permission": perm,
                            "content": meta.get("_content", "")
                        })
                    except Exception as e:
                        logger.error(f"Failed to read skill at {skill_path}: {e}")
        except Exception as e:
            logger.error(f"Error scanning {scope} skills dir {s_dir}: {e}")
            
    return skills_list

def create_skill(work_dir: str, scope: str, name: str, description: str, content: str) -> str:
    """Create a new skill directory and SKILL.md file. Returns the path of the created SKILL.md."""
    # Validate name format
    safe_name = "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip()
    if not safe_name:
        raise ValueError("Invalid skill name.")
        
    s_dir = get_skills_dir(work_dir, scope)
    skill_folder = os.path.join(s_dir, safe_name)
    os.makedirs(skill_folder, exist_ok=True)
    
    skill_path = os.path.join(skill_folder, "SKILL.md")
    
    # Format YAML frontmatter
    file_content = f"""---
name: {safe_name}
description: {description}
compatibility: opencode
---

{content.strip()}
"""
    with open(skill_path, "w", encoding="utf-8") as f:
        f.write(file_content)
        
    return os.path.abspath(skill_path)

def delete_skill(work_dir: str, scope: str, name: str) -> bool:
    """Delete a skill folder from disk."""
    s_dir = get_skills_dir(work_dir, scope)
    skill_folder = os.path.abspath(os.path.join(s_dir, name))
    
    # Security traversal check
    if not skill_folder.startswith(s_dir):
        raise ValueError("Security Violation: Path traversal detected.")
        
    if os.path.exists(skill_folder) and os.path.isdir(skill_folder):
        shutil.rmtree(skill_folder)
        return True
    return False


def import_skill_from_url(url: str, scope: str, work_dir: str) -> list[str]:
    """Clones a Git repository or downloads a public URL, registers any SKILL.md found, and returns a list of imported skill names."""
    import tempfile
    import urllib.request
    import urllib.parse
    import subprocess
    
    imported = []
    s_dir = get_skills_dir(work_dir, scope)
    os.makedirs(s_dir, exist_ok=True)
    
    is_git = False
    if ("github.com" in url or url.endswith(".git")) and not ("/raw/" in url or "raw.githubusercontent.com" in url or url.endswith(".md") or url.endswith(".txt")):
        is_git = True
        
    if is_git:
        # Clone using git CLI
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                # Run git clone --depth 1
                subprocess.run(["git", "clone", "--depth", "1", url, temp_dir], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                raise RuntimeError(f"Git clone failed: {e}")
                
            # Scan recursively for SKILL.md files
            for root, dirs, files in os.walk(temp_dir):
                if "SKILL.md" in files:
                    file_path = os.path.join(root, "SKILL.md")
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            content = f.read()
                            
                        meta = parse_frontmatter(content)
                        # Default name to the parent directory name if not present in frontmatter
                        name = meta.get("name", os.path.basename(root))
                        # Validate name (only alphanumeric and hyphens/underscores)
                        name = "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip()
                        if not name:
                            continue
                            
                        # Create skill directory
                        dest_folder = os.path.join(s_dir, name)
                        os.makedirs(dest_folder, exist_ok=True)
                        dest_file = os.path.join(dest_folder, "SKILL.md")
                        
                        # Copy content
                        with open(dest_file, "w", encoding="utf-8") as f:
                            f.write(content)
                            
                        imported.append(name)
                    except Exception:
                        continue
    else:
        # Direct URL download
        try:
            req = urllib.request.Request(
                url, 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                content = response.read().decode('utf-8')
        except Exception as e:
            raise RuntimeError(f"Failed to fetch URL: {e}")
            
        meta = parse_frontmatter(content)
        # Default name to URL path's basename without extension
        url_path = urllib.parse.urlparse(url).path
        default_name = os.path.basename(url_path).split(".")[0] or "imported-skill"
        name = meta.get("name", default_name)
        name = "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip()
        if not name:
            name = "imported-skill"
            
        dest_folder = os.path.join(s_dir, name)
        os.makedirs(dest_folder, exist_ok=True)
        dest_file = os.path.join(dest_folder, "SKILL.md")
        
        with open(dest_file, "w", encoding="utf-8") as f:
            f.write(content)
            
        imported.append(name)
        
    return imported
