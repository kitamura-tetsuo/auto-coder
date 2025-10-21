"""
CSV and JSON emitters for Python
"""

import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from scanner.python_scanner import GraphData, CodeNode, CodeEdge


def emit_csv(data: GraphData, output_dir: str) -> None:
    """Emit nodes and edges as CSV files for Neo4j import"""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Write nodes.csv
    nodes_path = output_path / 'nodes.csv'
    with open(nodes_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'id:ID', 'kind', 'fqname', 'sig', 'short', 
            'complexity:int', 'tokens_est:int', 'tags', 
            'file', 'start_line:int', 'end_line:int'
        ])
        
        for node in data.nodes:
            writer.writerow([
                node.id,
                node.kind,
                node.fqname,
                node.sig,
                escape_csv_field(node.short),
                node.complexity,
                node.tokens_est,
                ';'.join(node.tags) if node.tags else '',
                node.file or '',
                node.start_line or '',
                node.end_line or '',
            ])
    
    print(f"Wrote {len(data.nodes)} nodes to {nodes_path}")
    
    # Write rels.csv
    rels_path = output_path / 'rels.csv'
    with open(rels_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([':START_ID', ':END_ID', 'type', 'count:int', 'locations'])
        
        for edge in data.edges:
            writer.writerow([
                edge.from_id,
                edge.to_id,
                edge.type,
                edge.count,
                json.dumps(edge.locations) if edge.locations else '',
            ])
    
    print(f"Wrote {len(data.edges)} edges to {rels_path}")


def emit_json(data: GraphData, output_dir: str, timestamp: str = None) -> None:
    """Emit graph data as JSON batch file"""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    if timestamp is None:
        from datetime import datetime
        timestamp = datetime.now().isoformat().replace(':', '-').replace('.', '-')
    
    output_file = output_path / f'batch-{timestamp}.json'
    
    batch_data = {
        'nodes': [
            {
                'id': node.id,
                'kind': node.kind,
                'fqname': node.fqname,
                'sig': node.sig,
                'short': node.short,
                'complexity': node.complexity,
                'tokens_est': node.tokens_est,
                'tags': node.tags or [],
                'file': node.file,
                'start_line': node.start_line,
                'end_line': node.end_line,
            }
            for node in data.nodes
        ],
        'edges': [
            {
                'from': edge.from_id,
                'to': edge.to_id,
                'type': edge.type,
                'count': edge.count,
                'locations': edge.locations,
            }
            for edge in data.edges
        ],
    }
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(batch_data, f, indent=2)
    
    print(f"Wrote batch JSON to {output_file}")


def emit_diff_json(diff_data: Dict[str, Any], output_dir: str, commit: str = None) -> None:
    """Emit diff data as JSON file"""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    commit_hash = commit or 'latest'
    output_file = output_path / f'diff-{commit_hash}.json'
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(diff_data, f, indent=2)
    
    print(f"Wrote diff JSON to {output_file}")


def escape_csv_field(field: str) -> str:
    """Escape CSV field to prevent injection"""
    if not field:
        return ''
    # Replace newlines and quotes
    return field.replace('\n', ' ').replace('"', '""')

