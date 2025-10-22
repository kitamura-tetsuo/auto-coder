/**
 * Normalizer for generating fqname, sig, id, short, complexity, tokens_est
 */
import { CodeNode } from './types';
/**
 * Synthesize a short summary from JSDoc or function name
 * @param jsdoc JSDoc comment (if available)
 * @param name Function/method name
 * @param params Parameter names
 * @returns Short summary (30-80 tokens)
 */
export declare function synthesizeShortSummary(jsdoc: string | undefined, name: string, params: string[]): string;
/**
 * Normalize a code node with all required fields
 * @param partial Partial node data
 * @returns Complete normalized node
 */
export declare function normalizeNode(partial: Partial<CodeNode> & {
    kind: CodeNode['kind'];
    fqname: string;
    sig: string;
}): CodeNode;
/**
 * Detect side-effect tags from code patterns
 * @param code Source code
 * @param sig Signature
 * @returns Array of tags
 */
export declare function detectTags(code: string, sig: string): string[];
//# sourceMappingURL=normalizer.d.ts.map