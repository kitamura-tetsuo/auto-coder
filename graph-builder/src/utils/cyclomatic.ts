/**
 * Cyclomatic complexity calculation utilities
 */

import { SyntaxKind, Node } from 'ts-morph';

/**
 * Calculate cyclomatic complexity for a TypeScript node
 * Counts: if, switch, for, while, do-while, catch, conditional expressions, logical operators
 * @param node AST node
 * @returns Complexity score
 */
export function calculateComplexity(node: Node): number {
  let complexity = 1; // Base complexity

  node.forEachDescendant((child) => {
    const kind = child.getKind();
    
    switch (kind) {
      case SyntaxKind.IfStatement:
      case SyntaxKind.ForStatement:
      case SyntaxKind.ForInStatement:
      case SyntaxKind.ForOfStatement:
      case SyntaxKind.WhileStatement:
      case SyntaxKind.DoStatement:
      case SyntaxKind.CaseClause:
      case SyntaxKind.CatchClause:
      case SyntaxKind.ConditionalExpression:
        complexity++;
        break;
      case SyntaxKind.AmpersandAmpersandToken:
      case SyntaxKind.BarBarToken:
      case SyntaxKind.QuestionQuestionToken:
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
export function calculatePythonComplexity(code: string): number {
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

