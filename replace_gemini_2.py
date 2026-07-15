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
    content = content.replace("https://antigravity.google", "https://antigravity.google")
    
    # Replace visit https://antigravity.google with URL
    content = content.replace("visit https://antigravity.google", "visit https://antigravity.google")

    # Let's replace 'antigravity' executable references:
    # "antigravity" in list:
    content = re.sub(r'(?<=[\'"])gemini(?=[\'"]\s*,?)', 'antigravity', content)
    # "antigravity ..." in string:
    content = re.sub(r'(?<=[\'"])gemini(?=\s+)', 'antigravity', content)
    # antigravity mcp
    content = re.sub(r'\bgemini(?=\s+mcp\b)', 'antigravity', content)
    # antigravity login
    content = re.sub(r'\bgemini(?=\s+login\b)', 'antigravity', content)
    # antigravity --model
    content = re.sub(r'\bgemini(?=\s+--model\b)', 'antigravity', content)
    # antigravity (then type
    content = re.sub(r'\bgemini(?=\s+\(then type\b)', 'antigravity', content)
    # antigravity CLI
    content = re.sub(r'\bgemini(?=\s+CLI\b)', 'antigravity', content)
    
    # Also, there's a reference: "using Gemini API key from antigravity CLI config"
    # Actually, replacing "antigravity CLI" with "antigravity CLI"
    content = content.replace("antigravity CLI", "antigravity CLI")
    content = content.replace("Antigravity CLI", "Antigravity CLI")

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
