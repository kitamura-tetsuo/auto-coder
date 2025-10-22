#!/usr/bin/env node
"use strict";
/**
 * CLI for graph-builder
 */
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
const child_process_1 = require("child_process");
const commander_1 = require("commander");
const fs = __importStar(require("fs"));
const glob_1 = require("glob");
const path = __importStar(require("path"));
const csv_1 = require("./emitter/csv");
const json_1 = require("./emitter/json");
const javascript_1 = require("./scanner/javascript");
const typescript_1 = require("./scanner/typescript");
const program = new commander_1.Command();
/**
 * Find all tsconfig.json files in a project directory
 * Excludes node_modules and other common build directories
 */
function findTsConfigFiles(projectPath) {
    const tsConfigFiles = [];
    // First check if there's a tsconfig.json at the root
    const rootTsConfig = path.join(projectPath, 'tsconfig.json');
    if (fs.existsSync(rootTsConfig)) {
        tsConfigFiles.push(rootTsConfig);
        return tsConfigFiles; // If root tsconfig exists, use only that
    }
    // Otherwise, search for tsconfig.json files in subdirectories (monorepo support)
    try {
        const pattern = path.join(projectPath, '**/tsconfig.json');
        const files = glob_1.glob.sync(pattern, {
            ignore: [
                '**/node_modules/**',
                '**/dist/**',
                '**/build/**',
                '**/.next/**',
                '**/out/**',
                '**/.svelte-kit/**',
            ],
            absolute: true,
        });
        tsConfigFiles.push(...files);
    }
    catch (error) {
        console.error('Error searching for tsconfig.json files:', error);
    }
    return tsConfigFiles;
}
program
    .name('graph-builder')
    .description('TypeScript, JavaScript, and Python code analyzer for Neo4j graph database')
    .version('1.0.0');
