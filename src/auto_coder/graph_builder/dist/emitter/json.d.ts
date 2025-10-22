/**
 * JSON emitter for batch operations
 */
import { GraphData, DiffData } from '../types';
/**
 * Emit graph data as JSON batch file
 * @param data Graph data
 * @param outputDir Output directory
 * @param timestamp Optional timestamp for filename
 */
export declare function emitJSON(data: GraphData, outputDir: string, timestamp?: string): void;
/**
 * Emit diff data as JSON file
 * @param diff Diff data
 * @param outputDir Output directory
 * @param commit Optional commit hash
 */
export declare function emitDiffJSON(diff: DiffData, outputDir: string, commit?: string): void;
//# sourceMappingURL=json.d.ts.map