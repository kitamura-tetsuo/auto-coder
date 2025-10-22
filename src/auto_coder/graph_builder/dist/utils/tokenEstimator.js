"use strict";
/**
 * Token estimation utilities
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.estimateTokens = estimateTokens;
exports.calculateNodeTokens = calculateNodeTokens;
/**
 * Estimate the number of tokens in a text
 * Simple heuristic: characters / 4
 * @param text Text to estimate
 * @returns Estimated token count
 */
function estimateTokens(text) {
    if (!text)
        return 0;
    return Math.ceil(text.length / 4);
}
/**
 * Calculate total tokens for a node
 * @param short Short summary
 * @param sig Signature
 * @returns Total estimated tokens
 */
function calculateNodeTokens(short, sig) {
    return estimateTokens(short) + estimateTokens(sig);
}
//# sourceMappingURL=tokenEstimator.js.map