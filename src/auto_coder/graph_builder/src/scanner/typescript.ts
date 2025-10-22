/**
 * TypeScript code scanner using ts-morph
 */

import { Project, SyntaxKind, Node, FunctionDeclaration, MethodDeclaration, ClassDeclaration, InterfaceDeclaration, TypeAliasDeclaration, SourceFile } from 'ts-morph';
import { CodeNode, CodeEdge, GraphData } from '../types';
import { normalizeNode, synthesizeShortSummary, detectTags } from '../normalizer';
import { calculateComplexity } from '../utils/cyclomatic';
import { generateFileId } from '../utils/hash';

/**
 * Scan TypeScript project and extract graph data
 * @param tsConfigPath Path to tsconfig.json
 * @param limit Optional limit on number of files to process
 * @returns Graph data with nodes and edges
 */
export function scanTypeScriptProject(tsConfigPath: string, limit?: number): GraphData {
  const project = new Project({ tsConfigFilePath: tsConfigPath });
  const sourceFiles = project.getSourceFiles();
  
  const nodes: CodeNode[] = [];
  const edges: CodeEdge[] = [];
  const edgeMap = new Map<string, CodeEdge>();

  const filesToProcess = limit ? sourceFiles.slice(0, limit) : sourceFiles;

  for (const sourceFile of filesToProcess) {
    const filePath = sourceFile.getFilePath();
    
    // Add file node
    const fileNode = createFileNode(sourceFile);
    nodes.push(fileNode);

    // Process all declarations
    sourceFile.forEachDescendant((node) => {
      if (node.getKind() === SyntaxKind.FunctionDeclaration) {
        const funcNode = processFunctionDeclaration(node as FunctionDeclaration, filePath);
        if (funcNode) {
          nodes.push(funcNode);
          edges.push({
            from: fileNode.id,
            to: funcNode.id,
            type: 'CONTAINS',
            count: 1,
          });
        }
      } else if (node.getKind() === SyntaxKind.MethodDeclaration) {
        const methodNode = processMethodDeclaration(node as MethodDeclaration, filePath);
        if (methodNode) {
          nodes.push(methodNode);
        }
      } else if (node.getKind() === SyntaxKind.ClassDeclaration) {
        const classNode = processClassDeclaration(node as ClassDeclaration, filePath);
        if (classNode) {
          nodes.push(classNode);
          edges.push({
            from: fileNode.id,
            to: classNode.id,
            type: 'CONTAINS',
            count: 1,
          });
        }
      } else if (node.getKind() === SyntaxKind.InterfaceDeclaration) {
        const interfaceNode = processInterfaceDeclaration(node as InterfaceDeclaration, filePath);
        if (interfaceNode) {
          nodes.push(interfaceNode);
          edges.push({
            from: fileNode.id,
            to: interfaceNode.id,
            type: 'CONTAINS',
            count: 1,
          });
        }
      } else if (node.getKind() === SyntaxKind.TypeAliasDeclaration) {
        const typeNode = processTypeAliasDeclaration(node as TypeAliasDeclaration, filePath);
        if (typeNode) {
          nodes.push(typeNode);
          edges.push({
            from: fileNode.id,
            to: typeNode.id,
            type: 'CONTAINS',
            count: 1,
          });
        }
      } else if (node.getKind() === SyntaxKind.CallExpression) {
        const callEdge = processCallExpression(node, filePath);
        if (callEdge) {
          const key = `${callEdge.from}-${callEdge.to}-${callEdge.type}`;
          const existing = edgeMap.get(key);
          if (existing) {
            existing.count++;
            if (callEdge.locations && callEdge.locations[0]) {
              existing.locations = existing.locations || [];
              existing.locations.push(callEdge.locations[0]);
            }
          } else {
            edgeMap.set(key, callEdge);
          }
        }
      }
    });

    // Process imports
    const importEdges = processImports(sourceFile, fileNode.id);
    importEdges.forEach(edge => {
      const key = `${edge.from}-${edge.to}-${edge.type}`;
      if (!edgeMap.has(key)) {
        edgeMap.set(key, edge);
      }
    });
  }

  edges.push(...Array.from(edgeMap.values()));

  return { nodes, edges };
}

function createFileNode(sourceFile: SourceFile): CodeNode {
  const filePath = sourceFile.getFilePath();
  return normalizeNode({
    kind: 'File',
    fqname: filePath,
    sig: '',
    short: `File: ${filePath}`,
    complexity: 0,
    file: filePath,
  });
}

function processFunctionDeclaration(node: FunctionDeclaration, filePath: string): CodeNode | null {
  const name = node.getName();
  if (!name) return null;

  const fqname = `${filePath}:${name}`;
  const sig = generateSignature(node);
  const jsdoc = node.getJsDocs()[0]?.getComment();
  const params = node.getParameters().map(p => p.getName());
  const short = synthesizeShortSummary(typeof jsdoc === 'string' ? jsdoc : undefined, name, params);
  const complexity = calculateComplexity(node);
  const code = node.getText();
  const tags = detectTags(code, sig);

  return normalizeNode({
    kind: 'Function',
    fqname,
    sig,
    short,
    complexity,
    tags,
    file: filePath,
    start_line: node.getStartLineNumber(),
    end_line: node.getEndLineNumber(),
  });
}

