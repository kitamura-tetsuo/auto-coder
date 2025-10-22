/**
 * JSON emitter for batch operations
 */

import * as fs from 'fs';
import * as path from 'path';
import { GraphData, DiffData } from '../types';

/**
 * Emit graph data as JSON batch file
 * @param data Graph data
 * @param outputDir Output directory
 * @param timestamp Optional timestamp for filename
 */
export function emitJSON(data: GraphData, outputDir: string, timestamp?: string): void {
  // Ensure output directory exists
  if (!fs.existsSync(outputDir)) {
    fs.mkdirSync(outputDir, { recursive: true });
  }

  const ts = timestamp || new Date().toISOString().replace(/[:.]/g, '-');
  const outputPath = path.join(outputDir, `batch-${ts}.json`);

  const batchData = {
    nodes: data.nodes.map(node => ({
      id: node.id,
      kind: node.kind,
      fqname: node.fqname,
      sig: node.sig,
      short: node.short,
      complexity: node.complexity,
      tokens_est: node.tokens_est,
      tags: node.tags || [],
      file: node.file,
      start_line: node.start_line,
      end_line: node.end_line,
    })),
    edges: data.edges.map(edge => ({
      from: edge.from,
      to: edge.to,
      type: edge.type,
      count: edge.count,
      locations: edge.locations,
    })),
  };

  fs.writeFileSync(outputPath, JSON.stringify(batchData, null, 2));
  console.log(`Wrote batch JSON to ${outputPath}`);
}

/**
 * Emit diff data as JSON file
 * @param diff Diff data
 * @param outputDir Output directory
 * @param commit Optional commit hash
 */
export function emitDiffJSON(diff: DiffData, outputDir: string, commit?: string): void {
  // Ensure output directory exists
  if (!fs.existsSync(outputDir)) {
    fs.mkdirSync(outputDir, { recursive: true });
  }

  const commitHash = commit || 'latest';
  const outputPath = path.join(outputDir, `diff-${commitHash}.json`);

  fs.writeFileSync(outputPath, JSON.stringify(diff, null, 2));
  console.log(`Wrote diff JSON to ${outputPath}`);
}

