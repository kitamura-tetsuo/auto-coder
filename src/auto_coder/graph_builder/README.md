# Graph Builder

A tool that analyzes TypeScript and Python codebases to generate data for Neo4j graph databases.

## Overview

This tool analyzes TypeScript and Python projects and extracts the following:

- **Nodes**: File, Module, Function, Method, Class, Interface, Type
- **Edges**: IMPORTS, CALLS, CONTAINS, EXTENDS, IMPLEMENTS

Each node includes the following information:

- `id`: Unique identifier (16-character SHA1 hash)
- `kind`: Node type
- `fqname`: Fully qualified name (e.g., `src/user/service.ts:UserService.getUserById`)
- `sig`: Type signature (e.g., `(string)->Promise<User>`)
- `short`: Summary (30-80 tokens, JSDoc or auto-generated)
- `complexity`: Cyclomatic complexity
- `tokens_est`: Estimated token count
- `tags`: Side effect tags (IO, DB, NETWORK, ASYNC, PURE)

## Installation

### TypeScript version

```bash
cd graph-builder
npm install
npm run build
```

### Python version

```bash
cd graph-builder
# Dependencies: standard library only
```

## Usage

### TypeScript version

```bash
# Scan project
node dist/cli.js scan --project ./my-project --out ./out --languages typescript,python

# CSV output (for Neo4j bulk import)
node dist/cli.js emit-csv --out ./out

# JSON output (for batch operations)
node dist/cli.js emit-json --out ./out

# Detect changes from Git diff
node dist/cli.js diff --since HEAD~1 --out ./out
```

### Python version

```bash
# Scan project
python3 src/cli_python.py scan --project ./my-project --out ./out

# CSV output (for Neo4j bulk import)
python3 src/cli_python.py emit-csv --out ./out

# JSON output (for batch operations)
python3 src/cli_python.py emit-json --out ./out

# Detect changes from Git diff
python3 src/cli_python.py diff --since HEAD~1 --out ./out
```

## Command Options

### scan

Scans the project and extracts graph data.

```bash
graph-builder scan [options]
```

**Options:**

- `--project <path>`: Project path (default: `.`)
- `--out <path>`: Output directory (default: `./out`)
- `--mode <mode>`: Scan mode `full` or `diff` (default: `full`)
- `--since <ref>`: Git reference for diff mode
- `--limit <number>`: Limit on number of files to process
- `--batch-size <number>`: Batch size for output (default: `500`)
- `--languages <langs>`: Languages to scan (comma-separated: `typescript,python`)

### emit-csv

Outputs CSV files for Neo4j bulk import.

```bash
graph-builder emit-csv [options]
```

**Options:**

- `--out <path>`: Output directory (default: `./out`)

**Output Files:**

- `nodes.csv`: Node data
- `rels.csv`: Edge data

### emit-json

Outputs JSON files for batch operations.

```bash
graph-builder emit-json [options]
```

**Options:**

- `--out <path>`: Output directory (default: `./out`)

**Output Files:**

- `batch-{timestamp}.json`: Batch data

### diff

Generates diffs from Git changes.

```bash
graph-builder diff [options]
```

**Options:**

- `--since <ref>`: Git reference to compare (default: `HEAD~1`)
- `--out <path>`: Output directory (default: `./out`)

**Output Files:**

- `diff-{commit}.json`: Diff data

## Output Schema

### nodes.csv

```csv
id:ID,kind,fqname,sig,short,complexity:int,tokens_est:int,tags,file,start_line:int,end_line:int
a1b2c3d4e5f6,Function,src/user/service.ts:UserService.getUserById,(string)->Promise<User>,"gets user by id",6,28,DB;IO,src/user/service.ts,42,58
```

### rels.csv

```csv
:START_ID,:END_ID,type,count:int,locations
a1b2c3d4e5f6,aabbcc112233,CALLS,1,"[{""file"":""src/user/service.ts"",""line"":42}]"
```

### batch JSON

```json
{
  "nodes": [
    {
      "id": "a1b2c3",
      "kind": "Function",
      "fqname": "src/x:Foo.bar",
      "sig": "(id:string)->Promise<User>",
      "short": "fetch user by id",
      "complexity": 5,
      "tokens_est": 25,
      "tags": ["DB", "IO"]
    }
  ],
  "edges": [
    {
      "from": "a1b2c3",
      "to": "d4e5f6",
      "type": "CALLS",
      "count": 1
    }
  ]
}
```

### diff JSON

```json
{
  "meta": {
    "commit": "abcdef",
    "files": ["src/x.ts"],
    "timestamp": "2025-10-21T12:00:00Z"
  },
  "added": {
    "nodes": [...],
    "edges": [...]
  },
  "updated": {
    "nodes": [...],
    "edges": [...]
  },
  "removed": {
    "nodes": ["..."],
    "edges": ["..."]
  }
}
```

## Neo4j Import

### Bulk Import (initial load)

```bash
# Generate CSV files
graph-builder scan --project ./my-project --out ./out
graph-builder emit-csv --out ./out

# Import to Neo4j
neo4j-admin import \
  --nodes=./out/nodes.csv \
  --relationships=./out/rels.csv \
  --delimiter=',' \
  --array-delimiter=';'
```

### Online/Differential Updates (UNWIND)

```bash
# Generate JSON batch files
graph-builder scan --project ./my-project --out ./out
graph-builder emit-json --out ./out

# Load with Cypher query
CALL apoc.load.json('file:///out/batch-*.json') YIELD value
UNWIND value.nodes AS node
MERGE (n {id: node.id})
SET n = node

UNWIND value.edges AS edge
MATCH (from {id: edge.from})
MATCH (to {id: edge.to})
MERGE (from)-[r:CALLS]->(to)
SET r.count = edge.count
```

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Update Code Graph

on:
  push:
    branches: [main]

jobs:
  update-graph:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Setup Node.js
        uses: actions/setup-node@v3
        with:
          node-version: '18'

      - name: Install graph-builder
        run: |
          cd graph-builder
          npm install
          npm run build

      - name: Generate diff
        run: |
          node graph-builder/dist/cli.js diff --since ${{ github.event.before }} --out ./artifacts

      - name: Upload artifacts
        uses: actions/upload-artifact@v3
        with:
          name: graph-diff
          path: ./artifacts/diff-*.json
```

## Testing

### TypeScript

```bash
npm test
```

### Python

```bash
python3 src/tests/test_python_scanner.py
```

## Known Limitations

- **Unresolved symbols**: External libraries or dynamic imports are tagged with `unresolved: true`
- **Type inference**: Complex TypeScript type inference is not fully supported
- **Python type hints**: Types without hints are treated as `Any`

## License

MIT

## Contributing

Pull requests are welcome!

## Support

If you encounter any issues, please report them on GitHub Issues.
