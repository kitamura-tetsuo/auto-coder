/**
 * JavaScript/JSX code scanner using ts-morph
 * 
 * This scanner analyzes JavaScript and JSX files by treating them as TypeScript
 * with allowJs enabled. ts-morph can parse JavaScript files without type annotations.
 */

import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import { Project, SourceFile, SyntaxKind, Node, FunctionDeclaration, ClassDeclaration, VariableStatement, ArrowFunction, FunctionExpression } from 'ts-morph';
import { CodeEdge, CodeNode, GraphData } from '../types';

/**
 * Generate unique ID from fqname and signature
 */
function generateId(fqname: string, sig: string): string {
  const hash = crypto.createHash('sha1');
  hash.update(fqname + sig);
  return hash.digest('hex').substring(0, 16);
}

/**
 * Generate unique ID for a file
 */
function generateFileId(filePath: string): string {
  const hash = crypto.createHash('sha1');
  hash.update(filePath);
  return hash.digest('hex').substring(0, 16);
}

/**
 * Estimate token count (simple heuristic: chars / 4)
 */
function estimateTokens(text: string): number {
  if (!text) return 0;
  return Math.ceil(text.length / 4);
}

/**
 * Calculate cyclomatic complexity
 */
function calculateComplexity(node: Node): number {
  let complexity = 1;
  
  node.forEachDescendant((child) => {
    const kind = child.getKind();
    if (
      kind === SyntaxKind.IfStatement ||
      kind === SyntaxKind.ForStatement ||
      kind === SyntaxKind.ForInStatement ||
      kind === SyntaxKind.ForOfStatement ||
      kind === SyntaxKind.WhileStatement ||
      kind === SyntaxKind.DoStatement ||
      kind === SyntaxKind.CaseClause ||
      kind === SyntaxKind.CatchClause ||
      kind === SyntaxKind.ConditionalExpression ||
      kind === SyntaxKind.BinaryExpression
    ) {
      if (kind === SyntaxKind.BinaryExpression) {
        const binExpr = child.asKind(SyntaxKind.BinaryExpression);
        if (binExpr) {
          const opKind = binExpr.getOperatorToken().getKind();
          if (opKind === SyntaxKind.AmpersandAmpersandToken || opKind === SyntaxKind.BarBarToken) {
            complexity++;
          }
        }
      } else {
        complexity++;
      }
    }
  });
  
  return complexity;
}

/**
 * Generate short summary from JSDoc or function name
 */
function synthesizeShortSummary(jsDocs: string | undefined, name: string, params: string[]): string {
  // Priority 1: Use JSDoc description
  if (jsDocs) {
    const lines = jsDocs.trim().split('\n');
    for (const line of lines) {
      const cleaned = line.replace(/^\s*\*\s*/, '').trim();
      if (cleaned && !cleaned.startsWith('@')) {
        return truncateToTokenLimit(cleaned, 80);
      }
    }
  }
  
  // Priority 2: Generate from function name
  const summary = generateSummaryFromName(name, params);
  return truncateToTokenLimit(summary, 80);
}

/**
 * Generate summary from function name
 */
function generateSummaryFromName(name: string, params: string[]): string {
  // Convert camelCase to words
  const words = name.replace(/([A-Z])/g, ' $1').toLowerCase().trim();
  
  // Common verb patterns
  const verbPatterns: Record<string, string> = {
    'get': 'gets {object}',
    'set': 'sets {object}',
    'create': 'creates {object}',
    'delete': 'deletes {object}',
    'update': 'updates {object}',
    'fetch': 'fetches {object}',
    'load': 'loads {object}',
    'save': 'saves {object}',
    'handle': 'handles {event}',
    'on': 'handles {event}',
    'is': 'checks if {condition}',
    'has': 'checks if has {property}',
    'can': 'checks if can {action}',
  };
  
  for (const [verb, template] of Object.entries(verbPatterns)) {
    if (words.startsWith(verb + ' ')) {
      const object = words.substring(verb.length + 1);
      return template.replace('{object}', object).replace('{event}', object).replace('{condition}', object).replace('{property}', object).replace('{action}', object);
    }
  }
  
  // Default: just use the words
  return words;
}

/**
 * Truncate text to token limit
 */
function truncateToTokenLimit(text: string, maxTokens: number): string {
  const maxChars = maxTokens * 4;
  if (text.length <= maxChars) {
    return text;
  }
  return text.substring(0, maxChars - 3) + '...';
}

/**
 * Scan a JavaScript/JSX source file
 */
