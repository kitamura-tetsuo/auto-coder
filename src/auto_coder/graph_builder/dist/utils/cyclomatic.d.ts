/**
 * Cyclomatic complexity calculation utilities
 */
import { Node } from 'ts-morph';
/**
 * Calculate cyclomatic complexity for a TypeScript node
 * Counts: if, switch, for, while, do-while, catch, conditional expressions, logical operators
 * @param node AST node
 * @returns Complexity score
 */
export declare function calculateComplexity(node: Node): number;
/**
 * Calculate cyclomatic complexity for Python code (simple heuristic)
 * @param code Python source code
 * @returns Complexity score
 */
export declare function calculatePythonComplexity(code: string): number;
//# sourceMappingURL=cyclomatic.d.ts.map