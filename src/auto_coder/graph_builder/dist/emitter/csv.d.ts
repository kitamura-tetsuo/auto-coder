/**
 * CSV emitter for Neo4j bulk import
 */
import { GraphData } from '../types';
/**
 * Emit nodes and edges as CSV files for Neo4j import
 * @param data Graph data
 * @param outputDir Output directory
 */
export declare function emitCSV(data: GraphData, outputDir: string): Promise<void>;
//# sourceMappingURL=csv.d.ts.map