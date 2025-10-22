"use strict";
/**
 * Normalizer for generating fqname, sig, id, short, complexity, tokens_est
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.synthesizeShortSummary = synthesizeShortSummary;
exports.normalizeNode = normalizeNode;
exports.detectTags = detectTags;
const hash_1 = require("./utils/hash");
const tokenEstimator_1 = require("./utils/tokenEstimator");
/**
 * Synthesize a short summary from JSDoc or function name
 * @param jsdoc JSDoc comment (if available)
 * @param name Function/method name
 * @param params Parameter names
 * @returns Short summary (30-80 tokens)
 */
function synthesizeShortSummary(jsdoc, name, params) {
    // Priority 1: Use first line of JSDoc if available
    if (jsdoc) {
        const lines = jsdoc.trim().split('\n');
        const firstLine = lines[0].replace(/^\/?\*+\s*/, '').replace(/\*+\/$/, '').trim();
        if (firstLine && firstLine.length > 0) {
            return truncateToTokenLimit(firstLine, 80);
        }
    }
    // Priority 2: Generate from function name using verb + object pattern
    const summary = generateSummaryFromName(name, params);
    return truncateToTokenLimit(summary, 80);
}
/**
 * Generate summary from function name
 * @param name Function name
 * @param params Parameter names
 * @returns Generated summary
 */
function generateSummaryFromName(name, params) {
    // Convert camelCase/PascalCase to words
    const words = name.replace(/([A-Z])/g, ' $1').toLowerCase().trim();
    // Common verb patterns
    const verbPatterns = [
        { prefix: 'get', template: 'gets {object}' },
        { prefix: 'set', template: 'sets {object}' },
        { prefix: 'create', template: 'creates {object}' },
        { prefix: 'delete', template: 'deletes {object}' },
        { prefix: 'update', template: 'updates {object}' },
        { prefix: 'fetch', template: 'fetches {object}' },
        { prefix: 'find', template: 'finds {object}' },
        { prefix: 'search', template: 'searches {object}' },
        { prefix: 'validate', template: 'validates {object}' },
        { prefix: 'process', template: 'processes {object}' },
        { prefix: 'handle', template: 'handles {object}' },
        { prefix: 'calculate', template: 'calculates {object}' },
        { prefix: 'compute', template: 'computes {object}' },
        { prefix: 'is', template: 'checks if {object}' },
        { prefix: 'has', template: 'checks if has {object}' },
    ];
    for (const pattern of verbPatterns) {
        if (words.startsWith(pattern.prefix)) {
            const object = words.slice(pattern.prefix.length).trim() || 'value';
            return pattern.template.replace('{object}', object);
        }
    }
    // Default: use the words as-is
    return words || 'performs operation';
}
/**
 * Truncate text to token limit
 * @param text Text to truncate
 * @param maxTokens Maximum tokens
 * @returns Truncated text
 */
function truncateToTokenLimit(text, maxTokens) {
    const estimatedTokens = Math.ceil(text.length / 4);
    if (estimatedTokens <= maxTokens) {
        return text;
    }
    const maxChars = maxTokens * 4;
    return text.slice(0, maxChars - 3) + '...';
}
/**
 * Normalize a code node with all required fields
 * @param partial Partial node data
 * @returns Complete normalized node
 */
function normalizeNode(partial) {
    const id = (0, hash_1.generateId)(partial.fqname, partial.sig);
    const short = partial.short || '';
    const tokens_est = (0, tokenEstimator_1.calculateNodeTokens)(short, partial.sig);
    return {
        id,
        kind: partial.kind,
        fqname: partial.fqname,
        sig: partial.sig,
        short,
        complexity: partial.complexity || 1,
        tokens_est,
        tags: partial.tags || [],
        unresolved: partial.unresolved || false,
        file: partial.file,
        start_line: partial.start_line,
        end_line: partial.end_line,
    };
}
/**
 * Detect side-effect tags from code patterns
 * @param code Source code
 * @param sig Signature
 * @returns Array of tags
 */
function detectTags(code, sig) {
    const tags = [];
    // IO operations
    if (/\b(read|write|open|close|fs\.|file)\b/i.test(code)) {
        tags.push('IO');
    }
    // Database operations
    if (/\b(query|execute|select|insert|update|delete|db\.|database)\b/i.test(code)) {
        tags.push('DB');
    }
    // Network operations
    if (/\b(fetch|http|request|axios|ajax|socket|ws\.|websocket)\b/i.test(code)) {
        tags.push('NETWORK');
    }
    // Async operations
    if (/\basync\b/.test(code) || /Promise</.test(sig)) {
        tags.push('ASYNC');
    }
    // Pure function heuristic (no side effects detected and no async)
    if (tags.length === 0 && !/\basync\b/.test(code) && !/Promise</.test(sig)) {
        tags.push('PURE');
    }
    return tags;
}
//# sourceMappingURL=normalizer.js.map