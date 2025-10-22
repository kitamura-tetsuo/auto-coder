"use strict";
/**
 * CSV emitter for Neo4j bulk import
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
exports.emitCSV = emitCSV;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const csv_writer_1 = require("csv-writer");
/**
 * Emit nodes and edges as CSV files for Neo4j import
 * @param data Graph data
 * @param outputDir Output directory
 */
async function emitCSV(data, outputDir) {
    // Ensure output directory exists
    if (!fs.existsSync(outputDir)) {
        fs.mkdirSync(outputDir, { recursive: true });
    }
    // Write nodes.csv
    const nodesPath = path.join(outputDir, 'nodes.csv');
    const nodesCsvWriter = (0, csv_writer_1.createObjectCsvWriter)({
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
    const relsCsvWriter = (0, csv_writer_1.createObjectCsvWriter)({
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
function escapeCsvField(field) {
    if (!field)
        return '';
    // Replace newlines and quotes
    return field.replace(/\n/g, ' ').replace(/"/g, '""');
}
//# sourceMappingURL=csv.js.map