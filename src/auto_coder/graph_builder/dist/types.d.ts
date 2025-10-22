/**
 * Common types for graph-builder
 */
export type NodeKind = 'File' | 'Module' | 'Function' | 'Method' | 'Class' | 'Interface' | 'Type';
export type EdgeType = 'IMPORTS' | 'CALLS' | 'CONTAINS' | 'EXTENDS' | 'IMPLEMENTS';
export interface CodeNode {
    id: string;
    kind: NodeKind;
    fqname: string;
    sig: string;
    short: string;
    complexity: number;
    tokens_est: number;
    tags?: string[];
    unresolved?: boolean;
    file?: string;
    start_line?: number;
    end_line?: number;
}
export interface CodeEdge {
    from: string;
    to: string;
    type: EdgeType;
    count: number;
    locations?: Array<{
        file: string;
        line: number;
    }>;
}
export interface GraphData {
    nodes: CodeNode[];
    edges: CodeEdge[];
}
export interface DiffData {
    meta: {
        commit?: string;
        files: string[];
        timestamp: string;
    };
    added: GraphData;
    updated: GraphData;
    removed: {
        nodes: string[];
        edges: string[];
    };
}
export interface ScanOptions {
    project: string;
    out: string;
    mode: 'full' | 'diff';
    since?: string;
    limit?: number;
    batchSize?: number;
    languages?: ('typescript' | 'python')[];
}
//# sourceMappingURL=types.d.ts.map