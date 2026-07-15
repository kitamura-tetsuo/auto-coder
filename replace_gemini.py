import os
import re

def process_file(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        return
    
    orig_content = content
    
    # Replace antigravity with antigravity
    content = content.replace("antigravity", "antigravity")
    
    # Replace google-gemini/antigravity URL with antigravity URL
    content = re.sub(r'https://github\.com/google-gemini/antigravity', 'https://antigravity.google', content)
    
    # Replace visit https://antigravity.google with URL
    content = re.sub(r'visit https://antigravity.google', 'visit https://antigravity.google', content)

    # In subprocess or command strings: "antigravity " -> "antigravity "
    # "agy mcp" -> "agy mcp"
    # "agy login" -> "agy login"
    # ['antigravity', ...] -> ['antigravity', ...]
    
    # Let's replace 'antigravity' executable references:
    content = re.sub(r'(?<=[\'"])gemini(?=[\'"]\s*,)', 'antigravity', content) # ['antigravity', ...]
    content = re.sub(r'(?<=[\'"])gemini(?=\s+)', 'antigravity', content) # "antigravity ..."
    content = re.sub(r'\bgemini(?=\s+mcp)', 'antigravity', content) # antigravity mcp
    content = re.sub(r'\bgemini(?=\s+login)', 'antigravity', content) # antigravity login
    content = re.sub(r'\bgemini(?=\s+--model)', 'antigravity', content) # antigravity --model
    
    if orig_content != content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Updated {filepath}")

for root, dirs, files in os.walk('.'):
    if '.git' in root or '__pycache__' in root or 'egg-info' in root or '.pytest_cache' in root or '.venv' in root:
        continue
    for file in files:
        if file.endswith('.py') or file.endswith('.md') or file.endswith('.sh') or file.endswith('.txt'):
            process_file(os.path.join(root, file))