function scanJavaScriptFile(sourceFile: SourceFile, projectPath: string): GraphData {
  const nodes: CodeNode[] = [];
  const edges: CodeEdge[] = [];
  const edgeMap = new Map<string, CodeEdge>();
  
  const filePath = sourceFile.getFilePath();
  const relativePath = path.relative(projectPath, filePath);
  
  // Add file node
  const fileId = generateFileId(filePath);
  nodes.push({
    id: fileId,
    kind: 'File',
    fqname: filePath,
    sig: '',
    short: `File: ${relativePath}`,
    complexity: 0,
    tokens_est: estimateTokens(filePath),
    tags: [],
    unresolved: false,
    file: filePath,
  });
  
  // Scan functions
  sourceFile.getFunctions().forEach((func) => {
    const name = func.getName() || '<anonymous>';
    const params = func.getParameters().map(p => p.getName());
    const sig = `(${params.join(', ')})`;
    const fqname = `${relativePath}::${name}`;
    const id = generateId(fqname, sig);
    
    const jsDocs = func.getJsDocs().map(doc => doc.getDescription()).join('\n');
    const short = synthesizeShortSummary(jsDocs, name, params);
    
    nodes.push({
      id,
      kind: 'Function',
      fqname,
      sig,
      short,
      complexity: calculateComplexity(func),
      tokens_est: estimateTokens(func.getText()),
      tags: [],
      unresolved: false,
      file: filePath,
      start_line: func.getStartLineNumber(),
      end_line: func.getEndLineNumber(),
    });
    
    // Add CONTAINS edge from file to function
    const edgeKey = `${fileId}-${id}-CONTAINS`;
    edgeMap.set(edgeKey, {
      from: fileId,
      to: id,
      type: 'CONTAINS',
      count: 1,
      locations: [],
    });
  });
  
  // Scan classes
  sourceFile.getClasses().forEach((cls) => {
    const name = cls.getName() || '<anonymous>';
    const fqname = `${relativePath}::${name}`;
    const id = generateId(fqname, '');
    
    const jsDocs = cls.getJsDocs().map(doc => doc.getDescription()).join('\n');
    const short = jsDocs ? truncateToTokenLimit(jsDocs.split('\n')[0], 80) : `Class ${name}`;
    
    nodes.push({
      id,
      kind: 'Class',
      fqname,
      sig: '',
      short,
      complexity: 0,
      tokens_est: estimateTokens(cls.getText()),
      tags: [],
      unresolved: false,
      file: filePath,
      start_line: cls.getStartLineNumber(),
      end_line: cls.getEndLineNumber(),
    });
    
    // Add CONTAINS edge from file to class
    const edgeKey = `${fileId}-${id}-CONTAINS`;
    edgeMap.set(edgeKey, {
      from: fileId,
      to: id,
      type: 'CONTAINS',
      count: 1,
      locations: [],
    });
    
    // Scan methods
    cls.getMethods().forEach((method) => {
      const methodName = method.getName();
      const params = method.getParameters().map(p => p.getName());
      const sig = `(${params.join(', ')})`;
      const methodFqname = `${fqname}.${methodName}`;
      const methodId = generateId(methodFqname, sig);
      
      const methodJsDocs = method.getJsDocs().map(doc => doc.getDescription()).join('\n');
      const methodShort = synthesizeShortSummary(methodJsDocs, methodName, params);
      
      nodes.push({
        id: methodId,
        kind: 'Method',
        fqname: methodFqname,
        sig,
        short: methodShort,
        complexity: calculateComplexity(method),
        tokens_est: estimateTokens(method.getText()),
        tags: [],
        unresolved: false,
        file: filePath,
        start_line: method.getStartLineNumber(),
        end_line: method.getEndLineNumber(),
      });
      
      // Add CONTAINS edge from class to method
      const methodEdgeKey = `${id}-${methodId}-CONTAINS`;
      edgeMap.set(methodEdgeKey, {
        from: id,
        to: methodId,
        type: 'CONTAINS',
        count: 1,
        locations: [],
      });
    });
  });
  
  edges.push(...edgeMap.values());
  
  return { nodes, edges };
}

/**
 * Scan JavaScript project
 */
export function scanJavaScriptProject(projectPath: string, limit?: number): GraphData {
  const project = new Project({
    compilerOptions: {
      allowJs: true,
      checkJs: false,
      noEmit: true,
    },
  });
  
  // Find all JavaScript and JSX files
  const jsFiles: string[] = [];
  const extensions = ['.js', '.jsx'];
  
  function findJsFiles(dir: string) {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      
      // Skip excluded directories
      if (entry.isDirectory()) {
        if (['node_modules', 'dist', 'build', '.git', '.svelte-kit', '__pycache__'].includes(entry.name)) {
          continue;
        }
        findJsFiles(fullPath);
      } else if (entry.isFile()) {
        const ext = path.extname(entry.name);
        if (extensions.includes(ext)) {
          jsFiles.push(fullPath);
        }
      }
    }
  }
  
  findJsFiles(projectPath);
  
  if (limit) {
    jsFiles.splice(limit);
  }
  
  // Add files to project
  jsFiles.forEach(file => project.addSourceFileAtPath(file));
  
  const allNodes: CodeNode[] = [];
  const allEdges: CodeEdge[] = [];
  
  // Scan each file
  project.getSourceFiles().forEach((sourceFile) => {
    const { nodes, edges } = scanJavaScriptFile(sourceFile, projectPath);
    allNodes.push(...nodes);
    allEdges.push(...edges);
  });
  
  return { nodes: allNodes, edges: allEdges };
}

