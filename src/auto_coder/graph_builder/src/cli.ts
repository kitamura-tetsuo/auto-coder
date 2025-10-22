#!/usr/bin/env node
/**
 * CLI for graph-builder
 */

import { execSync } from 'child_process';
import { Command } from 'commander';
import * as fs from 'fs';
import { glob } from 'glob';
import * as path from 'path';
import { emitCSV } from './emitter/csv';
import { emitDiffJSON, emitJSON } from './emitter/json';
import { scanJavaScriptProject } from './scanner/javascript';
import { scanTypeScriptProject } from './scanner/typescript';
import { DiffData, GraphData } from './types';

const program = new Command();

/**
 * Find all tsconfig.json files in a project directory
 * Excludes node_modules and other common build directories
 */
function findTsConfigFiles(projectPath: string): string[] {
  const tsConfigFiles: string[] = [];

  // First check if there's a tsconfig.json at the root
  const rootTsConfig = path.join(projectPath, 'tsconfig.json');
  if (fs.existsSync(rootTsConfig)) {
    tsConfigFiles.push(rootTsConfig);
    return tsConfigFiles; // If root tsconfig exists, use only that
  }

  // Otherwise, search for tsconfig.json files in subdirectories (monorepo support)
  try {
    const pattern = path.join(projectPath, '**/tsconfig.json');
    const files = glob.sync(pattern, {
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
  } catch (error) {
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
      const languages = options.languages.split(',').map((l: string) => l.trim());

      console.log(`Scanning project: ${projectPath}`);
      console.log(`Output directory: ${outputDir}`);
      console.log(`Mode: ${options.mode}`);
      console.log(`Languages: ${languages.join(', ')}`);

      let graphData: GraphData = { nodes: [], edges: [] };

      // Scan TypeScript
      if (languages.includes('typescript')) {
        const tsConfigFiles = findTsConfigFiles(projectPath);
        if (tsConfigFiles.length > 0) {
          console.log(`Found ${tsConfigFiles.length} tsconfig.json file(s)`);
          for (const tsConfigPath of tsConfigFiles) {
            console.log(`Scanning TypeScript project: ${tsConfigPath}`);
            try {
              const tsData = scanTypeScriptProject(tsConfigPath, limit);
              graphData.nodes.push(...tsData.nodes);
              graphData.edges.push(...tsData.edges);
              console.log(`  Found ${tsData.nodes.length} TypeScript nodes`);
            } catch (error) {
              console.error(`  Error scanning ${tsConfigPath}:`, error);
            }
          }
          console.log(`Total TypeScript nodes: ${graphData.nodes.length}`);
        } else {
          console.log('No tsconfig.json found, skipping TypeScript scan');
        }
      }

      // Scan JavaScript
      if (languages.includes('javascript')) {
        console.log('Scanning JavaScript files...');
        try {
          const jsData = scanJavaScriptProject(projectPath, limit);
          graphData.nodes.push(...jsData.nodes);
          graphData.edges.push(...jsData.edges);
          console.log(`Found ${jsData.nodes.length} JavaScript nodes`);
        } catch (error) {
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
            const result = execSync(
              `PYTHONPATH=${srcDir} python3 -c "import sys; from scanner.python_scanner import scan_python_project; import json; data = scan_python_project('${projectPath}', ${limit || 'None'}); print(json.dumps({'nodes': [n.__dict__ for n in data.nodes], 'edges': [{'from': e.from_id, 'to': e.to_id, 'type': e.type, 'count': e.count, 'locations': e.locations} for e in data.edges]}), file=sys.stdout)"`,
              { cwd: srcDir, encoding: 'utf-8', stdio: ['pipe', 'pipe', 'inherit'] }
            );
            const pyData = JSON.parse(result);
            graphData.nodes.push(...pyData.nodes);
            graphData.edges.push(...pyData.edges);
            console.log(`Found ${pyData.nodes.length} Python nodes`);
          } catch (error) {
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

    } catch (error) {
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

      const graphData: GraphData = JSON.parse(fs.readFileSync(dataPath, 'utf-8'));
      await emitCSV(graphData, outputDir);
      console.log('CSV files generated successfully');

    } catch (error) {
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

      const graphData: GraphData = JSON.parse(fs.readFileSync(dataPath, 'utf-8'));
      emitJSON(graphData, outputDir);
      console.log('JSON batch file generated successfully');

    } catch (error) {
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
      const changedFiles = execSync(`git diff --name-only ${since}`, { encoding: 'utf-8' })
        .split('\n')
        .filter(f => f.endsWith('.ts') || f.endsWith('.py'));

      console.log(`Found ${changedFiles.length} changed files`);

      const diffData: DiffData = {
        meta: {
          commit: execSync('git rev-parse HEAD', { encoding: 'utf-8' }).trim(),
          files: changedFiles,
          timestamp: new Date().toISOString(),
        },
        added: { nodes: [], edges: [] },
        updated: { nodes: [], edges: [] },
        removed: { nodes: [], edges: [] },
      };

      emitDiffJSON(diffData, outputDir, diffData.meta.commit?.slice(0, 8));
      console.log('Diff JSON generated successfully');

    } catch (error) {
      console.error('Error generating diff:', error);
      process.exit(1);
    }
  });

program.parse(process.argv);

