/**
 * Token estimation utilities
 */
/**
 * Estimate the number of tokens in a text
 * Simple heuristic: characters / 4
 * @param text Text to estimate
 * @returns Estimated token count
 */
export declare function estimateTokens(text: string): number;
/**
 * Calculate total tokens for a node
 * @param short Short summary
 * @param sig Signature
 * @returns Total estimated tokens
 */
export declare function calculateNodeTokens(short: string, sig: string): number;
//# sourceMappingURL=tokenEstimator.d.ts.map