function processMethodDeclaration(node: MethodDeclaration, filePath: string): CodeNode | null {
  const name = node.getName();
  const className = node.getParent()?.getSymbol()?.getName() || 'Unknown';
  const fqname = `${filePath}:${className}.${name}`;
  const sig = generateSignature(node);
  const jsdoc = node.getJsDocs()[0]?.getComment();
  const params = node.getParameters().map(p => p.getName());
  const short = synthesizeShortSummary(typeof jsdoc === 'string' ? jsdoc : undefined, name, params);
  const complexity = calculateComplexity(node);
  const code = node.getText();
  const tags = detectTags(code, sig);

  return normalizeNode({
    kind: 'Method',
    fqname,
    sig,
    short,
    complexity,
    tags,
    file: filePath,
    start_line: node.getStartLineNumber(),
    end_line: node.getEndLineNumber(),
  });
}

function processClassDeclaration(node: ClassDeclaration, filePath: string): CodeNode | null {
  const name = node.getName();
  if (!name) return null;

  const fqname = `${filePath}:${name}`;
  const sig = `class ${name}`;
  const jsdoc = node.getJsDocs()[0]?.getComment();
  const short = typeof jsdoc === 'string' ? jsdoc : `Class ${name}`;

  return normalizeNode({
    kind: 'Class',
    fqname,
    sig,
    short,
    complexity: 0,
    file: filePath,
    start_line: node.getStartLineNumber(),
    end_line: node.getEndLineNumber(),
  });
}

function processInterfaceDeclaration(node: InterfaceDeclaration, filePath: string): CodeNode | null {
  const name = node.getName();
  const fqname = `${filePath}:${name}`;
  const sig = `interface ${name}`;
  const jsdoc = node.getJsDocs()[0]?.getComment();
  const short = typeof jsdoc === 'string' ? jsdoc : `Interface ${name}`;

  return normalizeNode({
    kind: 'Interface',
    fqname,
    sig,
    short,
    complexity: 0,
    file: filePath,
    start_line: node.getStartLineNumber(),
    end_line: node.getEndLineNumber(),
  });
}

function processTypeAliasDeclaration(node: TypeAliasDeclaration, filePath: string): CodeNode | null {
  const name = node.getName();
  const fqname = `${filePath}:${name}`;
  const sig = `type ${name}`;
  const jsdoc = node.getJsDocs()[0]?.getComment();
  const short = typeof jsdoc === 'string' ? jsdoc : `Type ${name}`;

  return normalizeNode({
    kind: 'Type',
    fqname,
    sig,
    short,
    complexity: 0,
    file: filePath,
    start_line: node.getStartLineNumber(),
    end_line: node.getEndLineNumber(),
  });
}

function generateSignature(node: FunctionDeclaration | MethodDeclaration): string {
  const params = node.getParameters().map(p => {
    const type = p.getType().getText();
    return type;
  }).join(',');
  
  const returnType = node.getReturnType().getText();
  return `(${params})->${returnType}`;
}

function processCallExpression(node: Node, filePath: string): CodeEdge | null {
  try {
    const callExpr = node.asKind(SyntaxKind.CallExpression);
    if (!callExpr) return null;

    const expression = callExpr.getExpression();
    const symbol = expression.getSymbol();
    
    if (!symbol) {
      return null;
    }

    const declarations = symbol.getDeclarations();
    if (declarations.length === 0) return null;

    const decl = declarations[0];
    const declFile = decl.getSourceFile().getFilePath();
    const declName = symbol.getName();
    const calleeFqname = `${declFile}:${declName}`;

    // Find caller
    const callerFunc = node.getFirstAncestorByKind(SyntaxKind.FunctionDeclaration) ||
                       node.getFirstAncestorByKind(SyntaxKind.MethodDeclaration);
    
    if (!callerFunc) return null;

    const callerName = callerFunc.getSymbol()?.getName() || 'unknown';
    const callerFqname = `${filePath}:${callerName}`;

    return {
      from: callerFqname,
      to: calleeFqname,
      type: 'CALLS',
      count: 1,
      locations: [{
        file: filePath,
        line: node.getStartLineNumber(),
      }],
    };
  } catch (e) {
    return null;
  }
}

function processImports(sourceFile: SourceFile, fileId: string): CodeEdge[] {
  const edges: CodeEdge[] = [];
  const imports = sourceFile.getImportDeclarations();

  for (const imp of imports) {
    const moduleSpecifier = imp.getModuleSpecifierValue();
    const resolvedModule = imp.getModuleSpecifierSourceFile();
    
    if (resolvedModule) {
      const targetPath = resolvedModule.getFilePath();
      const targetId = generateFileId(targetPath);
      
      edges.push({
        from: fileId,
        to: targetId,
        type: 'IMPORTS',
        count: 1,
      });
    }
  }

  return edges;
}