program
    .command('scan')
    .description('Scan project and extract graph data')
    .option('--project <path>', 'Project path', '.')
    .option('--out <path>', 'Output directory', './out')
    .option('--mode <mode>', 'Scan mode: full or diff', 'full')
    .option('--since <ref>', 'Git reference for diff mode')
    .option('--limit <number>', 'Limit number of files to process')
    .option('--batch-size <number>', 'Batch size for output', '500')
    .option('--languages <langs>', 'Languages to scan (comma-separated): typescript,javascript,python', 'typescript,javascript,python')
    .action(async (options) => {
    try {
        const projectPath = path.resolve(options.project);
        const outputDir = path.resolve(options.out);
        const limit = options.limit ? parseInt(options.limit) : undefined;
        const languages = options.languages.split(',').map((l) => l.trim());
        console.log(`Scanning project: ${projectPath}`);
        console.log(`Output directory: ${outputDir}`);
        console.log(`Mode: ${options.mode}`);
        console.log(`Languages: ${languages.join(', ')}`);
        let graphData = { nodes: [], edges: [] };
        // Scan TypeScript
        if (languages.includes('typescript')) {
            const tsConfigFiles = findTsConfigFiles(projectPath);
            if (tsConfigFiles.length > 0) {
                console.log(`Found ${tsConfigFiles.length} tsconfig.json file(s)`);
                for (const tsConfigPath of tsConfigFiles) {
                    console.log(`Scanning TypeScript project: ${tsConfigPath}`);
                    try {
                        const tsData = (0, typescript_1.scanTypeScriptProject)(tsConfigPath, limit);
                        graphData.nodes.push(...tsData.nodes);
                        graphData.edges.push(...tsData.edges);
                        console.log(`  Found ${tsData.nodes.length} TypeScript nodes`);
                    }
                    catch (error) {
                        console.error(`  Error scanning ${tsConfigPath}:`, error);
                    }
                }
                console.log(`Total TypeScript nodes: ${graphData.nodes.length}`);
            }
            else {
                console.log('No tsconfig.json found, skipping TypeScript scan');
            }
        }
        // Scan JavaScript
        if (languages.includes('javascript')) {
            console.log('Scanning JavaScript files...');
            try {
                const jsData = (0, javascript_1.scanJavaScriptProject)(projectPath, limit);
                graphData.nodes.push(...jsData.nodes);
                graphData.edges.push(...jsData.edges);
                console.log(`Found ${jsData.nodes.length} JavaScript nodes`);
            }
            catch (error) {
                console.error('Error scanning JavaScript files:', error);
            }
        }
        // Scan Python
        if (languages.includes('python')) {
            console.log('Scanning Python files...');
            const pythonScanner = path.join(__dirname, 'scanner', 'python_scanner.py');
            if (fs.existsSync(pythonScanner)) {
                try {
                    const srcDir = path.join(__dirname, '..');
                    const result = (0, child_process_1.execSync)(`PYTHONPATH=${srcDir} python3 -c "import sys; from scanner.python_scanner import scan_python_project; import json; data = scan_python_project('${projectPath}', ${limit || 'None'}); print(json.dumps({'nodes': [n.__dict__ for n in data.nodes], 'edges': [{'from': e.from_id, 'to': e.to_id, 'type': e.type, 'count': e.count, 'locations': e.locations} for e in data.edges]}), file=sys.stdout)"`, { cwd: srcDir, encoding: 'utf-8', stdio: ['pipe', 'pipe', 'inherit'] });
                    const pyData = JSON.parse(result);
                    graphData.nodes.push(...pyData.nodes);
                    graphData.edges.push(...pyData.edges);
                    console.log(`Found ${pyData.nodes.length} Python nodes`);
                }
                catch (error) {
                    console.error('Error scanning Python files:', error);
                }
            }
        }
        // Ensure output directory exists
        if (!fs.existsSync(outputDir)) {
            fs.mkdirSync(outputDir, { recursive: true });
        }
        // Save intermediate data
        const dataPath = path.join(outputDir, 'graph-data.json');
        fs.writeFileSync(dataPath, JSON.stringify(graphData, null, 2));
        console.log(`Saved graph data to ${dataPath}`);
        console.log(`Total nodes: ${graphData.nodes.length}`);
        console.log(`Total edges: ${graphData.edges.length}`);
    }
    catch (error) {
        console.error('Error during scan:', error);
        process.exit(1);
    }
});
program
    .command('emit-csv')
    .description('Emit CSV files for Neo4j import')
    .option('--out <path>', 'Output directory', './out')
    .action(async (options) => {
    try {
        const outputDir = path.resolve(options.out);
        const dataPath = path.join(outputDir, 'graph-data.json');
        if (!fs.existsSync(dataPath)) {
            console.error(`Graph data not found at ${dataPath}. Run 'scan' first.`);
            process.exit(1);
        }
        const graphData = JSON.parse(fs.readFileSync(dataPath, 'utf-8'));
        await (0, csv_1.emitCSV)(graphData, outputDir);
        console.log('CSV files generated successfully');
    }
    catch (error) {
        console.error('Error emitting CSV:', error);
        process.exit(1);
    }
});
program
    .command('emit-json')
    .description('Emit JSON batch file')
    .option('--out <path>', 'Output directory', './out')
    .action((options) => {
    try {
        const outputDir = path.resolve(options.out);
        const dataPath = path.join(outputDir, 'graph-data.json');
        if (!fs.existsSync(dataPath)) {
            console.error(`Graph data not found at ${dataPath}. Run 'scan' first.`);
            process.exit(1);
        }
        const graphData = JSON.parse(fs.readFileSync(dataPath, 'utf-8'));
        (0, json_1.emitJSON)(graphData, outputDir);
        console.log('JSON batch file generated successfully');
    }
    catch (error) {
        console.error('Error emitting JSON:', error);
        process.exit(1);
    }
});
program
    .command('diff')
    .description('Generate diff from git changes')
    .option('--since <ref>', 'Git reference to compare against', 'HEAD~1')
    .option('--out <path>', 'Output directory', './out')
    .action((options) => {
    try {
        const outputDir = path.resolve(options.out);
        const since = options.since;
        console.log(`Generating diff since ${since}...`);
        // Get changed files
        const changedFiles = (0, child_process_1.execSync)(`git diff --name-only ${since}`, { encoding: 'utf-8' })
            .split('\n')
            .filter(f => f.endsWith('.ts') || f.endsWith('.py'));
        console.log(`Found ${changedFiles.length} changed files`);
        const diffData = {
            meta: {
                commit: (0, child_process_1.execSync)('git rev-parse HEAD', { encoding: 'utf-8' }).trim(),
                files: changedFiles,
                timestamp: new Date().toISOString(),
            },
            added: { nodes: [], edges: [] },
            updated: { nodes: [], edges: [] },
            removed: { nodes: [], edges: [] },
        };
        (0, json_1.emitDiffJSON)(diffData, outputDir, diffData.meta.commit?.slice(0, 8));
        console.log('Diff JSON generated successfully');
    }
    catch (error) {
        console.error('Error generating diff:', error);
        process.exit(1);
    }
});
program.parse(process.argv);
//# sourceMappingURL=cli.js.map