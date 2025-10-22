"use strict";
/**
 * JSON emitter for batch operations
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
exports.emitJSON = emitJSON;
exports.emitDiffJSON = emitDiffJSON;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
/**
 * Emit graph data as JSON batch file
 * @param data Graph data
 * @param outputDir Output directory
 * @param timestamp Optional timestamp for filename
 */
function emitJSON(data, outputDir, timestamp) {
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
function emitDiffJSON(diff, outputDir, commit) {
    // Ensure output directory exists
    if (!fs.existsSync(outputDir)) {
        fs.mkdirSync(outputDir, { recursive: true });
    }
    const commitHash = commit || 'latest';
    const outputPath = path.join(outputDir, `diff-${commitHash}.json`);
    fs.writeFileSync(outputPath, JSON.stringify(diff, null, 2));
    console.log(`Wrote diff JSON to ${outputPath}`);
}
//# sourceMappingURL=json.js.map