/**
 * TypeScript code scanner using ts-morph
 */
import { GraphData } from '../types';
/**
 * Scan TypeScript project and extract graph data
 * @param tsConfigPath Path to tsconfig.json
 * @param limit Optional limit on number of files to process
 * @returns Graph data with nodes and edges
 */
export declare function scanTypeScriptProject(tsConfigPath: string, limit?: number): GraphData;
//# sourceMappingURL=typescript.d.ts.map