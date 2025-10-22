"use strict";
/**
 * JavaScript/JSX code scanner using ts-morph
 *
 * This scanner analyzes JavaScript and JSX files by treating them as TypeScript
 * with allowJs enabled. ts-morph can parse JavaScript files without type annotations.
 */
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.scanJavaScriptProject = scanJavaScriptProject;
const crypto = __importStar(require("crypto"));
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const ts_morph_1 = require("ts-morph");
/**
 * Generate unique ID from fqname and signature
 */
function generateId(fqname, sig) {
    const hash = crypto.createHash('sha1');
    hash.update(fqname + sig);
    return hash.digest('hex').substring(0, 16);
}
/**
 * Generate unique ID for a file
 */
function generateFileId(filePath) {
    const hash = crypto.createHash('sha1');
    hash.update(filePath);
    return hash.digest('hex').substring(0, 16);
}
/**
 * Estimate token count (simple heuristic: chars / 4)
 */
function estimateTokens(text) {
    if (!text)
        return 0;
    return Math.ceil(text.length / 4);
}
/**
 * Calculate cyclomatic complexity
 */
function calculateComplexity(node) {
    let complexity = 1;
    node.forEachDescendant((child) => {
        const kind = child.getKind();
        if (kind === ts_morph_1.SyntaxKind.IfStatement ||
            kind === ts_morph_1.SyntaxKind.ForStatement ||
            kind === ts_morph_1.SyntaxKind.ForInStatement ||
            kind === ts_morph_1.SyntaxKind.ForOfStatement ||
            kind === ts_morph_1.SyntaxKind.WhileStatement ||
            kind === ts_morph_1.SyntaxKind.DoStatement ||
            kind === ts_morph_1.SyntaxKind.CaseClause ||
            kind === ts_morph_1.SyntaxKind.CatchClause ||
            kind === ts_morph_1.SyntaxKind.ConditionalExpression ||
            kind === ts_morph_1.SyntaxKind.BinaryExpression) {
            if (kind === ts_morph_1.SyntaxKind.BinaryExpression) {
                const binExpr = child.asKind(ts_morph_1.SyntaxKind.BinaryExpression);
                if (binExpr) {
                    const opKind = binExpr.getOperatorToken().getKind();
                    if (opKind === ts_morph_1.SyntaxKind.AmpersandAmpersandToken || opKind === ts_morph_1.SyntaxKind.BarBarToken) {
                        complexity++;
                    }
                }
            }
            else {
                complexity++;
            }
        }
    });
    return complexity;
}
/**
 * Generate short summary from JSDoc or function name
 */
function synthesizeShortSummary(jsDocs, name, params) {
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
function generateSummaryFromName(name, params) {
    // Convert camelCase to words
    const words = name.replace(/([A-Z])/g, ' $1').toLowerCase().trim();
    // Common verb patterns
    const verbPatterns = {
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
function truncateToTokenLimit(text, maxTokens) {
    const maxChars = maxTokens * 4;
    if (text.length <= maxChars) {
        return text;
    }
    return text.substring(0, maxChars - 3) + '...';
}
/**
 * Scan a JavaScript/JSX source file
 */
function scanJavaScriptFile(sourceFile, projectPath) {
    const nodes = [];
    const edges = [];
    const edgeMap = new Map();
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
function scanJavaScriptProject(projectPath, limit) {
    const project = new ts_morph_1.Project({
        compilerOptions: {
            allowJs: true,
            checkJs: false,
            noEmit: true,
        },
    });
    // Find all JavaScript and JSX files
    const jsFiles = [];
    const extensions = ['.js', '.jsx'];
    function findJsFiles(dir) {
        const entries = fs.readdirSync(dir, { withFileTypes: true });
        for (const entry of entries) {
            const fullPath = path.join(dir, entry.name);
            // Skip excluded directories
            if (entry.isDirectory()) {
                if (['node_modules', 'dist', 'build', '.git', '.svelte-kit', '__pycache__'].includes(entry.name)) {
                    continue;
                }
                findJsFiles(fullPath);
            }
            else if (entry.isFile()) {
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
    const allNodes = [];
    const allEdges = [];
    // Scan each file
    project.getSourceFiles().forEach((sourceFile) => {
        const { nodes, edges } = scanJavaScriptFile(sourceFile, projectPath);
        allNodes.push(...nodes);
        allEdges.push(...edges);
    });
    return { nodes: allNodes, edges: allEdges };
}
//# sourceMappingURL=javascript.js.map