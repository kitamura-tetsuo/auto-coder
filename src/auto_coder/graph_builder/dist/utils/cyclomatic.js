"use strict";
/**
 * Cyclomatic complexity calculation utilities
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.calculateComplexity = calculateComplexity;
exports.calculatePythonComplexity = calculatePythonComplexity;
const ts_morph_1 = require("ts-morph");
/**
 * Calculate cyclomatic complexity for a TypeScript node
 * Counts: if, switch, for, while, do-while, catch, conditional expressions, logical operators
 * @param node AST node
 * @returns Complexity score
 */
function calculateComplexity(node) {
    let complexity = 1; // Base complexity
    node.forEachDescendant((child) => {
        const kind = child.getKind();
        switch (kind) {
            case ts_morph_1.SyntaxKind.IfStatement:
            case ts_morph_1.SyntaxKind.ForStatement:
            case ts_morph_1.SyntaxKind.ForInStatement:
            case ts_morph_1.SyntaxKind.ForOfStatement:
            case ts_morph_1.SyntaxKind.WhileStatement:
            case ts_morph_1.SyntaxKind.DoStatement:
            case ts_morph_1.SyntaxKind.CaseClause:
            case ts_morph_1.SyntaxKind.CatchClause:
            case ts_morph_1.SyntaxKind.ConditionalExpression:
                complexity++;
                break;
            case ts_morph_1.SyntaxKind.AmpersandAmpersandToken:
            case ts_morph_1.SyntaxKind.BarBarToken:
            case ts_morph_1.SyntaxKind.QuestionQuestionToken:
                complexity++;
                break;
        }
    });
    return complexity;
}
/**
 * Calculate cyclomatic complexity for Python code (simple heuristic)
 * @param code Python source code
 * @returns Complexity score
 */
function calculatePythonComplexity(code) {
    let complexity = 1; // Base complexity
    const lines = code.split('\n');
    const patterns = [
        /^\s*(if|elif)\s+/,
        /^\s*for\s+/,
        /^\s*while\s+/,
        /^\s*except\s*/,
        /^\s*with\s+/,
        /\s+and\s+/,
        /\s+or\s+/,
    ];
    for (const line of lines) {
        for (const pattern of patterns) {
            if (pattern.test(line)) {
                complexity++;
                break;
            }
        }
    }
    return complexity;
}
//# sourceMappingURL=cyclomatic.js.map