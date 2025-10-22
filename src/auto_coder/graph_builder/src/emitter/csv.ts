/**
 * CSV emitter for Neo4j bulk import
 */

import * as fs from 'fs';
import * as path from 'path';
import { createObjectCsvWriter } from 'csv-writer';
import { GraphData } from '../types';

/**
 * Emit nodes and edges as CSV files for Neo4j import
 * @param data Graph data
 * @param outputDir Output directory
 */
export async function emitCSV(data: GraphData, outputDir: string): Promise<void> {
  // Ensure output directory exists
  if (!fs.existsSync(outputDir)) {
    fs.mkdirSync(outputDir, { recursive: true });
  }

  // Write nodes.csv
  const nodesPath = path.join(outputDir, 'nodes.csv');
  const nodesCsvWriter = createObjectCsvWriter({
    path: nodesPath,
    header: [
      { id: 'id', title: 'id:ID' },
      { id: 'kind', title: 'kind' },
      { id: 'fqname', title: 'fqname' },
      { id: 'sig', title: 'sig' },
      { id: 'short', title: 'short' },
      { id: 'complexity', title: 'complexity:int' },
      { id: 'tokens_est', title: 'tokens_est:int' },
      { id: 'tags', title: 'tags' },
      { id: 'file', title: 'file' },
      { id: 'start_line', title: 'start_line:int' },
      { id: 'end_line', title: 'end_line:int' },
    ],
  });

  const nodeRecords = data.nodes.map(node => ({
    id: node.id,
    kind: node.kind,
    fqname: node.fqname,
    sig: node.sig,
    short: escapeCsvField(node.short),
    complexity: node.complexity,
    tokens_est: node.tokens_est,
    tags: node.tags?.join(';') || '',
    file: node.file || '',
    start_line: node.start_line || '',
    end_line: node.end_line || '',
  }));

  await nodesCsvWriter.writeRecords(nodeRecords);
  console.log(`Wrote ${nodeRecords.length} nodes to ${nodesPath}`);

  // Write rels.csv
  const relsPath = path.join(outputDir, 'rels.csv');
  const relsCsvWriter = createObjectCsvWriter({
    path: relsPath,
    header: [
      { id: 'start', title: ':START_ID' },
      { id: 'end', title: ':END_ID' },
      { id: 'type', title: 'type' },
      { id: 'count', title: 'count:int' },
      { id: 'locations', title: 'locations' },
    ],
  });

  const relRecords = data.edges.map(edge => ({
    start: edge.from,
    end: edge.to,
    type: edge.type,
    count: edge.count,
    locations: edge.locations ? JSON.stringify(edge.locations) : '',
  }));

  await relsCsvWriter.writeRecords(relRecords);
  console.log(`Wrote ${relRecords.length} edges to ${relsPath}`);
}

/**
 * Escape CSV field to prevent injection
 * @param field Field value
 * @returns Escaped field
 */
function escapeCsvField(field: string): string {
  if (!field) return '';
  // Replace newlines and quotes
  return field.replace(/\n/g, ' ').replace(/"/g, '""');
}

