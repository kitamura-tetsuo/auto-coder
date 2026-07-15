import os
import re

def process_file(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        return
    
    orig_content = content
    
    # "antigravity" in list -> "agy"
    # Actually let's be very specific:
    content = content.replace('["agy"]', '["agy"]')
    content = content.replace('["agy",', '["agy",')
    content = content.replace('"agy mcp"', '"agy mcp"')
    content = content.replace('"agy login"', '"agy login"')
    content = content.replace('"agy --version"', '"agy --version"')
    content = content.replace('\'agy mcp list\'', '\'agy mcp list\'')
    content = content.replace('agy mcp add', 'agy mcp add')
    content = content.replace('agy mcp list', 'agy mcp list')
    content = content.replace('agy --version', 'agy --version')
    content = content.replace("tool_name=\"antigravity\",", "tool_name=\"agy\",")
    
    # In auth_utils.py, it probably has 'antigravity login' or similar.
    
    if orig_content != content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Updated {filepath}")

for root, dirs, files in os.walk('.'):
    if '.git' in root or '__pycache__' in root or 'egg-info' in root or '.pytest_cache' in root or '.venv' in root:
        continue
    for file in files:
        if file.endswith('.py') or file.endswith('.md') or file.endswith('.sh') or file.endswith('.txt') or file.endswith('.yaml'):
            process_file(os.path.join(root, file))
